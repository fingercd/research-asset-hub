from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import UUID

import yaml
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.catalog_queries import serialize_catalog_asset
from apps.api.code_browser import (
    CodeBrowserUnavailable,
    local_code_repository,
)
from apps.api.config import ApiSettings
from apps.api.contracts import (
    AssetPatch,
)
from apps.api.database import DatabaseManager
from apps.api.domain_views import presentation_domains
from apps.api.editor import CODE_REPRESENTATION_TYPES, resolve_editor_path
from apps.api.events import record_mutation, stable_hash
from apps.api.pagination import encode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.search_runtime import SearchRuntime
from apps.api.serialization import (
    serialize_asset,
    serialize_domain_overview,
    serialize_quick_action,
    serialize_representation,
    serialize_research_unit,
)
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.enums import (
    AuditAction,
    FieldClaimStatus,
    FieldResolutionKind,
)
from packages.domain.models import (
    Activity,
    Annotation,
    Asset,
    AssetRedirect,
    BackupRun,
    FieldClaim,
    FieldResolution,
    IndexState,
    Job,
    ReadingState,
    Relation,
    RelationEvidenceModel,
    Representation,
    Source,
)
from packages.domain.serving_scope import (
    RetiredRepresentationError,
    active_representation_clause,
    assert_representation_active,
)
from packages.ingestion.safe_tree_policy import path_has_reparse

router = APIRouter(prefix="/api/v1")
PAGE_SIZE = 30
_NO_CODE_ALLOWLIST_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "check-unit-actions-allowlist.yaml"
)
_NO_CODE_AUDIT_REASON = (
    "经审计暂无本地可用的官方公开实现；如存在联网线索，其可能仅为占位项目页或第三方仓库。"
)
_WORD = re.compile(r"[\w\u3400-\u9fff-]+", re.UNICODE)
_OVERRIDABLE_ASSET_FIELDS = frozenset(
    {
        "abstract",
        "analysis",
        "asset_type",
        "attributes",
        "authors",
        "content",
        "domain_slugs",
        "language",
        "source_urls",
        "subtitle",
        "summary",
        "tags",
        "title",
        "title_zh",
        "venue",
        "visibility",
        "year",
    }
)


class RepresentationOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presentation: Literal["hub", "original"] = "original"
    embed: bool = False


def _database(request: Request) -> DatabaseManager:
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, DatabaseManager):
        raise RuntimeError("API database manager is not initialized.")
    return database


def _settings(request: Request) -> ApiSettings:
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, ApiSettings):
        raise RuntimeError("API settings are not initialized.")
    return settings


def _search_runtime(request: Request) -> SearchRuntime:
    runtime = getattr(request.app.state, "search_runtime", None)
    if isinstance(runtime, SearchRuntime):
        return runtime
    runtime = SearchRuntime.connect(_settings(request))
    request.app.state.search_runtime = runtime
    return runtime


def _source_registry(request: Request) -> SourceRegistry:
    registry = getattr(request.app.state, "source_registry", None)
    if isinstance(registry, SourceRegistry):
        return registry
    registry = SourceRegistry.load(
        _settings(request).sources_config_path, data_dir=_settings(request).data_dir
    )
    request.app.state.source_registry = registry
    return registry


def _editor_payload(
    request: Request,
    representation: Representation,
    *,
    source_alias: str | None,
) -> dict[str, Any]:
    resolution = resolve_editor_path(
        _source_registry(request),
        representation,
        source_alias=source_alias,
    )
    return {
        "available": resolution.available,
        "editor": resolution.editor,
        "path": resolution.path,
        "uri": resolution.uri,
        "reason": resolution.reason,
    }


def _serialize_representation_for_ui(
    request: Request,
    representation: Representation,
    *,
    source_alias: str | None,
) -> dict[str, Any]:
    result = serialize_representation(representation)
    if representation.representation_type in CODE_REPRESENTATION_TYPES:
        editor = _editor_payload(request, representation, source_alias=source_alias)
        result["local_available"] = editor["available"]
        result["local_reason"] = editor["reason"]
    return result


def _retired_representation_problem(
    error: RetiredRepresentationError,
) -> ApiProblem:
    """Build a path-free 410 response for historical representation IDs."""

    successor = error.successor_representation_id
    headers = (
        {
            "Link": (
                f'</api/v1/representations/{successor}/open-actions>; rel="successor-version"'
            ),
            "X-Successor-Representation-ID": str(successor),
        }
        if successor is not None
        else None
    )
    return ApiProblem(
        410,
        "表示层已退出服务",
        error.detail,
        problem_type=f"urn:research-asset-hub:problem:{error.code.replace('_', '-')}",
        code=error.code,
        headers=headers,
        errors=[
            {"successor_representation_id": (str(successor) if successor is not None else None)}
        ],
    )


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return str(value) if value is not None else None


def _etag(version: int) -> str:
    return f'W/"{version}"'


def _check_if_match(
    if_match: str | None,
    version: int,
    *,
    required: bool = False,
) -> None:
    if if_match is None:
        if required:
            raise ApiProblem(
                428,
                "缺少版本条件",
                "PATCH requests must include the current If-Match ETag.",
                problem_type="urn:research-asset-hub:problem:precondition-required",
                headers={"ETag": _etag(version)},
            )
        return
    allowed = {item.strip() for item in if_match.split(",")}
    if "*" not in allowed and _etag(version) not in allowed and f'"{version}"' not in allowed:
        raise ApiProblem(
            412,
            "资源版本已变化",
            "The supplied If-Match value does not match the current row version.",
            problem_type="urn:research-asset-hub:problem:precondition-failed",
            headers={"ETag": _etag(version)},
        )


def _list_payload(items: list[dict[str, Any]], offset: int, total: int) -> dict[str, Any]:
    next_cursor = encode_cursor(offset + len(items)) if offset + len(items) < total else None
    return {"items": items, "next_cursor": next_cursor, "total": total}


def _live_asset(session: Session, asset_id: UUID, *, include_deleted: bool = False) -> Asset:
    canonical_id, _ = _resolve_asset_redirect(session, asset_id)
    asset = session.get(Asset, canonical_id)
    if (
        asset is None
        or _is_system_hidden_asset(asset)
        or (asset.deleted_at is not None and not include_deleted)
    ):
        raise not_found("Asset", asset_id)
    return asset


def _resolve_asset_redirect(
    session: Session,
    asset_id: UUID,
) -> tuple[UUID, list[AssetRedirect]]:
    current = asset_id
    visited: set[UUID] = set()
    chain: list[AssetRedirect] = []
    while True:
        if current in visited:
            raise ApiProblem(
                500,
                "资产重定向损坏",
                "An asset redirect cycle was detected; no resource was changed.",
                problem_type="urn:research-asset-hub:problem:redirect-cycle",
            )
        visited.add(current)
        redirect = session.get(AssetRedirect, current)
        if redirect is None:
            return current, chain
        chain.append(redirect)
        current = redirect.to_asset_id


def _set_canonical_asset_headers(
    response: Response,
    *,
    requested_id: UUID,
    canonical_id: UUID,
) -> None:
    if requested_id == canonical_id:
        return
    canonical_path = f"/api/v1/assets/{canonical_id}"
    response.headers["Content-Location"] = canonical_path
    response.headers["Link"] = f'<{canonical_path}>; rel="canonical"'
    response.headers["X-Canonical-Asset-ID"] = str(canonical_id)
    response.headers["X-Asset-Redirect"] = "permanent"


def _live_annotation(
    session: Session,
    annotation_id: UUID,
    *,
    include_deleted: bool = False,
) -> Annotation:
    annotation = session.get(Annotation, annotation_id)
    if annotation is None or (annotation.deleted_at is not None and not include_deleted):
        raise not_found("Annotation", annotation_id)
    return annotation


def _asset_context(
    session: Session,
    assets: Iterable[Asset],
) -> tuple[dict[UUID, ReadingState], dict[UUID, str]]:
    asset_ids = [item.id for item in assets]
    if not asset_ids:
        return {}, {}
    states = session.scalars(
        select(ReadingState).where(
            ReadingState.user_id == "local",
            ReadingState.asset_id.in_(asset_ids),
        )
    ).all()
    annotations = session.scalars(
        select(Annotation)
        .where(
            Annotation.asset_id.in_(asset_ids),
            Annotation.deleted_at.is_(None),
        )
        .order_by(Annotation.pinned.desc(), Annotation.updated_at.desc())
    ).all()
    summaries: dict[UUID, str] = {}
    for annotation in annotations:
        if annotation.asset_id in summaries:
            continue
        text = annotation.summary or annotation.body_markdown or annotation.title or ""
        summaries[annotation.asset_id] = " ".join(text.split())[:60]
    return {item.asset_id: item for item in states}, summaries


def _serialize_assets(session: Session, assets: list[Asset]) -> list[dict[str, Any]]:
    states, summaries = _asset_context(session, assets)
    return [
        serialize_asset(
            asset,
            reading_state=states.get(asset.id),
            annotation_summary=summaries.get(asset.id),
        )
        for asset in assets
    ]


def _asset_snapshot(asset: Asset) -> dict[str, Any]:
    return serialize_asset(asset)


def _apply_asset_patch(asset: Asset, patch: AssetPatch) -> None:
    for key, value in patch.model_dump(exclude_unset=True).items():
        setattr(asset, key, value)


def _tokens(text: str) -> set[str]:
    return {token.casefold() for token in _WORD.findall(text) if len(token) > 1}


def _is_system_hidden_asset(asset: Asset) -> bool:
    value = (asset.attributes or {}).get("system_hidden")
    return value is True or (isinstance(value, str) and value.casefold() == "true")


_ACTION_REPRESENTATION_TYPES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "html": frozenset({"html", "html_document"}),
    "guide": frozenset({"guide", "analysis", "markdown"}),
    "code": frozenset({"source_code", "repository", "notebook"}),
}
_HTML_REPRESENTATION_TYPES = frozenset({"html", "guide", "analysis", "html_document"})
_REPRESENTATION_ROLE_ORDER = {
    role: index
    for index, role in enumerate(
        (
            "primary",
            "original",
            "official",
            "guide",
            "analysis",
            "annotated",
            "supplemental",
            "mirror",
            "snapshot",
            "derived",
        )
    )
}


def _representation_matches_action(
    representation: Representation,
    kind: str,
) -> bool:
    if kind == "guide":
        return (
            representation.role == "guide"
            or representation.representation_type in _ACTION_REPRESENTATION_TYPES["guide"]
        )
    if kind == "html":
        return (
            representation.role != "guide" or bool(representation.attributes.get("unified_guide"))
        ) and representation.representation_type in _ACTION_REPRESENTATION_TYPES["html"]
    return representation.representation_type in _ACTION_REPRESENTATION_TYPES[kind]


def _has_available_action(
    representations: Sequence[Representation],
    kind: str,
) -> bool:
    return any(
        representation.availability == "available"
        and _representation_matches_action(representation, kind)
        for representation in representations
    )


def _representation_is_local(
    representation: Representation,
    *,
    source_registry: SourceRegistry | None = None,
    source_alias: str | None = None,
) -> bool:
    if bool(representation.path_ref) and representation.materialization_kind != "url":
        return True
    if (
        source_registry is not None
        and representation.representation_type in CODE_REPRESENTATION_TYPES
    ):
        return resolve_editor_path(
            source_registry,
            representation,
            source_alias=source_alias,
        ).available
    return False


def _representation_is_serving_local_code(
    representation: Representation,
    *,
    source_registry: SourceRegistry | None,
    source_alias: str | None,
) -> bool:
    """Require a registered, real, non-reparse code path for allowlist override."""

    if (
        representation.availability != "available"
        or representation.representation_type not in CODE_REPRESENTATION_TYPES
        or representation.materialization_kind not in {"directory", "file"}
        or not representation.path_ref
        or source_registry is None
        or source_alias is None
    ):
        return False
    root_key = representation.root_key or "repository"
    try:
        root = source_registry.root_for(source_alias, root_key)
    except UnsafeAssetPath:
        return False
    path_ref = PurePosixPath(representation.path_ref.replace("\\", "/"))
    if path_ref.is_absolute() or ".." in path_ref.parts:
        return False
    candidate = root.joinpath(*path_ref.parts)
    has_reparse, _ = path_has_reparse(root, candidate)
    if has_reparse:
        return False
    return resolve_editor_path(
        source_registry,
        representation,
        source_alias=source_alias,
    ).available


def _representation_requires_network(
    representation: Representation,
    *,
    source_registry: SourceRegistry | None = None,
    source_alias: str | None = None,
) -> bool:
    if _representation_is_local(
        representation,
        source_registry=source_registry,
        source_alias=source_alias,
    ):
        return False
    return bool(representation.network_required or representation.external_url is not None)


def _representation_action_status(
    representation: Representation,
    *,
    source_registry: SourceRegistry | None = None,
    source_alias: str | None = None,
) -> str:
    if representation.availability != "available":
        return "missing"
    if _representation_is_local(
        representation,
        source_registry=source_registry,
        source_alias=source_alias,
    ):
        return "available"
    if _representation_requires_network(
        representation,
        source_registry=source_registry,
        source_alias=source_alias,
    ):
        return "network_required"
    return "available"


def _representation_timestamp(representation: Representation) -> float:
    values = [
        value.timestamp()
        for value in (representation.verified_at, representation.updated_at)
        if value is not None
    ]
    return max(values, default=0.0)


def _representation_choice_key(
    candidate: tuple[Representation, Asset, str | None],
    *,
    source_registry: SourceRegistry | None = None,
    source_alias: str | None = None,
) -> tuple[int, int, int, int, float, str]:
    representation, _, _ = candidate
    return (
        0 if representation.availability == "available" else 1,
        0
        if _representation_is_local(
            representation,
            source_registry=source_registry,
            source_alias=source_alias,
        )
        else 1,
        -1
        if representation.attributes.get("unified_guide")
        else _REPRESENTATION_ROLE_ORDER.get(representation.role, 100),
        0
        if not _representation_requires_network(
            representation,
            source_registry=source_registry,
            source_alias=source_alias,
        )
        else 1,
        -_representation_timestamp(representation),
        str(representation.id),
    )


def _representation_missing_reason(representation: Representation) -> str:
    attributes = dict(representation.attributes or {})
    for key in ("missing_reason", "availability_reason", "reason", "error", "detail"):
        value = attributes.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return f"Representation availability is {representation.availability}."


def _no_code_allowlist_problem(path: Path, detail: str) -> ApiProblem:
    return ApiProblem(
        503,
        "无代码审计配置不可用",
        f"Could not safely load {path.name}: {detail}",
        problem_type="urn:research-asset-hub:problem:configuration",
    )


def _load_no_code_titles_by_domain(
    path: Path | None = None,
) -> dict[str, frozenset[str]]:
    """Load the audited exact-title allowlist with strict, fail-closed validation."""

    target = path or _NO_CODE_ALLOWLIST_PATH
    try:
        document = yaml.load(target.read_text(encoding="utf-8"), Loader=yaml.CSafeLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as error:
        raise _no_code_allowlist_problem(target, type(error).__name__) from error
    if document is None:
        document = {}
    if not isinstance(document, dict):
        raise _no_code_allowlist_problem(target, "top level must be a mapping")

    result: dict[str, frozenset[str]] = {}
    for raw_domain, raw_policy in document.items():
        if not isinstance(raw_domain, str) or not raw_domain or raw_domain.strip() != raw_domain:
            raise _no_code_allowlist_problem(
                target,
                "every domain key must be a non-empty exact string",
            )
        if not isinstance(raw_policy, dict):
            raise _no_code_allowlist_problem(
                target,
                f"domain {raw_domain!r} must map to an object",
            )
        raw_titles = raw_policy.get("no_code_titles", [])
        if not isinstance(raw_titles, list) or any(
            not isinstance(title, str) or not title or title.strip() != title
            for title in raw_titles
        ):
            raise _no_code_allowlist_problem(
                target,
                f"domain {raw_domain!r}.no_code_titles must be a list of exact titles",
            )
        result[raw_domain] = frozenset(raw_titles)
    return result


def _paper_is_no_code_allowlisted(
    paper: Asset,
    no_code_titles_by_domain: dict[str, frozenset[str]],
) -> bool:
    if paper.asset_type != "paper":
        return False
    return any(
        paper.title in no_code_titles_by_domain.get(domain, frozenset())
        for domain in paper.domain_slugs or []
    )


def _quick_action(
    *,
    kind: str,
    candidates: Sequence[tuple[Representation, Asset, str | None]],
    missing_owner_asset_id: UUID | None,
    missing_relation_type: str | None,
    source_registry: SourceRegistry | None = None,
    source_aliases: dict[UUID, str] | None = None,
) -> dict[str, Any]:
    def source_alias_for(representation: Representation) -> str | None:
        if source_aliases is None or representation.source_id is None:
            return None
        return source_aliases.get(representation.source_id)

    ordered = sorted(
        candidates,
        key=lambda candidate: _representation_choice_key(
            candidate,
            source_registry=source_registry,
            source_alias=source_alias_for(candidate[0]),
        ),
    )
    if not ordered:
        missing_reasons = {
            "pdf": "来源未提供 PDF。",
            "html": "来源未提供 HTML。",
            "code": (
                "没有已确认且可用的代码仓库。"
                if missing_relation_type == "implemented_by"
                else "来源未提供代码。"
            ),
        }
        return serialize_quick_action(
            kind=kind,
            status="not_provided",
            representation_id=None,
            owner_asset_id=(
                str(missing_owner_asset_id) if missing_owner_asset_id is not None else None
            ),
            relation_type=missing_relation_type,
            reason=missing_reasons[kind],
            alternatives=[],
        )

    representation, owner, relation_type = ordered[0]
    status = _representation_action_status(
        representation,
        source_registry=source_registry,
        source_alias=source_alias_for(representation),
    )
    if status == "missing":
        stored_reason = _representation_missing_reason(representation)
        reason: str | None = (
            stored_reason
            if stored_reason != f"Representation availability is {representation.availability}."
            else (
                "PDF 已登记，但本地文件不存在或来源访问受限。"
                if kind == "pdf"
                else f"{kind.upper()} 已登记，但本地文件不存在。"
            )
        )
    elif status == "network_required":
        reason = "该入口需要联网。"
    else:
        reason = None

    alternatives: list[dict[str, Any]] = []
    available_candidates = [
        candidate
        for candidate in ordered
        if candidate[0].availability == "available" and candidate[0].id != representation.id
    ]
    if status in {"available", "network_required"} and available_candidates:
        for alternative, alternative_owner, alternative_relation_type in available_candidates:
            alternatives.append(
                {
                    "representation_id": str(alternative.id),
                    "owner_asset_id": str(alternative_owner.id),
                    "label": (alternative_owner.title if kind == "code" else alternative.role),
                    "role": alternative.role,
                    "status": _representation_action_status(
                        alternative,
                        source_registry=source_registry,
                        source_alias=source_alias_for(alternative),
                    ),
                    "relation_type": alternative_relation_type,
                }
            )
    return serialize_quick_action(
        kind=kind,
        status=status,
        representation_id=str(representation.id),
        owner_asset_id=str(owner.id),
        relation_type=relation_type,
        reason=reason,
        alternatives=alternatives,
    )


def _research_unit_quick_actions(
    primary_asset: Asset,
    related_repositories: list[Asset],
    representations: dict[UUID, list[Representation]],
    *,
    source_registry: SourceRegistry | None = None,
    source_aliases: dict[UUID, str] | None = None,
    no_code_titles_by_domain: dict[str, frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for kind in ("pdf", "html"):
        candidates = [
            (representation, primary_asset, None)
            for representation in representations.get(primary_asset.id, [])
            if _representation_matches_action(representation, kind)
        ]
        actions.append(
            _quick_action(
                kind=kind,
                candidates=candidates,
                missing_owner_asset_id=primary_asset.id,
                missing_relation_type=None,
                source_registry=source_registry,
                source_aliases=source_aliases,
            )
        )

    if primary_asset.asset_type == "repository":
        code_owners = [primary_asset]
        relation_type = None
    else:
        code_owners = related_repositories
        relation_type = "implemented_by"
    code_candidates = [
        (representation, owner, relation_type)
        for owner in code_owners
        for representation in representations.get(owner.id, [])
        if _representation_matches_action(representation, "code")
    ]

    def source_alias_for(representation: Representation) -> str | None:
        if source_aliases is None or representation.source_id is None:
            return None
        return source_aliases.get(representation.source_id)

    has_available_local_code = any(
        _representation_is_serving_local_code(
            representation,
            source_registry=source_registry,
            source_alias=source_alias_for(representation),
        )
        for representation, _, _ in code_candidates
    )
    suppress_network_only_code = (
        primary_asset.asset_type == "paper"
        and _paper_is_no_code_allowlisted(
            primary_asset,
            no_code_titles_by_domain or {},
        )
        and not has_available_local_code
    )
    if suppress_network_only_code:
        actions.append(
            serialize_quick_action(
                kind="code",
                status="not_provided",
                representation_id=None,
                owner_asset_id=str(primary_asset.id),
                relation_type="implemented_by",
                reason=_NO_CODE_AUDIT_REASON,
                alternatives=[],
            )
        )
    else:
        actions.append(
            _quick_action(
                kind="code",
                candidates=code_candidates,
                missing_owner_asset_id=(
                    code_owners[0].id
                    if code_owners
                    else primary_asset.id
                    if primary_asset.asset_type == "paper"
                    else None
                ),
                missing_relation_type=(
                    "implemented_by" if primary_asset.asset_type == "paper" else relation_type
                ),
                source_registry=source_registry,
                source_aliases=source_aliases,
            )
        )
    return actions


def _asset_search_score(asset_id: UUID, search_hits: dict[UUID, Any]) -> float:
    hit = search_hits.get(asset_id)
    return round(float(hit.score), 6) if hit is not None else 1.0


def _asset_search_snippet(asset: Asset, search_hits: dict[UUID, Any], query: str) -> str | None:
    hit = search_hits.get(asset.id)
    if hit is not None:
        return " ".join(hit.document.text.split())[:360]
    if not query.strip():
        return None
    fallback = asset.abstract or asset.title
    return " ".join(fallback.split())[:360] if fallback else None


def _serialize_matched_asset(
    asset: Asset,
    *,
    search_hits: dict[UUID, Any],
    query: str,
    states: dict[UUID, ReadingState],
    summaries: dict[UUID, str],
) -> dict[str, Any]:
    hit = search_hits.get(asset.id)
    item = serialize_catalog_asset(
        asset,
        reading_state=states.get(asset.id),
        annotation_summary=summaries.get(asset.id),
        score=_asset_search_score(asset.id, search_hits),
    )
    for field in ("summary", "abstract", "attributes"):
        item.pop(field, None)
    snippet = _asset_search_snippet(asset, search_hits, query)
    if snippet is not None:
        item["snippet"] = snippet
    if hit is not None:
        item.update(
            {
                "document_kind": hit.document.kind,
                "anchor": hit.document.metadata.get("anchor"),
                "representation_id": hit.document.metadata.get("representation_id"),
                "search_source": hit.source,
                "exact_match": hit.exact_match,
            }
        )
    return item


def _build_research_units(
    session: Session,
    *,
    catalog_assets: list[Asset],
    primary_ids: set[UUID],
    matched_assets: list[Asset],
    search_hits: dict[UUID, Any],
    query: str,
    states: dict[UUID, ReadingState],
    summaries: dict[UUID, str],
    representations: dict[UUID, list[Representation]],
    include_independent_repositories: bool,
    source_registry: SourceRegistry | None = None,
) -> tuple[list[dict[str, Any]], list[Asset]]:
    # Catalog SQL has already filtered, grouped and paged primary_ids. Related
    # assets are context only: never re-filter the page or promote that context.
    configured_policy = (
        source_registry.config_path.parent / "check-unit-actions-allowlist.yaml"
        if source_registry is not None and source_registry.config_path is not None
        else None
    )
    no_code_titles_by_domain = _load_no_code_titles_by_domain(
        configured_policy if configured_policy is not None and configured_policy.is_file() else None
    )
    assets_by_id = {asset.id: asset for asset in catalog_assets}
    source_aliases: dict[UUID, str] = {}
    if source_registry is not None:
        source_aliases = {
            source_id: alias
            for source_id, alias in session.execute(select(Source.id, Source.alias))
        }
    paper_repository_ids: dict[UUID, set[UUID]] = defaultdict(set)
    relations = session.scalars(
        select(Relation)
        .where(
            Relation.relation_type == "implemented_by",
            Relation.confirmation_status == "confirmed",
            Relation.deleted_at.is_(None),
            Relation.subject_asset_id.in_(assets_by_id),
        )
        .order_by(Relation.subject_asset_id, Relation.object_asset_id, Relation.id)
    ).all()
    for relation in relations:
        paper = assets_by_id.get(relation.subject_asset_id)
        repository = assets_by_id.get(relation.object_asset_id)
        if (
            paper is None
            or paper.asset_type != "paper"
            or repository is None
            or repository.asset_type != "repository"
        ):
            continue
        paper_repository_ids[paper.id].add(repository.id)

    matched_ids = {asset.id for asset in matched_assets}
    matched_order = {asset.id: index for index, asset in enumerate(matched_assets)}
    browse = not query.strip()
    sortable_units: list[tuple[float, int, float, str, dict[str, Any], Asset]] = []
    represented_repository_ids: set[UUID] = set()

    for paper in catalog_assets:
        if paper.asset_type != "paper" or paper.id not in primary_ids:
            continue
        related_repositories = sorted(
            (
                assets_by_id[repository_id]
                for repository_id in paper_repository_ids.get(paper.id, set())
            ),
            key=lambda item: (item.title.casefold(), str(item.id)),
        )
        member_ids = {paper.id, *(repository.id for repository in related_repositories)}
        if not browse and not member_ids.intersection(matched_ids):
            continue
        if browse:
            unit_matches = [paper]
            match_source = "browse"
        else:
            unit_matches = [
                item for item in [paper, *related_repositories] if item.id in matched_ids
            ]
            paper_matched = paper.id in matched_ids
            repository_matched = any(
                repository.id in matched_ids for repository in related_repositories
            )
            if paper_matched and repository_matched:
                match_source = "paper_and_related_repository"
            elif repository_matched:
                match_source = "related_repository"
            else:
                match_source = "paper"
        best_match = max(
            unit_matches,
            key=lambda item: (
                _asset_search_score(item.id, search_hits),
                1 if item.id == paper.id else 0,
                -matched_order.get(item.id, len(matched_order)),
            ),
        )
        score = _asset_search_score(best_match.id, search_hits)
        payload = serialize_research_unit(
            unit_id=str(paper.id),
            unit_kind="paper",
            primary_asset=serialize_catalog_asset(
                paper,
                reading_state=states.get(paper.id),
                annotation_summary=summaries.get(paper.id),
            ),
            related_repositories=[
                serialize_catalog_asset(
                    repository,
                    reading_state=states.get(repository.id),
                    annotation_summary=summaries.get(repository.id),
                )
                for repository in related_repositories
            ],
            quick_actions=_research_unit_quick_actions(
                paper,
                related_repositories,
                representations,
                source_registry=source_registry,
                source_aliases=source_aliases,
                no_code_titles_by_domain=no_code_titles_by_domain,
            ),
            matched_assets=[
                _serialize_matched_asset(
                    item,
                    search_hits=search_hits,
                    query=query,
                    states=states,
                    summaries=summaries,
                )
                for item in unit_matches
            ],
            score=score,
            snippet=_asset_search_snippet(best_match, search_hits, query),
            match_source=match_source,
        )
        first_match = min(
            (matched_order.get(item.id, len(matched_order)) for item in unit_matches),
            default=len(matched_order),
        )
        sortable_units.append(
            (
                -score,
                first_match,
                -(paper.updated_at.timestamp() if paper.updated_at is not None else 0.0),
                str(paper.id),
                payload,
                paper,
            )
        )
        represented_repository_ids.update(repository.id for repository in related_repositories)

    if include_independent_repositories:
        for repository in catalog_assets:
            if (
                repository.asset_type != "repository"
                or repository.id not in primary_ids
                or repository.id in represented_repository_ids
                or (not browse and repository.id not in matched_ids)
            ):
                continue
            score = _asset_search_score(repository.id, search_hits)
            payload = serialize_research_unit(
                unit_id=str(repository.id),
                unit_kind="repository",
                primary_asset=serialize_catalog_asset(
                    repository,
                    reading_state=states.get(repository.id),
                    annotation_summary=summaries.get(repository.id),
                ),
                related_repositories=[],
                quick_actions=_research_unit_quick_actions(
                    repository,
                    [],
                    representations,
                    source_registry=source_registry,
                    source_aliases=source_aliases,
                    no_code_titles_by_domain=no_code_titles_by_domain,
                ),
                matched_assets=[
                    _serialize_matched_asset(
                        repository,
                        search_hits=search_hits,
                        query=query,
                        states=states,
                        summaries=summaries,
                    )
                ],
                score=score,
                snippet=_asset_search_snippet(repository, search_hits, query),
                match_source="browse" if browse else "repository",
            )
            sortable_units.append(
                (
                    -score,
                    matched_order.get(repository.id, len(matched_order)),
                    -(
                        repository.updated_at.timestamp()
                        if repository.updated_at is not None
                        else 0.0
                    ),
                    str(repository.id),
                    payload,
                    repository,
                )
            )

    sortable_units.sort(key=lambda item: item[:4])
    return (
        [item[4] for item in sortable_units],
        [item[5] for item in sortable_units],
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ApiProblem(
            503,
            "配置不可用",
            f"Could not read {path.name}: {type(error).__name__}.",
            problem_type="urn:research-asset-hub:problem:configuration",
        ) from error
    return value if isinstance(value, dict) else {}


def _sync_etag(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _serialize_backup_run(run: BackupRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "status": run.status,
        "backup_kind": run.backup_kind,
        "destination_label": run.destination_label,
        "manifest_path": run.manifest_path,
        "database_digest": run.database_digest,
        "export_digest": run.export_digest,
        "local_copy_only": run.local_copy_only,
        "counters": run.counters,
        "error": run.error,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "verified_at": run.verified_at.isoformat() if run.verified_at else None,
        "restored_at": run.restored_at.isoformat() if run.restored_at else None,
        "row_version": run.row_version,
    }


def _serialize_relation(
    relation: Relation,
    *,
    evidence_count: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": str(relation.id),
        "subject_asset_id": str(relation.subject_asset_id),
        "object_asset_id": str(relation.object_asset_id),
        "relation_type": relation.relation_type,
        "source_key": relation.source_key,
        "confidence": relation.confidence,
        "confirmation_status": relation.confirmation_status,
        "attributes": dict(relation.attributes or {}),
        "created_at": relation.created_at.isoformat(),
        "updated_at": relation.updated_at.isoformat(),
        "deleted_at": (
            relation.deleted_at.isoformat() if relation.deleted_at is not None else None
        ),
        "row_version": relation.row_version,
    }
    if evidence_count is not None:
        result["evidence_count"] = evidence_count
    return result


def _serialize_relation_evidence(item: RelationEvidenceModel) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "relation_id": str(item.relation_id),
        "source_record_version_id": (
            str(item.source_record_version_id)
            if item.source_record_version_id is not None
            else None
        ),
        "provider": item.provider,
        "source_key": item.source_key,
        "evidence": dict(item.evidence or {}),
        "evidence_hash": item.evidence_hash,
        "confidence": item.confidence,
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at is not None else None,
        "created_at": item.created_at.isoformat(),
    }


def _serialize_override(item: FieldResolution) -> dict[str, Any]:
    cleared = item.resolution_kind == FieldResolutionKind.CLEARED.value
    return {
        "id": str(item.id),
        "entity_type": item.entity_type,
        "entity_id": str(item.entity_id),
        "field_path": item.field_path,
        "resolution_kind": item.resolution_kind,
        "override_value": item.override_value,
        "reason": item.reason,
        "resolved_by": item.resolved_by,
        "active": not cleared,
        "deleted_at": item.updated_at.isoformat() if cleared else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "row_version": item.row_version,
    }


def _serialize_index_state(item: IndexState) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "index_name": item.index_name,
        "index_kind": item.index_kind,
        "entity_type": item.entity_type,
        "entity_id": str(item.entity_id),
        "content_digest": item.content_digest,
        "status": item.status,
        "provider": item.provider,
        "model": item.model,
        "dimension": item.dimension,
        "version": item.version,
        "error": item.error,
        "indexed_at": item.indexed_at.isoformat() if item.indexed_at is not None else None,
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "row_version": item.row_version,
    }


def _job_representation_id(job: Job) -> UUID | None:
    raw_value = (job.parameters or {}).get("representation_id")
    if raw_value is None:
        return None
    try:
        return UUID(str(raw_value))
    except (TypeError, ValueError):
        return None


def _serialize_extraction(
    job: Job,
    *,
    representation: Representation | None,
    activity: Activity | None,
    chunk_count: int,
) -> dict[str, Any]:
    representation_id = _job_representation_id(job)
    activity_result = dict(activity.result or {}) if activity is not None else {}
    return {
        "id": str(job.id),
        "job_id": str(job.id),
        "representation_id": (str(representation_id) if representation_id is not None else None),
        "asset_id": (str(representation.asset_id) if representation is not None else None),
        "representation_type": (
            representation.representation_type if representation is not None else None
        ),
        "availability": (representation.availability if representation is not None else None),
        "status": job.status,
        "extraction_status": activity_result.get("status"),
        "activity_id": str(activity.id) if activity is not None else None,
        "activity_status": activity.status if activity is not None else None,
        "progress": job.progress,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "chunk_count": int(activity_result.get("chunk_count") or chunk_count),
        "file_count": activity_result.get("file_count"),
        "ocr_states": list(activity_result.get("ocr_states") or []),
        "warnings": list(activity_result.get("warnings") or []),
        "error": job.error or activity_result.get("error"),
        "scheduled_at": (job.scheduled_at.isoformat() if job.scheduled_at is not None else None),
        "started_at": job.started_at.isoformat() if job.started_at is not None else None,
        "finished_at": (job.finished_at.isoformat() if job.finished_at is not None else None),
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "row_version": job.row_version,
    }


def _live_relation(
    session: Session,
    relation_id: UUID,
    *,
    include_deleted: bool = False,
) -> Relation:
    relation = session.get(Relation, relation_id)
    if relation is None or (relation.deleted_at is not None and not include_deleted):
        raise not_found("Relation", relation_id)
    return relation


def _live_override(
    session: Session,
    override_id: UUID,
    *,
    include_deleted: bool = False,
) -> FieldResolution:
    item = session.get(FieldResolution, override_id)
    is_override = item is not None and item.resolution_kind in {
        FieldResolutionKind.OVERRIDE.value,
        FieldResolutionKind.CLEARED.value,
    }
    if (
        item is None
        or item.entity_type != "asset"
        or not is_override
        or (item.resolution_kind == FieldResolutionKind.CLEARED.value and not include_deleted)
    ):
        raise not_found("Override", override_id)
    return item


def _override_asset_field(field_path: str) -> str:
    field_name = field_path.removeprefix("/")
    if "/" in field_name or field_name not in _OVERRIDABLE_ASSET_FIELDS:
        raise ApiProblem(
            422,
            "覆盖字段不受支持",
            (
                "Only approved top-level asset metadata fields may be overridden; "
                "system IDs, versions, timestamps and deletion state are immutable."
            ),
            problem_type="urn:research-asset-hub:problem:validation",
            errors=[
                {
                    "location": ["body", "field_path"],
                    "message": f"{field_path!r} is not an overridable asset field.",
                    "code": "unsupported_override_path",
                }
            ],
        )
    return field_name


def _validated_override_value(field_name: str, value: Any) -> Any:
    try:
        patch = AssetPatch.model_validate({field_name: value})
    except ValidationError as error:
        raise ApiProblem(
            422,
            "覆盖值无效",
            "The override value does not satisfy the canonical asset field contract.",
            problem_type="urn:research-asset-hub:problem:validation",
            errors=[
                {
                    "location": ["body", "override_value", *item["loc"]],
                    "message": item["msg"],
                    "code": item["type"],
                }
                for item in error.errors()
            ],
        ) from error
    return patch.model_dump(exclude_unset=True)[field_name]


def _override_base_claim_key(item: FieldResolution) -> str:
    return f"override-base:{item.id}"


def _override_base_claim(
    session: Session,
    item: FieldResolution,
) -> FieldClaim | None:
    return session.scalar(
        select(FieldClaim).where(
            FieldClaim.entity_type == "asset",
            FieldClaim.entity_id == item.entity_id,
            FieldClaim.field_path == item.field_path,
            FieldClaim.source_key == _override_base_claim_key(item),
        )
    )


def _ensure_override_base_claim(
    session: Session,
    item: FieldResolution,
    asset: Asset,
) -> FieldClaim:
    existing = _override_base_claim(session, item)
    if existing is not None:
        return existing
    field_name = _override_asset_field(item.field_path)
    base_value = getattr(asset, field_name)
    claim = FieldClaim(
        entity_type="asset",
        entity_id=asset.id,
        field_path=item.field_path,
        value_json=base_value,
        value_digest=stable_hash(base_value),
        provider="local-api",
        source_pointer=item.field_path,
        source_key=_override_base_claim_key(item),
        authority=1000,
        confidence=1.0,
        status=FieldClaimStatus.ACTIVE.value,
    )
    session.add(claim)
    session.flush()
    return claim


def _current_source_claim(
    session: Session,
    item: FieldResolution,
) -> FieldClaim | None:
    source_claim = session.scalar(
        select(FieldClaim)
        .where(
            FieldClaim.entity_type == "asset",
            FieldClaim.entity_id == item.entity_id,
            FieldClaim.field_path == item.field_path,
            FieldClaim.status == FieldClaimStatus.ACTIVE.value,
            FieldClaim.source_key != _override_base_claim_key(item),
        )
        .order_by(
            FieldClaim.observed_at.desc(),
            FieldClaim.authority.desc(),
            FieldClaim.created_at.desc(),
            FieldClaim.id.desc(),
        )
    )
    return source_claim or _override_base_claim(session, item)


def _project_override_value(
    asset: Asset,
    *,
    field_path: str,
    value: Any,
) -> None:
    field_name = _override_asset_field(field_path)
    setattr(asset, field_name, _validated_override_value(field_name, value))


def _record_asset_projection(
    session: Session,
    *,
    asset: Asset,
    before: dict[str, Any],
    request: Request,
    event_type: str,
) -> None:
    after = serialize_asset(asset)
    if before == after:
        return
    record_mutation(
        session,
        entity_type="asset",
        entity_id=asset.id,
        action=AuditAction.UPDATE,
        before=before,
        after=after,
        request_id=_request_id(request),
        event_type=event_type,
    )


def _relation_duplicate(
    session: Session,
    *,
    subject_asset_id: UUID,
    relation_type: str,
    object_asset_id: UUID,
    exclude_id: UUID | None = None,
) -> Relation | None:
    statement = select(Relation).where(
        Relation.subject_asset_id == subject_asset_id,
        Relation.relation_type == relation_type,
        Relation.object_asset_id == object_asset_id,
    )
    if exclude_id is not None:
        statement = statement.where(Relation.id != exclude_id)
    return session.scalar(statement.order_by(Relation.created_at))


def _local_code_context(
    request: Request,
    representation_id: UUID,
) -> tuple[Representation, Path]:
    with _database(request).session() as session:
        representation = session.get(Representation, representation_id)
        if representation is None or representation.deleted_at is not None:
            raise not_found("Representation", representation_id)
        _live_asset(session, representation.asset_id)
        try:
            assert_representation_active(session, representation)
        except RetiredRepresentationError as error:
            raise _retired_representation_problem(error) from error
        if representation.representation_type not in CODE_REPRESENTATION_TYPES:
            raise conflict("该表示不是代码项目。")
        if representation.availability != "available":
            raise conflict("该代码表示当前不可用。")
        source = (
            session.get(Source, representation.source_id)
            if representation.source_id is not None
            else None
        )
        if source is None or not source.enabled:
            raise conflict("该代码来源当前不可用。")
        source_alias = source.alias
        try:
            root = local_code_repository(
                _source_registry(request),
                representation,
                source_alias=source_alias,
            )
        except CodeBrowserUnavailable as error:
            raise conflict(str(error)) from error
        return representation, root


def _domain_overview_warning(representation: Representation) -> str | None:
    if representation.availability == "available":
        if representation.external_url and not representation.path_ref:
            return "The overview is external-only and uses its original presentation."
        return None
    if representation.availability == "missing":
        return _representation_missing_reason(representation)
    return f"Overview representation is {representation.availability}."


def _domain_overviews(
    session: Session,
    portal_assets: list[Asset],
) -> dict[str, dict[str, Any]]:
    portal_ids = [asset.id for asset in portal_assets]
    representations: dict[UUID, list[Representation]] = defaultdict(list)
    if portal_ids:
        for representation in session.scalars(
            select(Representation)
            .where(
                Representation.asset_id.in_(portal_ids),
                Representation.deleted_at.is_(None),
                active_representation_clause(),
                Representation.representation_type.in_(_HTML_REPRESENTATION_TYPES),
            )
            .order_by(Representation.asset_id, Representation.id)
        ):
            representations[representation.asset_id].append(representation)

    result: dict[str, dict[str, Any]] = {}
    domain_slugs = {slug for asset in portal_assets for slug in (asset.domain_slugs or [])}
    for slug in sorted(domain_slugs):
        matching_assets = sorted(
            (asset for asset in portal_assets if slug in (asset.domain_slugs or [])),
            key=lambda asset: (
                -(asset.updated_at.timestamp() if asset.updated_at is not None else 0.0),
                str(asset.id),
            ),
        )
        candidates = sorted(
            (
                (representation, asset, None)
                for asset in matching_assets
                for representation in representations.get(asset.id, [])
            ),
            key=_representation_choice_key,
        )
        if not candidates:
            asset = matching_assets[0]
            result[slug] = serialize_domain_overview(
                asset_id=str(asset.id),
                representation_id=None,
                status="unavailable",
                default_presentation="original",
                last_verified_at=None,
                warning="No HTML representation is registered for this domain overview.",
            )
            continue
        representation, asset, _ = candidates[0]
        if representation.availability == "available":
            status = (
                "network_required"
                if _representation_requires_network(representation)
                else "available"
            )
        elif representation.availability == "missing":
            status = "missing"
        else:
            status = "unavailable"
        default_presentation = (
            "hub"
            if representation.availability == "available" and representation.path_ref
            else "original"
        )
        result[slug] = serialize_domain_overview(
            asset_id=str(asset.id),
            representation_id=str(representation.id),
            status=status,
            default_presentation=default_presentation,
            last_verified_at=representation.verified_at,
            warning=_domain_overview_warning(representation),
        )
    return result


def _comparison_columns(assets: list[Asset]) -> list[dict[str, Any]]:
    fields: list[tuple[str, str, Any]] = [
        ("year", "年份", lambda item: item.year),
        ("authors", "作者", lambda item: item.authors),
        ("venue", "发表场所", lambda item: item.venue),
        ("domains", "领域", presentation_domains),
        ("summary", "摘要", lambda item: item.summary or item.abstract),
        ("tags", "标签", lambda item: item.tags),
    ]
    return [
        {
            "key": key,
            "label": label,
            "values": {str(item.id): getter(item) for item in assets},
        }
        for key, label, getter in fields
    ]


# 子路由模块在 apps.api.routes/__init__.py 聚合导入；导入该包即向共享
# router 注册全部路由（必须位于所有辅助定义之后）。
from apps.api import routes  # noqa: E402,F401
