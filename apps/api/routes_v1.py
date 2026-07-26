from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
import yaml
from fastapi import APIRouter, Body, Header, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from apps.api.config import PROJECT_ROOT, ApiSettings
from apps.api.contracts import (
    AnnotationCreate,
    AnnotationPatch,
    AssetCreate,
    AssetPatch,
    ChatRequest,
    ComparisonCreate,
    ConflictResolutionRequest,
    OverrideCreate,
    OverridePatch,
    ReadingStateUpdate,
    RelationCreate,
    RelationEvidenceCreate,
    RelationPatch,
    SyncApplyRequest,
)
from apps.api.database import DatabaseManager
from apps.api.events import record_mutation, replay_idempotent, stable_hash
from apps.api.pagination import decode_cursor, encode_cursor
from apps.api.problem import ApiProblem, conflict, not_found
from apps.api.rag_runtime import answer_with_rag
from apps.api.search_runtime import SearchRuntime
from apps.api.serialization import (
    serialize_annotation,
    serialize_annotation_revision,
    serialize_asset,
    serialize_comparison,
    serialize_domain_overview,
    serialize_job,
    serialize_quick_action,
    serialize_representation,
    serialize_research_unit,
    serialize_sync_conflict,
)
from apps.asset_gateway.security import create_gateway_token
from packages.backup import BackupManager, create_platform_backup
from packages.domain.enums import (
    AuditAction,
    BackupStatus,
    ConflictResolution,
    FieldClaimStatus,
    FieldResolutionKind,
    IntegrationStatus,
    JobStatus,
    SyncConflictStatus,
    SyncStatus,
)
from packages.domain.models import (
    Activity,
    Annotation,
    AnnotationRevision,
    Asset,
    AssetRedirect,
    BackupRun,
    ChatMessage,
    ChatSession,
    Citation,
    DocumentChunk,
    FieldClaim,
    FieldResolution,
    IndexState,
    Integration,
    Job,
    ReadingState,
    Relation,
    RelationEvidenceModel,
    Representation,
    SavedView,
    SourceRecordVersion,
    SyncConflict,
    SyncRun,
)
from packages.integrations import (
    ZoteroLocalClient,
    obsidian_sync_etag,
    preview_obsidian_assets,
    sync_obsidian_assets,
)

router = APIRouter(prefix="/api/v1")
PAGE_SIZE = 30
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


def _is_domain_overview_asset(asset: Asset) -> bool:
    return bool(
        asset.asset_type == "collection"
        and _is_system_hidden_asset(asset)
        and str((asset.attributes or {}).get("kind") or "") == "domain-overview"
    )


_ACTION_REPRESENTATION_TYPES: dict[str, frozenset[str]] = {
    "pdf": frozenset({"pdf"}),
    "html": frozenset({"html", "html_document"}),
    "guide": frozenset({"guide", "analysis", "markdown"}),
    "code": frozenset({"source_code", "repository", "notebook"}),
}
_HTML_REPRESENTATION_TYPES = frozenset(
    {"html", "guide", "analysis", "html_document"}
)
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
            representation.role != "guide"
            and representation.representation_type in _ACTION_REPRESENTATION_TYPES["html"]
        )
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


def _representation_is_local(representation: Representation) -> bool:
    return bool(representation.path_ref) and representation.materialization_kind != "url"


def _representation_requires_network(representation: Representation) -> bool:
    return bool(
        representation.network_required
        or (
            not _representation_is_local(representation) and representation.external_url is not None
        )
    )


def _representation_action_status(representation: Representation) -> str:
    if representation.availability != "available":
        return "missing"
    if _representation_requires_network(representation):
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
) -> tuple[int, int, int, int, float, str]:
    representation, _, _ = candidate
    return (
        0 if representation.availability == "available" else 1,
        0 if _representation_is_local(representation) else 1,
        _REPRESENTATION_ROLE_ORDER.get(representation.role, 100),
        0 if not _representation_requires_network(representation) else 1,
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


def _quick_action(
    *,
    kind: str,
    candidates: Sequence[tuple[Representation, Asset, str | None]],
    missing_owner_asset_id: UUID | None,
    missing_relation_type: str | None,
) -> dict[str, Any]:
    ordered = sorted(candidates, key=_representation_choice_key)
    if not ordered:
        missing_reasons = {
            "pdf": "来源未提供 PDF。",
            "html": "来源未提供 HTML。",
            "guide": "来源未提供导读。",
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
    status = _representation_action_status(representation)
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
                    "status": _representation_action_status(alternative),
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
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for kind in ("pdf", "html", "guide"):
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
    fallback = asset.summary or asset.abstract or asset.content or asset.title
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
    item = serialize_asset(
        asset,
        reading_state=states.get(asset.id),
        annotation_summary=summaries.get(asset.id),
        score=_asset_search_score(asset.id, search_hits),
    )
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


def _asset_matches_unit_filters(
    asset: Asset,
    *,
    related_repositories: list[Asset],
    representations: dict[UUID, list[Representation]],
    states: dict[UUID, ReadingState],
    annotation_assets: set[UUID],
    domain: str | None,
    status: str | None,
    author: str | None,
    year_from: int | None,
    year_to: int | None,
    tags: list[str] | None,
    has_pdf: bool | None,
    has_html: bool | None,
    has_code: bool | None,
    has_guide: bool | None,
    has_annotation: bool | None,
) -> bool:
    if domain and not any(
        domain in (member.domain_slugs or []) for member in (asset, *related_repositories)
    ):
        return False
    if author and not any(author.casefold() in item.casefold() for item in asset.authors or []):
        return False
    if year_from is not None and (asset.year is None or asset.year < year_from):
        return False
    if year_to is not None and (asset.year is None or asset.year > year_to):
        return False
    if tags and not set(tags).intersection(asset.tags or []):
        return False
    reading_state = states.get(asset.id)
    reading_status = reading_state.status if reading_state is not None else "unread"
    if status and reading_status != status:
        return False
    if has_annotation is not None and ((asset.id in annotation_assets) != has_annotation):
        return False

    asset_representations = representations.get(asset.id, [])
    availability_flags = {
        "pdf": _has_available_action(asset_representations, "pdf"),
        "html": _has_available_action(asset_representations, "html"),
        "guide": _has_available_action(asset_representations, "guide"),
        "code": (
            any(
                _has_available_action(
                    representations.get(repository.id, []),
                    "code",
                )
                for repository in related_repositories
            )
            if asset.asset_type == "paper"
            else _has_available_action(asset_representations, "code")
        ),
    }
    requested_flags = {
        "pdf": has_pdf,
        "html": has_html,
        "code": has_code,
        "guide": has_guide,
    }
    return all(
        requested is None or availability_flags[kind] == requested
        for kind, requested in requested_flags.items()
    )


def _asset_facets(
    assets: list[Asset],
    states: dict[UUID, ReadingState],
) -> dict[str, dict[str, int]]:
    return {
        "domains": dict(
            Counter(
                domain_slug for asset in assets for domain_slug in asset.domain_slugs or []
            ).most_common()
        ),
        "years": dict(
            sorted(
                Counter(str(asset.year) for asset in assets if asset.year is not None).items(),
                reverse=True,
            )
        ),
        "types": dict(Counter(asset.asset_type for asset in assets).most_common()),
        "reading_status": dict(
            Counter(
                states[asset.id].status if asset.id in states else "unread" for asset in assets
            ).most_common()
        ),
        "tags": dict(
            Counter(tag_value for asset in assets for tag_value in asset.tags or []).most_common(50)
        ),
    }


def _build_research_units(
    session: Session,
    *,
    catalog_assets: list[Asset],
    matched_assets: list[Asset],
    search_hits: dict[UUID, Any],
    query: str,
    states: dict[UUID, ReadingState],
    summaries: dict[UUID, str],
    annotation_assets: set[UUID],
    representations: dict[UUID, list[Representation]],
    include_independent_repositories: bool,
    domain: str | None,
    status: str | None,
    author: str | None,
    year_from: int | None,
    year_to: int | None,
    tags: list[str] | None,
    has_pdf: bool | None,
    has_html: bool | None,
    has_code: bool | None,
    has_guide: bool | None,
    has_annotation: bool | None,
) -> tuple[list[dict[str, Any]], list[Asset]]:
    assets_by_id = {asset.id: asset for asset in catalog_assets}
    paper_repository_ids: dict[UUID, set[UUID]] = defaultdict(set)
    relations = session.scalars(
        select(Relation)
        .where(
            Relation.relation_type == "implemented_by",
            Relation.confirmation_status == "confirmed",
            Relation.deleted_at.is_(None),
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
        if paper.asset_type != "paper":
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
        if not _asset_matches_unit_filters(
            paper,
            related_repositories=related_repositories,
            representations=representations,
            states=states,
            annotation_assets=annotation_assets,
            domain=domain,
            status=status,
            author=author,
            year_from=year_from,
            year_to=year_to,
            tags=tags,
            has_pdf=has_pdf,
            has_html=has_html,
            has_code=has_code,
            has_guide=has_guide,
            has_annotation=has_annotation,
        ):
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
            primary_asset=serialize_asset(
                paper,
                reading_state=states.get(paper.id),
                annotation_summary=summaries.get(paper.id),
            ),
            related_repositories=[
                serialize_asset(
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
                or repository.id in represented_repository_ids
                or (not browse and repository.id not in matched_ids)
            ):
                continue
            if not _asset_matches_unit_filters(
                repository,
                related_repositories=[],
                representations=representations,
                states=states,
                annotation_assets=annotation_assets,
                domain=domain,
                status=status,
                author=author,
                year_from=year_from,
                year_to=year_to,
                tags=tags,
                has_pdf=has_pdf,
                has_html=has_html,
                has_code=has_code,
                has_guide=has_guide,
                has_annotation=has_annotation,
            ):
                continue
            score = _asset_search_score(repository.id, search_hits)
            payload = serialize_research_unit(
                unit_id=str(repository.id),
                unit_kind="repository",
                primary_asset=serialize_asset(
                    repository,
                    reading_state=states.get(repository.id),
                    annotation_summary=summaries.get(repository.id),
                ),
                related_repositories=[],
                quick_actions=_research_unit_quick_actions(repository, [], representations),
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


def _domain_catalog(settings: ApiSettings) -> list[dict[str, Any]]:
    document = _load_yaml(settings.taxonomy_config_path)
    result: list[dict[str, Any]] = []
    for item in document.get("domains", []):
        if not isinstance(item, dict):
            continue
        slug = str(item.get("id", "")).strip()
        if slug:
            result.append(
                {
                    "slug": slug,
                    "label": str(item.get("label") or slug),
                    "aliases": list(item.get("aliases", [])),
                    "description": (
                        str(item["description"]) if item.get("description") else None
                    ),
                    "tags": list(item.get("tags", [])),
                }
            )
    return result


@router.get("/dashboard")
def dashboard(request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        assets = [
            asset
            for asset in session.scalars(
                select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.updated_at.desc())
            ).all()
            if not _is_system_hidden_asset(asset)
        ]
        states, summaries = _asset_context(session, assets)
        reading_counts = {"unread": 0, "reading": 0, "read": 0}
        for asset in assets:
            reading_state = states.get(asset.id)
            status = reading_state.status if reading_state is not None else "unread"
            reading_counts[status] = reading_counts.get(status, 0) + 1
        domains = _domains_with_counts(session, _settings(request), assets)
        relation_count = (
            session.scalar(select(func.count(Relation.id)).where(Relation.deleted_at.is_(None)))
            or 0
        )
        annotation_count = (
            session.scalar(select(func.count(Annotation.id)).where(Annotation.deleted_at.is_(None)))
            or 0
        )
        recent = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in assets[:8]
        ]
        continue_reading = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in assets
            if states.get(item.id) is not None and states[item.id].status == "reading"
        ][:8]
        indexed_assets = (
            session.scalar(
                select(func.count(func.distinct(IndexState.entity_id))).where(
                    IndexState.entity_type == "asset",
                    IndexState.status == "ready",
                )
            )
            or 0
        )
        stats = {
            "assets": len(assets),
            "papers": sum(item.asset_type == "paper" for item in assets),
            **reading_counts,
            "domains": len(domains),
            "relations": relation_count,
            "annotations": annotation_count,
        }
        return {
            "stats": stats,
            "total_assets": stats["assets"],
            "total_papers": stats["papers"],
            "unread_count": stats["unread"],
            "reading_count": stats["reading"],
            "read_count": stats["read"],
            "domain_count": stats["domains"],
            "relation_count": stats["relations"],
            "annotation_count": stats["annotations"],
            "recent_assets": recent,
            "continue_reading": continue_reading,
            "domains": domains,
            "index_state": {
                "status": "ready" if indexed_assets else "building",
                "indexed_assets": indexed_assets,
                "pending_assets": max(0, len(assets) - indexed_assets),
            },
        }


@router.get("/search")
def search_assets(
    request: Request,
    q: str = "",
    mode: str = Query("hybrid", pattern="^(exact|hybrid|semantic)$"),
    group_by: str = Query("none", pattern="^(paper|none)$"),
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    author: str | None = None,
    asset_type: str | None = Query(None, alias="type"),
    year_from: int | None = Query(None, ge=1450, le=2200),
    year_to: int | None = Query(None, ge=1450, le=2200),
    tag: Annotated[list[str] | None, Query()] = None,
    has_pdf: bool | None = None,
    has_html: bool | None = None,
    has_code: bool | None = None,
    has_guide: bool | None = None,
    has_annotation: bool | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    paper_grouping = group_by == "paper" and asset_type in {None, "all", "paper"}
    filters: dict[str, Any] = {
        "domain": domain,
        "status": status,
        "type": asset_type if asset_type != "all" else None,
        "year_from": year_from,
        "year_to": year_to,
        "tags": tag,
        "has_pdf": has_pdf,
        "has_html": has_html,
        "has_code": has_code,
        "has_guide": has_guide,
    }
    filters = {key: value for key, value in filters.items() if value is not None}
    runtime_filters = {} if paper_grouping else filters
    search_hits: dict[UUID, Any] = {}
    search_detail: dict[str, Any] = {}
    if q.strip():
        try:
            search_window = max(100, offset + PAGE_SIZE)
            search_options: dict[str, Any] = {}
            if paper_grouping:
                with _database(request).session() as count_session:
                    active_asset_count = (
                        count_session.scalar(
                            select(func.count(Asset.id)).where(Asset.deleted_at.is_(None))
                        )
                        or 0
                    )
                search_window = max(search_window, active_asset_count)
                search_options["candidate_limit"] = search_window
            result = _search_runtime(request).service.search(
                q,
                mode=mode,
                filters=runtime_filters,
                limit=search_window,
                **search_options,
            )
            for hit in result.hits:
                try:
                    asset_id = UUID(hit.document.asset_id)
                except ValueError:
                    continue
                search_hits.setdefault(asset_id, hit)
            search_detail = {
                "degraded": result.degraded,
                "errors": list(result.errors),
                "embedding": (
                    {
                        "provider": result.embedding_status.provider,
                        "model": result.embedding_status.model,
                        "detail": result.embedding_status.detail,
                    }
                    if result.embedding_status
                    else None
                ),
                "reranker": (
                    {
                        "provider": result.reranker_status.provider,
                        "model": result.reranker_status.model,
                        "detail": result.reranker_status.detail,
                    }
                    if result.reranker_status
                    else None
                ),
            }
        except Exception as error:
            search_detail = {
                "degraded": True,
                "errors": [f"hybrid-runtime:{type(error).__name__}"],
                "detail": "PostgreSQL metadata fallback is active.",
            }

    with _database(request).session() as session:
        catalog_assets = [
            asset
            for asset in session.scalars(
                select(Asset)
                .where(Asset.deleted_at.is_(None))
                .order_by(Asset.updated_at.desc(), Asset.id)
            ).all()
            if not _is_system_hidden_asset(asset)
        ]
        if search_hits:
            indexed_assets = {item.id: item for item in catalog_assets if item.id in search_hits}
            assets = [
                indexed_assets[asset_id] for asset_id in search_hits if asset_id in indexed_assets
            ]
            if not assets:
                search_hits.clear()
                search_detail = {
                    "degraded": True,
                    "errors": ["hybrid-runtime:stale-or-foreign-index"],
                    "detail": "PostgreSQL metadata fallback is active.",
                }
                assets = catalog_assets
        else:
            assets = catalog_assets

        context_assets = catalog_assets if paper_grouping else assets
        states, summaries = _asset_context(session, context_assets)
        context_asset_ids = [asset.id for asset in context_assets]
        annotation_assets = {
            asset_id
            for asset_id in session.scalars(
                select(Annotation.asset_id).where(
                    Annotation.deleted_at.is_(None),
                    Annotation.asset_id.in_(context_asset_ids),
                )
            )
        }
        representations: dict[UUID, list[Representation]] = defaultdict(list)
        if context_asset_ids:
            for representation in session.scalars(
                select(Representation)
                .where(
                    Representation.deleted_at.is_(None),
                    Representation.asset_id.in_(context_asset_ids),
                )
                .order_by(
                    Representation.asset_id,
                    Representation.role,
                    Representation.representation_type,
                    Representation.id,
                )
            ):
                representations[representation.asset_id].append(representation)
        filtered: list[Asset] = []
        for asset in assets:
            if not paper_grouping:
                if domain and domain not in (asset.domain_slugs or []):
                    continue
                if asset_type and asset_type != "all" and asset.asset_type != asset_type:
                    continue
                if author and not any(
                    author.casefold() in item.casefold() for item in asset.authors or []
                ):
                    continue
                if year_from is not None and (asset.year is None or asset.year < year_from):
                    continue
                if year_to is not None and (asset.year is None or asset.year > year_to):
                    continue
                if tag and not set(tag).intersection(asset.tags or []):
                    continue
                reading_state = states.get(asset.id)
                reading_status = reading_state.status if reading_state is not None else "unread"
                if status and reading_status != status:
                    continue
                if has_annotation is not None and (
                    (asset.id in annotation_assets) != has_annotation
                ):
                    continue
                asset_representations = representations.get(asset.id, [])
                availability_flags = {
                    kind: _has_available_action(asset_representations, kind)
                    for kind in ("pdf", "html", "code", "guide")
                }
                if has_pdf is not None and availability_flags["pdf"] != has_pdf:
                    continue
                if has_html is not None and availability_flags["html"] != has_html:
                    continue
                if has_code is not None and availability_flags["code"] != has_code:
                    continue
                if has_guide is not None and availability_flags["guide"] != has_guide:
                    continue
            if q.strip() and not search_hits:
                haystack = " ".join(
                    [
                        asset.title or "",
                        asset.title_zh or "",
                        " ".join(asset.authors or []),
                        asset.abstract or "",
                        asset.summary or "",
                        " ".join(asset.tags or []),
                    ]
                ).casefold()
                if q.casefold() not in haystack and not any(
                    token in haystack for token in _tokens(q)
                ):
                    continue
            filtered.append(asset)

        if paper_grouping:
            units, facet_assets = _build_research_units(
                session,
                catalog_assets=catalog_assets,
                matched_assets=filtered,
                search_hits=search_hits,
                query=q,
                states=states,
                summaries=summaries,
                annotation_assets=annotation_assets,
                representations=representations,
                include_independent_repositories=asset_type != "paper",
                domain=domain,
                status=status,
                author=author,
                year_from=year_from,
                year_to=year_to,
                tags=tag,
                has_pdf=has_pdf,
                has_html=has_html,
                has_code=has_code,
                has_guide=has_guide,
                has_annotation=has_annotation,
            )
            payload = _list_payload(units[offset : offset + PAGE_SIZE], offset, len(units))
            payload["mode"] = mode
            payload["group_by"] = "paper"
            payload.update(search_detail)
            payload.setdefault("degraded", bool(q.strip() and not search_hits))
            payload["facets"] = _asset_facets(facet_assets, states)
            return payload

        page = filtered[offset : offset + PAGE_SIZE]
        items: list[dict[str, Any]] = []
        for asset in page:
            selected_hit = search_hits.get(asset.id)
            item = serialize_asset(
                asset,
                reading_state=states.get(asset.id),
                annotation_summary=summaries.get(asset.id),
                score=(round(float(selected_hit.score), 6) if selected_hit is not None else 1.0),
            )
            if selected_hit is not None:
                item.update(
                    {
                        "snippet": " ".join(selected_hit.document.text.split())[:360],
                        "document_kind": selected_hit.document.kind,
                        "anchor": selected_hit.document.metadata.get("anchor"),
                        "representation_id": selected_hit.document.metadata.get(
                            "representation_id"
                        ),
                        "search_source": selected_hit.source,
                        "exact_match": selected_hit.exact_match,
                    }
                )
            item["representations"] = [
                serialize_representation(representation)
                for representation in representations.get(asset.id, [])
                if representation.availability == "available"
            ]
            items.append(item)
        payload = _list_payload(items, offset, len(filtered))
        payload["mode"] = mode
        payload.update(search_detail)
        payload.setdefault("degraded", bool(q.strip() and not search_hits))
        payload["facets"] = _asset_facets(filtered, states)
        return payload


@router.get("/assets")
def list_assets(
    request: Request,
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    cursor: str | None = None,
    include_deleted: bool = False,
    include: str | None = Query(None, pattern="^representations$"),
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        statement = select(Asset).order_by(Asset.updated_at.desc(), Asset.id)
        if not include_deleted:
            statement = statement.where(Asset.deleted_at.is_(None))
        assets = [
            asset
            for asset in session.scalars(statement).all()
            if not _is_system_hidden_asset(asset)
        ]
        states, summaries = _asset_context(session, assets)
        filtered: list[Asset] = []
        for asset in assets:
            if domain and domain not in (asset.domain_slugs or []):
                continue
            reading_state = states.get(asset.id)
            current_status = reading_state.status if reading_state is not None else "unread"
            if status and current_status != status:
                continue
            filtered.append(asset)
        page = filtered[offset : offset + PAGE_SIZE]
        items = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
            )
            for item in page
        ]
        if include == "representations":
            page_representations: dict[UUID, list[dict[str, Any]]] = defaultdict(list)
            if page:
                for representation in session.scalars(
                    select(Representation)
                    .where(
                        Representation.asset_id.in_([item.id for item in page]),
                        Representation.deleted_at.is_(None),
                    )
                    .order_by(
                        Representation.asset_id,
                        Representation.role,
                        Representation.representation_type,
                        Representation.id,
                    )
                ):
                    page_representations[representation.asset_id].append(
                        serialize_representation(representation)
                    )
            for item, asset in zip(items, page, strict=True):
                item["representations"] = page_representations[asset.id]
        return _list_payload(items, offset, len(filtered))


@router.post("/assets", status_code=201)
def create_asset(
    payload: AssetCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-asset",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        asset = Asset(**payload.model_dump())
        session.add(asset)
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="asset.created",
            idempotency_scope="create-asset",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result


@router.get("/assets/{asset_id}")
def get_asset(asset_id: UUID, request: Request, response: Response) -> dict[str, Any]:
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        state = session.scalar(
            select(ReadingState).where(
                ReadingState.asset_id == asset.id,
                ReadingState.user_id == "local",
            )
        )
        annotation = session.scalar(
            select(Annotation)
            .where(
                Annotation.asset_id == asset.id,
                Annotation.deleted_at.is_(None),
            )
            .order_by(Annotation.pinned.desc(), Annotation.updated_at.desc())
        )
        summary = None
        if annotation is not None:
            summary = " ".join((annotation.summary or annotation.body_markdown or "").split())[:80]
        result = serialize_asset(asset, reading_state=state, annotation_summary=summary)
        result["representations"] = [
            serialize_representation(item)
            for item in session.scalars(
                select(Representation).where(
                    Representation.asset_id == asset.id,
                    Representation.deleted_at.is_(None),
                )
            ).all()
        ]
        result["annotations"] = [
            serialize_annotation(item)
            for item in session.scalars(
                select(Annotation).where(
                    Annotation.asset_id == asset.id,
                    Annotation.deleted_at.is_(None),
                )
            ).all()
        ]
        response.headers["ETag"] = _etag(asset.row_version)
        return result


@router.get("/asset-redirects/{from_asset_id}")
@router.get("/assets/{from_asset_id}/redirect")
def get_asset_redirect(
    from_asset_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _database(request).session() as session:
        canonical_id, chain = _resolve_asset_redirect(session, from_asset_id)
        if not chain:
            raise not_found("AssetRedirect", from_asset_id)
        canonical = session.get(Asset, canonical_id)
        if canonical is None:
            raise ApiProblem(
                500,
                "资产重定向损坏",
                f"The redirect target {canonical_id} does not exist.",
                problem_type="urn:research-asset-hub:problem:broken-redirect",
            )
        _set_canonical_asset_headers(
            response,
            requested_id=from_asset_id,
            canonical_id=canonical_id,
        )
        response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return {
            "from_asset_id": str(from_asset_id),
            "to_asset_id": str(chain[0].to_asset_id),
            "canonical_asset_id": str(canonical_id),
            "permanent": True,
            "canonical_deleted": canonical.deleted_at is not None,
            "chain": [
                {
                    "from_asset_id": str(item.from_asset_id),
                    "to_asset_id": str(item.to_asset_id),
                    "reason": item.reason,
                    "created_at": item.created_at.isoformat(),
                }
                for item in chain
            ],
        }


@router.patch("/assets/{asset_id}")
def update_asset(
    asset_id: UUID,
    payload: AssetPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        _check_if_match(if_match, asset.row_version, required=True)
        before = _asset_snapshot(asset)
        _apply_asset_patch(asset, payload)
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="asset.updated",
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result


@router.delete("/assets/{asset_id}", status_code=204)
def delete_asset(
    asset_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _check_if_match(if_match, asset.row_version)
        before = _asset_snapshot(asset)
        asset.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.DELETE,
            before=before,
            after=_asset_snapshot(asset),
            request_id=_request_id(request),
            event_type="asset.deleted",
        )
    return Response(status_code=204)


@router.post("/assets/{asset_id}/restore")
def restore_asset(
    asset_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id, include_deleted=True)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        _check_if_match(if_match, asset.row_version)
        if asset.deleted_at is None:
            result = serialize_asset(asset)
            response.headers["ETag"] = _etag(asset.row_version)
            return result
        before = _asset_snapshot(asset)
        asset.deleted_at = None
        session.flush()
        result = serialize_asset(asset)
        record_mutation(
            session,
            entity_type="asset",
            entity_id=asset.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="asset.restored",
        )
        response.headers["ETag"] = _etag(asset.row_version)
        return result


@router.put("/assets/{asset_id}/reading-state")
def update_reading_state(
    asset_id: UUID,
    payload: ReadingStateUpdate,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    now = datetime.now(UTC)
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id)
        _set_canonical_asset_headers(
            response,
            requested_id=asset_id,
            canonical_id=asset.id,
        )
        state = session.scalar(
            select(ReadingState).where(
                ReadingState.asset_id == asset.id,
                ReadingState.user_id == "local",
            )
        )
        before = (
            {"status": state.status, "row_version": state.row_version}
            if state is not None
            else None
        )
        if state is None:
            state = ReadingState(user_id="local", asset_id=asset.id)
            session.add(state)
            session.flush()
        _check_if_match(if_match, state.row_version)
        state.status = payload.status
        if payload.status == "reading" and state.started_at is None:
            state.started_at = now
        if payload.status == "read":
            state.started_at = state.started_at or now
            state.read_at = now
        elif payload.status == "unread":
            state.started_at = None
            state.read_at = None
        session.flush()
        after = {"status": state.status, "row_version": state.row_version}
        record_mutation(
            session,
            entity_type="reading_state",
            entity_id=state.id,
            action=AuditAction.UPDATE if before is not None else AuditAction.CREATE,
            before=before,
            after=after,
            request_id=_request_id(request),
            event_type="reading-state.updated",
        )
        response.headers["ETag"] = _etag(state.row_version)
        return serialize_asset(asset, reading_state=state)


@router.get("/overrides")
def list_overrides(
    request: Request,
    entity_id: UUID | None = None,
    field_path: str | None = None,
    include_cleared: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        if entity_id is not None:
            entity_id, _ = _resolve_asset_redirect(session, entity_id)
        kinds = [FieldResolutionKind.OVERRIDE.value]
        if include_cleared:
            kinds.append(FieldResolutionKind.CLEARED.value)
        statement = (
            select(FieldResolution)
            .where(
                FieldResolution.entity_type == "asset",
                FieldResolution.resolution_kind.in_(kinds),
            )
            .order_by(FieldResolution.updated_at.desc(), FieldResolution.id)
        )
        if entity_id is not None:
            statement = statement.where(FieldResolution.entity_id == entity_id)
        if field_path is not None:
            statement = statement.where(FieldResolution.field_path == field_path)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_override(item) for item in page],
            offset,
            len(all_items),
        )


@router.post("/overrides", status_code=201)
def create_override(
    payload: OverrideCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _database(request).session() as session:
        _override_asset_field(payload.field_path)
        replay = replay_idempotent(
            session,
            scope="create-override",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _etag(int(replay["row_version"]))
            return replay
        asset = _live_asset(session, payload.entity_id)
        asset_before = serialize_asset(asset)
        override_value = _validated_override_value(
            _override_asset_field(payload.field_path),
            payload.override_value,
        )
        item = session.scalar(
            select(FieldResolution).where(
                FieldResolution.entity_type == payload.entity_type,
                FieldResolution.entity_id == asset.id,
                FieldResolution.field_path == payload.field_path,
            )
        )
        before: dict[str, Any] | None = None
        action = AuditAction.CREATE
        if item is not None:
            if item.resolution_kind == FieldResolutionKind.OVERRIDE.value:
                raise conflict(
                    f"An active override already exists as {item.id}; update it with PATCH."
                )
            before = {
                "id": str(item.id),
                "entity_type": item.entity_type,
                "entity_id": str(item.entity_id),
                "field_path": item.field_path,
                "resolution_kind": item.resolution_kind,
                "selected_claim_id": (
                    str(item.selected_claim_id) if item.selected_claim_id is not None else None
                ),
                "override_value": item.override_value,
                "reason": item.reason,
                "row_version": item.row_version,
            }
            action = (
                AuditAction.RESTORE
                if item.resolution_kind == FieldResolutionKind.CLEARED.value
                else AuditAction.UPDATE
            )
            item.resolution_kind = FieldResolutionKind.OVERRIDE.value
            item.selected_claim_id = None
            item.override_value = override_value
            item.reason = payload.reason
            item.resolved_by = "local"
        else:
            item = FieldResolution(
                entity_type=payload.entity_type,
                entity_id=asset.id,
                field_path=payload.field_path,
                resolution_kind=FieldResolutionKind.OVERRIDE.value,
                selected_claim_id=None,
                override_value=override_value,
                reason=payload.reason,
                resolved_by="local",
            )
            session.add(item)
        session.flush()
        _ensure_override_base_claim(session, item, asset)
        item.resolution_kind = FieldResolutionKind.OVERRIDE.value
        item.selected_claim_id = None
        item.override_value = override_value
        item.reason = payload.reason
        item.resolved_by = "local"
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=item.override_value,
        )
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-projected",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=action,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.created",
            idempotency_scope="create-override",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/overrides/{override_id}")
def get_override(
    override_id: UUID,
    request: Request,
    response: Response,
    include_cleared: bool = False,
) -> dict[str, Any]:
    with _database(request).session() as session:
        item = _live_override(
            session,
            override_id,
            include_deleted=include_cleared,
        )
        result = _serialize_override(item)
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.patch("/overrides/{override_id}")
def update_override(
    override_id: UUID,
    payload: OverridePatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        item = _live_override(session, override_id)
        _check_if_match(if_match, item.row_version, required=True)
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        changes = payload.model_dump(exclude_unset=True)
        if "override_value" in changes:
            item.override_value = _validated_override_value(
                _override_asset_field(item.field_path),
                changes["override_value"],
            )
            _project_override_value(
                asset,
                field_path=item.field_path,
                value=item.override_value,
            )
        if "reason" in changes:
            item.reason = changes["reason"]
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-projected",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.updated",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.delete("/overrides/{override_id}", status_code=204)
def delete_override(
    override_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _database(request).session() as session:
        item = _live_override(session, override_id)
        _check_if_match(if_match, item.row_version)
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        base_claim = _current_source_claim(session, item)
        if base_claim is None:
            raise conflict(
                "The override has no active source or recoverable base claim and "
                "cannot be safely cleared."
            )
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=base_claim.value_json,
        )
        item.resolution_kind = FieldResolutionKind.CLEARED.value
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-cleared",
        )
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.DELETE,
            before=before,
            after=_serialize_override(item),
            request_id=_request_id(request),
            event_type="field-override.cleared",
        )
    return Response(status_code=204)


@router.post("/overrides/{override_id}/restore")
def restore_override(
    override_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        item = _live_override(session, override_id, include_deleted=True)
        _check_if_match(if_match, item.row_version)
        if item.resolution_kind == FieldResolutionKind.OVERRIDE.value:
            result = _serialize_override(item)
            response.headers["ETag"] = _etag(item.row_version)
            return result
        before = _serialize_override(item)
        asset = _live_asset(session, item.entity_id)
        asset_before = serialize_asset(asset)
        if _override_base_claim(session, item) is None:
            raise conflict(
                "The override has no recoverable base claim and cannot be safely restored."
            )
        _project_override_value(
            asset,
            field_path=item.field_path,
            value=item.override_value,
        )
        item.resolution_kind = FieldResolutionKind.OVERRIDE.value
        item.resolved_by = "local"
        session.flush()
        _record_asset_projection(
            session,
            asset=asset,
            before=asset_before,
            request=request,
            event_type="asset.override-restored",
        )
        result = _serialize_override(item)
        record_mutation(
            session,
            entity_type="field_override",
            entity_id=item.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="field-override.restored",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/assets/{asset_id}/representations")
def list_representations(asset_id: UUID, request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        asset = _live_asset(session, asset_id)
        items = list(
            session.scalars(
                select(Representation)
                .where(
                    Representation.asset_id == asset.id,
                    Representation.deleted_at.is_(None),
                )
                .order_by(Representation.role, Representation.representation_type)
            ).all()
        )
        return {
            "items": [serialize_representation(item) for item in items],
            "next_cursor": None,
            "total": len(items),
        }


@router.post("/representations/{representation_id}/open-actions")
def open_representation(
    representation_id: UUID,
    request: Request,
    payload: Annotated[RepresentationOpenRequest | None, Body()] = None,
) -> dict[str, Any]:
    settings = _settings(request)
    requested_presentation = payload.presentation if payload is not None else "original"
    embed = payload.embed if payload is not None else False
    with _database(request).session() as session:
        representation = session.get(Representation, representation_id)
        if representation is None or representation.deleted_at is not None:
            raise not_found("Representation", representation_id)
        if representation.availability != "available":
            raise conflict("Only representations with availability=available can be opened.")
        if representation.external_url and not representation.path_ref:
            result: dict[str, Any] = {
                "kind": "external_url",
                "url": representation.external_url,
                "network_required": representation.network_required,
                "presentation": "original",
                "embed": embed,
            }
            if requested_presentation == "hub":
                result["warning"] = (
                    "Hub presentation is unavailable for an external-only representation."
                )
            return result
        try:
            token = create_gateway_token(
                settings.gateway_secret,
                representation.id,
                ttl_seconds=settings.gateway_session_seconds,
                nonce=uuid4().hex,
            )
        except ValueError as error:
            raise ApiProblem(
                503,
                "资产网关未配置",
                str(error),
                problem_type="urn:research-asset-hub:problem:gateway-configuration",
            ) from error
        origin = settings.gateway_origin.rstrip("/")
        url = f"{origin}/session/{representation.id}?token={quote(token, safe='')}"
        if payload is not None:
            url = f"{url}&presentation={requested_presentation}"
        return {
            "kind": "gateway_session",
            "url": url,
            "expires_in": settings.gateway_session_seconds,
            "representation_id": str(representation.id),
            "presentation": requested_presentation,
            "embed": embed,
        }


@router.get("/annotations")
def list_annotations(
    request: Request,
    asset_id: UUID | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = (
            select(Annotation)
            .where(Annotation.deleted_at.is_(None))
            .order_by(Annotation.pinned.desc(), Annotation.updated_at.desc())
        )
        if asset_id is not None:
            statement = statement.where(Annotation.asset_id == asset_id)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [serialize_annotation(item) for item in page],
            offset,
            len(all_items),
        )


@router.post("/annotations", status_code=201)
def create_annotation(
    payload: AnnotationCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _database(request).session() as session:
        asset = _live_asset(session, payload.asset_id)
        if payload.representation_id is not None:
            representation = session.get(Representation, payload.representation_id)
            if (
                representation is None
                or representation.deleted_at is not None
                or representation.asset_id != asset.id
            ):
                raise ApiProblem(
                    422,
                    "表示层无效",
                    "The representation does not belong to the annotation asset.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
        replay = replay_idempotent(
            session,
            scope="create-annotation",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        annotation_values = payload.model_dump()
        annotation_values["asset_id"] = asset.id
        annotation = Annotation(**annotation_values)
        session.add(annotation)
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.created",
            idempotency_scope="create-annotation",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result


@router.patch("/annotations/{annotation_id}")
def update_annotation(
    annotation_id: UUID,
    payload: AnnotationPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        annotation = _live_annotation(session, annotation_id)
        _check_if_match(if_match, annotation.row_version, required=True)
        before = serialize_annotation(annotation)
        current_revision = (
            session.scalar(
                select(func.max(AnnotationRevision.revision)).where(
                    AnnotationRevision.annotation_id == annotation.id
                )
            )
            or 0
        )
        session.add(
            AnnotationRevision(
                annotation_id=annotation.id,
                revision=current_revision + 1,
                title=annotation.title,
                summary=annotation.summary,
                body_markdown=annotation.body_markdown,
                anchor=annotation.anchor,
            )
        )
        if payload.representation_id is not None:
            representation = session.get(Representation, payload.representation_id)
            if (
                representation is None
                or representation.deleted_at is not None
                or representation.asset_id != annotation.asset_id
            ):
                raise ApiProblem(
                    422,
                    "表示层无效",
                    "The representation does not belong to the annotation asset.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
        for key, value in payload.model_dump(exclude_unset=True).items():
            setattr(annotation, key, value)
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.updated",
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result


@router.delete("/annotations/{annotation_id}", status_code=204)
def delete_annotation(
    annotation_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _database(request).session() as session:
        annotation = _live_annotation(session, annotation_id)
        _check_if_match(if_match, annotation.row_version)
        before = serialize_annotation(annotation)
        annotation.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.DELETE,
            before=before,
            after=serialize_annotation(annotation),
            request_id=_request_id(request),
            event_type="annotation.deleted",
        )
    return Response(status_code=204)


@router.get("/annotations/{annotation_id}/revisions")
def annotation_revisions(annotation_id: UUID, request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        _live_annotation(session, annotation_id, include_deleted=True)
        revisions = list(
            session.scalars(
                select(AnnotationRevision)
                .where(AnnotationRevision.annotation_id == annotation_id)
                .order_by(AnnotationRevision.revision.desc())
            ).all()
        )
        return {
            "items": [serialize_annotation_revision(item) for item in revisions],
            "next_cursor": None,
            "total": len(revisions),
        }


@router.post("/annotations/{annotation_id}/revisions/{revision_number}/restore")
def restore_annotation_revision(
    annotation_id: UUID,
    revision_number: int,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        annotation = _live_annotation(session, annotation_id, include_deleted=True)
        _check_if_match(if_match, annotation.row_version)
        revision = session.scalar(
            select(AnnotationRevision).where(
                AnnotationRevision.annotation_id == annotation_id,
                AnnotationRevision.revision == revision_number,
            )
        )
        if revision is None:
            raise not_found("Annotation revision", revision_number)
        before = serialize_annotation(annotation)
        current_revision = (
            session.scalar(
                select(func.max(AnnotationRevision.revision)).where(
                    AnnotationRevision.annotation_id == annotation.id
                )
            )
            or 0
        )
        session.add(
            AnnotationRevision(
                annotation_id=annotation.id,
                revision=current_revision + 1,
                title=annotation.title,
                summary=annotation.summary,
                body_markdown=annotation.body_markdown,
                anchor=annotation.anchor,
                source="restore",
            )
        )
        annotation.title = revision.title
        annotation.summary = revision.summary
        annotation.body_markdown = revision.body_markdown
        annotation.anchor = revision.anchor
        annotation.deleted_at = None
        session.flush()
        result = serialize_annotation(annotation)
        record_mutation(
            session,
            entity_type="annotation",
            entity_id=annotation.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="annotation.revision-restored",
        )
        response.headers["ETag"] = _etag(annotation.row_version)
        return result


@router.get("/relations")
def list_relations(
    request: Request,
    subject_asset_id: UUID | None = None,
    object_asset_id: UUID | None = None,
    asset_id: UUID | None = None,
    relation_type: str | None = None,
    confirmation_status: str | None = Query(
        None,
        pattern="^(candidate|confirmed|rejected)$",
    ),
    include_deleted: bool = False,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        if subject_asset_id is not None:
            subject_asset_id, _ = _resolve_asset_redirect(
                session,
                subject_asset_id,
            )
        if object_asset_id is not None:
            object_asset_id, _ = _resolve_asset_redirect(
                session,
                object_asset_id,
            )
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = select(Relation).order_by(Relation.updated_at.desc(), Relation.id)
        if not include_deleted:
            statement = statement.where(Relation.deleted_at.is_(None))
        if subject_asset_id is not None:
            statement = statement.where(Relation.subject_asset_id == subject_asset_id)
        if object_asset_id is not None:
            statement = statement.where(Relation.object_asset_id == object_asset_id)
        if asset_id is not None:
            statement = statement.where(
                or_(
                    Relation.subject_asset_id == asset_id,
                    Relation.object_asset_id == asset_id,
                )
            )
        if relation_type is not None:
            statement = statement.where(Relation.relation_type == relation_type)
        if confirmation_status is not None:
            statement = statement.where(Relation.confirmation_status == confirmation_status)
        all_items = list(session.scalars(statement).all())
        page = all_items[offset : offset + PAGE_SIZE]
        relation_ids = [item.id for item in page]
        evidence_counts: dict[UUID, int] = {}
        if relation_ids:
            evidence_counts = {
                relation_id: int(count)
                for relation_id, count in session.execute(
                    select(
                        RelationEvidenceModel.relation_id,
                        func.count(RelationEvidenceModel.id),
                    )
                    .where(RelationEvidenceModel.relation_id.in_(relation_ids))
                    .group_by(RelationEvidenceModel.relation_id)
                ).all()
            }
        return _list_payload(
            [
                _serialize_relation(
                    item,
                    evidence_count=evidence_counts.get(item.id, 0),
                )
                for item in page
            ],
            offset,
            len(all_items),
        )


@router.post("/relations", status_code=201)
def create_relation(
    payload: RelationCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    with _database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-relation",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            response.headers["ETag"] = _etag(int(replay["row_version"]))
            return replay
        subject_asset = _live_asset(session, payload.subject_asset_id)
        object_asset = _live_asset(session, payload.object_asset_id)
        relation_type = payload.relation_type.value
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=subject_asset.id,
            relation_type=relation_type,
            object_asset_id=object_asset.id,
        )
        if duplicate is not None:
            state = "soft-deleted; restore it" if duplicate.deleted_at else "already active"
            raise conflict(
                f"The logical relation already exists ({duplicate.id}, {state}). "
                "Add evidence to the existing relation instead."
            )
        relation = Relation(
            subject_asset_id=subject_asset.id,
            object_asset_id=object_asset.id,
            relation_type=relation_type,
            source_key=payload.source_key,
            evidence={},
            confidence=payload.confidence,
            confirmation_status=payload.confirmation_status.value,
            attributes=payload.attributes,
        )
        session.add(relation)
        session.flush()
        result = _serialize_relation(relation, evidence_count=0)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="relation.created",
            idempotency_scope="create-relation",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result


@router.get("/relations/{relation_id}")
def get_relation(
    relation_id: UUID,
    request: Request,
    response: Response,
    include_deleted: bool = False,
) -> dict[str, Any]:
    with _database(request).session() as session:
        relation = _live_relation(
            session,
            relation_id,
            include_deleted=include_deleted,
        )
        evidence_count = int(
            session.scalar(
                select(func.count(RelationEvidenceModel.id)).where(
                    RelationEvidenceModel.relation_id == relation.id
                )
            )
            or 0
        )
        result = _serialize_relation(relation, evidence_count=evidence_count)
        response.headers["ETag"] = _etag(relation.row_version)
        return result


@router.patch("/relations/{relation_id}")
def update_relation(
    relation_id: UUID,
    payload: RelationPatch,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        relation = _live_relation(session, relation_id)
        _check_if_match(if_match, relation.row_version, required=True)
        subject_asset_id = (
            _live_asset(session, payload.subject_asset_id).id
            if payload.subject_asset_id is not None
            else relation.subject_asset_id
        )
        object_asset_id = (
            _live_asset(session, payload.object_asset_id).id
            if payload.object_asset_id is not None
            else relation.object_asset_id
        )
        if subject_asset_id == object_asset_id:
            raise ApiProblem(
                422,
                "关系端点无效",
                "A relation cannot connect an asset to itself.",
                problem_type="urn:research-asset-hub:problem:validation",
            )
        _live_asset(session, subject_asset_id)
        _live_asset(session, object_asset_id)
        relation_type = (
            payload.relation_type.value
            if payload.relation_type is not None
            else relation.relation_type
        )
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=subject_asset_id,
            relation_type=relation_type,
            object_asset_id=object_asset_id,
            exclude_id=relation.id,
        )
        if duplicate is not None:
            raise conflict(
                f"Updating this relation would duplicate logical relation {duplicate.id}."
            )
        before = _serialize_relation(relation)
        changes = payload.model_dump(exclude_unset=True)
        changes["subject_asset_id"] = subject_asset_id
        changes["object_asset_id"] = object_asset_id
        for key, value in changes.items():
            if key in {"relation_type", "confirmation_status"} and value is not None:
                value = value.value
            setattr(relation, key, value)
        session.flush()
        evidence_count = int(
            session.scalar(
                select(func.count(RelationEvidenceModel.id)).where(
                    RelationEvidenceModel.relation_id == relation.id
                )
            )
            or 0
        )
        result = _serialize_relation(relation, evidence_count=evidence_count)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="relation.updated",
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result


@router.delete("/relations/{relation_id}", status_code=204)
def delete_relation(
    relation_id: UUID,
    request: Request,
    if_match: str | None = Header(None, alias="If-Match"),
) -> Response:
    with _database(request).session() as session:
        relation = _live_relation(session, relation_id)
        _check_if_match(if_match, relation.row_version)
        before = _serialize_relation(relation)
        relation.deleted_at = datetime.now(UTC)
        session.flush()
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.DELETE,
            before=before,
            after=_serialize_relation(relation),
            request_id=_request_id(request),
            event_type="relation.deleted",
        )
    return Response(status_code=204)


@router.post("/relations/{relation_id}/restore")
def restore_relation(
    relation_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        relation = _live_relation(session, relation_id, include_deleted=True)
        _check_if_match(if_match, relation.row_version)
        if relation.deleted_at is None:
            result = _serialize_relation(relation)
            response.headers["ETag"] = _etag(relation.row_version)
            return result
        duplicate = _relation_duplicate(
            session,
            subject_asset_id=relation.subject_asset_id,
            relation_type=relation.relation_type,
            object_asset_id=relation.object_asset_id,
            exclude_id=relation.id,
        )
        if duplicate is not None:
            raise conflict(
                f"Relation {duplicate.id} now owns the same logical edge; "
                "the deleted relation cannot be restored."
            )
        before = _serialize_relation(relation)
        relation.deleted_at = None
        session.flush()
        result = _serialize_relation(relation)
        record_mutation(
            session,
            entity_type="relation",
            entity_id=relation.id,
            action=AuditAction.RESTORE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="relation.restored",
        )
        response.headers["ETag"] = _etag(relation.row_version)
        return result


@router.get("/relations/{relation_id}/evidence")
def list_relation_evidence(
    relation_id: UUID,
    request: Request,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        _live_relation(session, relation_id, include_deleted=True)
        all_items = list(
            session.scalars(
                select(RelationEvidenceModel)
                .where(RelationEvidenceModel.relation_id == relation_id)
                .order_by(
                    RelationEvidenceModel.created_at.desc(),
                    RelationEvidenceModel.id,
                )
            ).all()
        )
        page = all_items[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_relation_evidence(item) for item in page],
            offset,
            len(all_items),
        )


@router.post("/relations/{relation_id}/evidence", status_code=201)
def create_relation_evidence(
    relation_id: UUID,
    payload: RelationEvidenceCreate,
    request: Request,
    response: Response,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    request_payload = payload.model_dump(mode="json")
    scope = f"create-relation-evidence:{relation_id}"
    with _database(request).session() as session:
        relation = _live_relation(session, relation_id)
        replay = replay_idempotent(
            session,
            scope=scope,
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            response.headers["Idempotency-Replayed"] = "true"
            return replay
        if (
            payload.source_record_version_id is not None
            and session.get(SourceRecordVersion, payload.source_record_version_id) is None
        ):
            raise not_found("SourceRecordVersion", payload.source_record_version_id)
        evidence_hash = stable_hash(
            {
                "source_record_version_id": payload.source_record_version_id,
                "provider": payload.provider,
                "source_key": payload.source_key,
                "evidence": payload.evidence,
                "confidence": payload.confidence,
            }
        )
        duplicate = session.scalar(
            select(RelationEvidenceModel).where(
                RelationEvidenceModel.relation_id == relation.id,
                RelationEvidenceModel.evidence_hash == evidence_hash,
            )
        )
        if duplicate is not None:
            raise conflict(f"Equivalent immutable evidence already exists as {duplicate.id}.")
        item = RelationEvidenceModel(
            relation_id=relation.id,
            source_record_version_id=payload.source_record_version_id,
            provider=payload.provider,
            source_key=payload.source_key,
            evidence=payload.evidence,
            evidence_hash=evidence_hash,
            confidence=payload.confidence,
            fetched_at=payload.fetched_at,
        )
        session.add(item)
        session.flush()
        result = _serialize_relation_evidence(item)
        record_mutation(
            session,
            entity_type="relation_evidence",
            entity_id=item.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="relation-evidence.created",
            idempotency_scope=scope,
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        return result


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
                Representation.representation_type.in_(_HTML_REPRESENTATION_TYPES),
            )
            .order_by(Representation.asset_id, Representation.id)
        ):
            representations[representation.asset_id].append(representation)

    result: dict[str, dict[str, Any]] = {}
    domain_slugs = {
        slug for asset in portal_assets for slug in (asset.domain_slugs or [])
    }
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


def _domains_with_counts(
    session: Session,
    settings: ApiSettings,
    assets: list[Asset] | None = None,
    *,
    include_overview: bool = False,
) -> list[dict[str, Any]]:
    all_assets = list(
        session.scalars(select(Asset).where(Asset.deleted_at.is_(None))).all()
    )
    portal_assets = [
        asset for asset in all_assets if _is_domain_overview_asset(asset)
    ]
    content_assets = (
        [asset for asset in all_assets if not _is_system_hidden_asset(asset)]
        if assets is None
        else [asset for asset in assets if not _is_system_hidden_asset(asset)]
    )
    states, _ = _asset_context(session, content_assets)
    overviews = _domain_overviews(session, portal_assets)
    confirmed_repository_ids = set(
        session.scalars(
            select(Relation.object_asset_id).where(
                Relation.relation_type == "implemented_by",
                Relation.confirmation_status == "confirmed",
                Relation.deleted_at.is_(None),
            )
        ).all()
    )
    result: list[dict[str, Any]] = []
    catalog = _domain_catalog(settings)
    known = {item["slug"] for item in catalog}
    discovered = {
        slug
        for asset in [*content_assets, *portal_assets]
        for slug in (asset.domain_slugs or [])
    }
    for unknown in sorted(discovered - known):
        catalog.append(
            {
                "slug": unknown,
                "label": unknown,
                "aliases": [],
                "description": None,
                "tags": [],
            }
        )
    unavailable_overview = serialize_domain_overview(
        asset_id=None,
        representation_id=None,
        status="unavailable",
        default_presentation="original",
        last_verified_at=None,
        warning="No domain overview is registered.",
    )
    for domain in catalog:
        slug = str(domain["slug"])
        matching = [
            item for item in content_assets if slug in (item.domain_slugs or [])
        ]
        reading_counts = {"unread": 0, "reading": 0, "read": 0}
        for asset in matching:
            reading_state = states.get(asset.id)
            value = reading_state.status if reading_state is not None else "unread"
            reading_counts[value] = reading_counts.get(value, 0) + 1
        annotation_count = 0
        matching_ids = [item.id for item in matching]
        if matching_ids:
            annotation_count = (
                session.scalar(
                    select(func.count(Annotation.id)).where(
                        Annotation.asset_id.in_(matching_ids),
                        Annotation.deleted_at.is_(None),
                    )
                )
                or 0
            )
        paper_count = sum(item.asset_type == "paper" for item in matching)
        repository_count = sum(item.asset_type == "repository" for item in matching)
        independent_repository_count = sum(
            item.asset_type == "repository" and item.id not in confirmed_repository_ids
            for item in matching
        )
        overview = overviews.get(slug, unavailable_overview)
        item = {
            **domain,
            "name": domain["label"],
            "asset_count": len(matching),
            "paper_count": paper_count,
            "repository_count": repository_count,
            "research_unit_count": paper_count + independent_repository_count,
            "annotation_count": annotation_count,
            "reading_counts": reading_counts,
            "overview_status": overview["status"],
            "recent_assets": _serialize_assets(
                session,
                sorted(matching, key=lambda asset: asset.updated_at, reverse=True)[:5],
            ),
        }
        if include_overview:
            item["overview"] = overview
        result.append(item)
    return result


@router.get("/domains")
def list_domains(request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        items = _domains_with_counts(session, _settings(request))
        return {"items": items, "next_cursor": None, "total": len(items)}


@router.get("/domains/{slug}")
def get_domain(slug: str, request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        for item in _domains_with_counts(
            session,
            _settings(request),
            include_overview=True,
        ):
            if item["slug"] == slug:
                return item
    raise not_found("Domain", slug)


def _graph_payload(
    session: Session,
    node_ids: set[UUID],
    relations: list[Relation],
    *,
    truncated: bool = False,
) -> dict[str, Any]:
    assets = {
        item.id: item for item in session.scalars(select(Asset).where(Asset.id.in_(node_ids))).all()
    }
    states, _ = _asset_context(session, list(assets.values()))
    return {
        "nodes": [
            {
                "id": str(asset.id),
                "label": asset.title,
                "title": asset.title,
                "asset_type": asset.asset_type,
                "domain_slugs": list(asset.domain_slugs or []),
                "year": asset.year,
                "reading_status": (states[asset.id].status if asset.id in states else "unread"),
            }
            for asset in assets.values()
        ],
        "edges": [
            {
                "id": str(item.id),
                "source": str(item.subject_asset_id),
                "target": str(item.object_asset_id),
                "subject_asset_id": str(item.subject_asset_id),
                "object_asset_id": str(item.object_asset_id),
                "relation_type": item.relation_type,
                "label": item.relation_type,
                "confidence": item.confidence,
                "confirmation_status": item.confirmation_status,
            }
            for item in relations
        ],
        "truncated": truncated,
    }


@router.get("/graph/neighbors")
def graph_neighbors(
    request: Request,
    asset_id: UUID,
    depth: int = Query(1, ge=1, le=3),
    relation_type: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _database(request).session() as session:
        asset_id = _live_asset(session, asset_id).id
        all_relations = list(
            session.scalars(
                select(Relation).where(
                    Relation.deleted_at.is_(None),
                    *([Relation.relation_type == relation_type] if relation_type else []),
                    *(
                        [Relation.confirmation_status == relation_status]
                        if relation_status != "all"
                        else []
                    ),
                )
            ).all()
        )
        visited = {asset_id}
        frontier = {asset_id}
        chosen: list[Relation] = []
        for _ in range(depth):
            next_frontier: set[UUID] = set()
            for relation in all_relations:
                if relation.subject_asset_id in frontier or relation.object_asset_id in frontier:
                    chosen.append(relation)
                    next_frontier.update([relation.subject_asset_id, relation.object_asset_id])
            next_frontier -= visited
            visited |= next_frontier
            frontier = next_frontier
            if not frontier or len(visited) >= 1_000:
                break
        unique_relations = list({item.id: item for item in chosen}.values())
        if status:
            states, _ = _asset_context(
                session,
                list(session.scalars(select(Asset).where(Asset.id.in_(visited))).all()),
            )
            filtered = {
                node_id
                for node_id in visited
                if node_id == asset_id
                or (states.get(node_id) is not None and states[node_id].status == status)
                or (status == "unread" and states.get(node_id) is None)
            }
            unique_relations = [
                item
                for item in unique_relations
                if item.subject_asset_id in filtered and item.object_asset_id in filtered
            ]
            visited = filtered
        return _graph_payload(
            session,
            visited,
            unique_relations,
            truncated=len(visited) >= 1_000,
        )


@router.get("/graph/path")
def graph_path(
    request: Request,
    from_asset: Annotated[UUID, Query(alias="from")],
    to_asset: Annotated[UUID, Query(alias="to")],
    max_depth: int = Query(3, ge=1, le=6),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _database(request).session() as session:
        from_asset = _live_asset(session, from_asset).id
        to_asset = _live_asset(session, to_asset).id
        relation_statement = select(Relation).where(Relation.deleted_at.is_(None))
        if relation_status != "all":
            relation_statement = relation_statement.where(
                Relation.confirmation_status == relation_status
            )
        relations = list(session.scalars(relation_statement).all())
        adjacency: dict[UUID, list[tuple[UUID, Relation]]] = defaultdict(list)
        for relation in relations:
            adjacency[relation.subject_asset_id].append((relation.object_asset_id, relation))
            adjacency[relation.object_asset_id].append((relation.subject_asset_id, relation))
        queue: deque[UUID] = deque([from_asset])
        parent: dict[UUID, tuple[UUID, Relation] | None] = {from_asset: None}
        while queue and to_asset not in parent:
            current = queue.popleft()
            current_depth = 0
            cursor = current
            while parent[cursor] is not None:
                cursor_link = parent[cursor]
                if cursor_link is None:
                    break
                cursor = cursor_link[0]
                current_depth += 1
            if current_depth >= max_depth:
                continue
            for neighbor, relation in adjacency[current]:
                if neighbor not in parent:
                    parent[neighbor] = (current, relation)
                    queue.append(neighbor)
        if to_asset not in parent:
            return {"nodes": [], "edges": [], "truncated": False}
        node_ids = {to_asset}
        selected: list[Relation] = []
        current = to_asset
        parent_link = parent[current]
        while parent_link is not None:
            previous_node, relation = parent_link
            node_ids.add(previous_node)
            selected.append(relation)
            current = previous_node
            parent_link = parent[current]
        selected.reverse()
        return _graph_payload(session, node_ids, selected)


@router.get("/graph/common-neighbors")
def graph_common_neighbors(
    request: Request,
    left: UUID,
    right: UUID,
    relation_type: str | None = None,
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _database(request).session() as session:
        left = _live_asset(session, left).id
        right = _live_asset(session, right).id
        relations = list(
            session.scalars(
                select(Relation).where(
                    Relation.deleted_at.is_(None),
                    *([Relation.relation_type == relation_type] if relation_type else []),
                    *(
                        [Relation.confirmation_status == relation_status]
                        if relation_status != "all"
                        else []
                    ),
                )
            ).all()
        )
        adjacency: dict[UUID, set[UUID]] = defaultdict(set)
        for relation in relations:
            adjacency[relation.subject_asset_id].add(relation.object_asset_id)
            adjacency[relation.object_asset_id].add(relation.subject_asset_id)
        common = adjacency[left] & adjacency[right]
        selected = [
            relation
            for relation in relations
            if (relation.subject_asset_id in {left, right} and relation.object_asset_id in common)
            or (relation.object_asset_id in {left, right} and relation.subject_asset_id in common)
        ]
        return {
            **_graph_payload(
                session,
                {left, right, *common},
                selected,
            ),
            "common_neighbor_ids": [str(item) for item in sorted(common, key=str)],
        }


@router.get("/graph/timeline")
def graph_timeline(
    request: Request,
    domain: str | None = None,
    status: str | None = Query(None, pattern="^(unread|reading|read)$"),
    relation_status: str = Query(
        "confirmed",
        pattern="^(candidate|confirmed|rejected|all)$",
    ),
) -> dict[str, Any]:
    with _database(request).session() as session:
        assets = list(
            session.scalars(
                select(Asset)
                .where(
                    Asset.deleted_at.is_(None),
                    Asset.asset_type == "paper",
                )
                .order_by(Asset.year.asc().nullslast(), Asset.title, Asset.id)
            ).all()
        )
        if domain:
            assets = [asset for asset in assets if domain in (asset.domain_slugs or [])]
        states, summaries = _asset_context(session, assets)
        if status:
            assets = [
                asset
                for asset in assets
                if (states[asset.id].status if asset.id in states else "unread") == status
            ]
        relation_counts = {
            asset_id: int(count)
            for asset_id, count in session.execute(
                select(
                    Asset.id,
                    func.count(Relation.id),
                )
                .outerjoin(
                    Relation,
                    and_(
                        or_(
                            Relation.subject_asset_id == Asset.id,
                            Relation.object_asset_id == Asset.id,
                        ),
                        Relation.deleted_at.is_(None),
                        *(
                            [Relation.confirmation_status == relation_status]
                            if relation_status != "all"
                            else []
                        ),
                    ),
                )
                .where(Asset.id.in_([asset.id for asset in assets]))
                .group_by(Asset.id)
            )
        }
        items = [
            {
                **serialize_asset(
                    asset,
                    reading_state=states.get(asset.id),
                    annotation_summary=summaries.get(asset.id),
                ),
                "relation_count": relation_counts.get(asset.id, 0),
            }
            for asset in assets
        ]
        return {
            "items": items,
            "next_cursor": None,
            "total": len(items),
        }


def _comparison_columns(assets: list[Asset]) -> list[dict[str, Any]]:
    fields: list[tuple[str, str, Any]] = [
        ("year", "年份", lambda item: item.year),
        ("authors", "作者", lambda item: item.authors),
        ("venue", "发表场所", lambda item: item.venue),
        ("domains", "领域", lambda item: item.domain_slugs),
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


@router.get("/comparisons")
def list_comparisons(request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        views = list(
            session.scalars(
                select(SavedView)
                .where(
                    SavedView.user_id == "local",
                    SavedView.view_kind == "comparison",
                    SavedView.deleted_at.is_(None),
                )
                .order_by(SavedView.updated_at.desc())
            ).all()
        )
        items: list[dict[str, Any]] = []
        for view in views:
            ids = [UUID(value) for value in view.definition.get("asset_ids", [])]
            assets = [
                asset
                for asset in session.scalars(
                    select(Asset).where(Asset.id.in_(ids))
                ).all()
                if not _is_system_hidden_asset(asset)
            ]
            order = {asset_id: index for index, asset_id in enumerate(ids)}
            assets.sort(key=lambda item: order.get(item.id, len(order)))
            items.append(serialize_comparison(view, _serialize_assets(session, assets)))
        return {"items": items, "next_cursor": None, "total": len(items)}


@router.post("/comparisons", status_code=201)
def create_comparison(
    payload: ComparisonCreate,
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if len(set(payload.asset_ids)) != len(payload.asset_ids):
        raise ApiProblem(
            422,
            "论文选择无效",
            "A comparison cannot contain the same asset more than once.",
            problem_type="urn:research-asset-hub:problem:validation",
        )
    request_payload = payload.model_dump(mode="json")
    with _database(request).session() as session:
        replay = replay_idempotent(
            session,
            scope="create-comparison",
            key=idempotency_key,
            request_payload=request_payload,
        )
        if replay is not None:
            return replay
        assets = [_live_asset(session, asset_id) for asset_id in payload.asset_ids]
        title = payload.title or " vs. ".join(item.title[:40] for item in assets)
        existing_view = session.scalar(
            select(SavedView).where(
                SavedView.user_id == "local",
                SavedView.view_kind == "comparison",
                SavedView.name == title[:255],
                SavedView.deleted_at.is_(None),
            )
        )
        requested_ids = [str(item.id) for item in assets]
        if existing_view is not None and existing_view.definition.get("asset_ids") == requested_ids:
            result = serialize_comparison(
                existing_view,
                _serialize_assets(session, assets),
            )
            if idempotency_key:
                record_mutation(
                    session,
                    entity_type="saved_view",
                    entity_id=existing_view.id,
                    action=AuditAction.UPDATE,
                    before=result,
                    after=result,
                    request_id=_request_id(request),
                    event_type="comparison.reused",
                    idempotency_scope="create-comparison",
                    idempotency_key=idempotency_key,
                    request_payload=request_payload,
                    response_payload=result,
                )
            return result
        if existing_view is not None:
            title = f"{title[:240]} · {uuid4().hex[:8]}"
        definition = {
            "asset_ids": requested_ids,
            "columns": _comparison_columns(assets),
        }
        view = SavedView(
            user_id="local",
            view_kind="comparison",
            name=title[:255],
            definition=definition,
        )
        session.add(view)
        session.flush()
        result = serialize_comparison(view, _serialize_assets(session, assets))
        record_mutation(
            session,
            entity_type="saved_view",
            entity_id=view.id,
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_request_id(request),
            event_type="comparison.created",
            idempotency_scope="create-comparison",
            idempotency_key=idempotency_key,
            request_payload=request_payload,
            response_payload=result,
        )
        return result


@router.get("/similar/{asset_id}")
def similar_assets(asset_id: UUID, request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        target = _live_asset(session, asset_id)
        try:
            semantic = _search_runtime(request).service.search(
                "\n".join(
                    value
                    for value in (
                        target.title,
                        target.title_zh,
                        target.abstract,
                        target.summary,
                    )
                    if value
                ),
                mode="semantic",
                filters={"type": "paper"},
                limit=60,
                rerank=True,
            )
        except Exception:
            semantic = None
        if semantic is not None:
            ranked_ids: list[UUID] = []
            hit_by_asset: dict[UUID, Any] = {}
            for hit in semantic.hits:
                try:
                    candidate_id = UUID(hit.document.asset_id)
                except ValueError:
                    continue
                if candidate_id == target.id or candidate_id in hit_by_asset:
                    continue
                hit_by_asset[candidate_id] = hit
                ranked_ids.append(candidate_id)
                if len(ranked_ids) >= 20:
                    break
            indexed = {
                item.id: item
                for item in session.scalars(
                    select(Asset).where(
                        Asset.id.in_(ranked_ids),
                        Asset.deleted_at.is_(None),
                        Asset.asset_type == "paper",
                    )
                )
            }
            values = [indexed[item] for item in ranked_ids if item in indexed]
            if values:
                states, summaries = _asset_context(session, values)
                return {
                    "items": [
                        serialize_asset(
                            item,
                            reading_state=states.get(item.id),
                            annotation_summary=summaries.get(item.id),
                            score=round(float(hit_by_asset[item.id].score), 6),
                        )
                        for item in values
                    ],
                    "next_cursor": None,
                    "total": len(values),
                    "method": "semantic-vector-rrf",
                    "degraded": semantic.degraded,
                    "errors": list(semantic.errors),
                }
        candidates = list(
            session.scalars(
                select(Asset).where(
                    Asset.deleted_at.is_(None),
                    Asset.id != target.id,
                )
            ).all()
        )
        target_terms = _tokens(f"{target.title} {target.title_zh or ''} {target.summary or ''}")
        target_domains = set(target.domain_slugs or [])
        target_tags = set(target.tags or [])
        ranked: list[tuple[float, Asset]] = []
        for item in candidates:
            terms = _tokens(f"{item.title} {item.title_zh or ''} {item.summary or ''}")
            term_score = len(target_terms & terms) / max(1, len(target_terms | terms))
            domain_score = len(target_domains & set(item.domain_slugs or []))
            tag_score = len(target_tags & set(item.tags or []))
            year_score = (
                max(0.0, 1.0 - abs(item.year - target.year) / 10)
                if item.year is not None and target.year is not None
                else 0.0
            )
            score = term_score * 3 + domain_score * 2 + tag_score + year_score * 0.25
            if score > 0:
                ranked.append((score, item))
        ranked.sort(key=lambda value: value[0], reverse=True)
        states, summaries = _asset_context(
            session,
            [asset for _, asset in ranked[:20]],
        )
        items = [
            serialize_asset(
                item,
                reading_state=states.get(item.id),
                annotation_summary=summaries.get(item.id),
                score=round(score, 4),
            )
            for score, item in ranked[:20]
        ]
        return {
            "items": items,
            "next_cursor": None,
            "total": len(items),
            "method": "metadata-fallback",
            "degraded": True,
        }


@router.post("/chat")
def chat(payload: ChatRequest, request: Request) -> dict[str, Any]:
    with _database(request).session() as session:
        canonical_asset_ids = tuple(
            _live_asset(session, asset_id).id for asset_id in payload.asset_ids
        )
        chat_session: ChatSession | None = None
        if payload.conversation_id is not None:
            chat_session = session.get(
                ChatSession,
                payload.conversation_id,
                with_for_update=True,
            )
            if chat_session is None or chat_session.deleted_at is not None:
                raise not_found("Chat session", payload.conversation_id)
        if chat_session is None:
            chat_session = ChatSession(
                title=payload.message[:120],
                filters={"asset_ids": [str(item) for item in canonical_asset_ids]},
            )
            session.add(chat_session)
            session.flush()
        next_sequence = (
            session.scalar(
                select(func.max(ChatMessage.sequence)).where(
                    ChatMessage.session_id == chat_session.id
                )
            )
            or 0
        ) + 1
        session.add(
            ChatMessage(
                session_id=chat_session.id,
                role="user",
                content=payload.message,
                sequence=next_sequence,
                status="complete",
            )
        )
        rag_result = answer_with_rag(
            session,
            _settings(request),
            payload.message,
            asset_ids=canonical_asset_ids,
            request_id=_request_id(request),
            search_runtime=_search_runtime(request),
        )
        answer = rag_result.answer.text
        assistant = ChatMessage(
            session_id=chat_session.id,
            role="assistant",
            content=answer,
            sequence=next_sequence + 1,
            status="complete",
            provider=rag_result.answer.provider,
            model=rag_result.answer.model,
            prompt_version=str(rag_result.audit.get("prompt_version") or "grounded-json-v1"),
            retrieval_snapshot=rag_result.audit,
        )
        session.add(assistant)
        session.flush()
        citation_payloads: list[dict[str, Any]] = []
        for ordinal, citation in enumerate(rag_result.answer.citations, 1):
            if (
                citation.chunk_id is None
                or citation.representation_id is None
                or citation.chunk_hash is None
            ):
                continue
            chunk = session.get(DocumentChunk, UUID(citation.chunk_id))
            if chunk is None:
                continue
            excerpt = " ".join(chunk.text_content.split())[:360]
            session.add(
                Citation(
                    message_id=assistant.id,
                    asset_id=UUID(citation.asset_id),
                    representation_id=UUID(citation.representation_id),
                    chunk_id=UUID(citation.chunk_id),
                    ordinal=ordinal,
                    excerpt=excerpt,
                    anchor=citation.anchor,
                    content_digest=citation.chunk_hash,
                )
            )
            citation_payloads.append(
                {
                    "asset_id": citation.asset_id,
                    "title": citation.title,
                    "excerpt": excerpt,
                    "representation_id": citation.representation_id,
                    "chunk_id": citation.chunk_id,
                    "anchor": citation.anchor,
                    "url": citation.url,
                }
            )
        record_mutation(
            session,
            entity_type="chat_message",
            entity_id=assistant.id,
            action=AuditAction.CREATE,
            before=None,
            after={
                "id": str(assistant.id),
                "session_id": str(chat_session.id),
                "role": assistant.role,
                "citation_count": len(citation_payloads),
            },
            request_id=_request_id(request),
            event_type="chat.answer-created",
        )
        return {
            "answer": answer,
            "citations": citation_payloads,
            "retrieved": [
                {
                    "asset_id": item.asset_id,
                    "title": item.title,
                    "representation_id": item.representation_id,
                    "chunk_id": item.chunk_id,
                    "anchor": item.anchor,
                    "score": item.score,
                    "sources": list(item.sources),
                    "url": item.url,
                }
                for item in rag_result.retrieved
            ],
            "conversation_id": str(chat_session.id),
            "mode": (
                "deepseek-grounded" if rag_result.stop_reason == "completed" else "retrieval-only"
            ),
            "provider": rag_result.answer.provider,
            "model": rag_result.answer.model,
            "stop_reason": rag_result.stop_reason,
        }


@router.post("/chat/stream", response_class=StreamingResponse)
def chat_stream(
    payload: ChatRequest,
    request: Request,
) -> StreamingResponse:
    result = chat(payload, request)

    def events() -> Iterable[str]:
        event_payloads = (
            ("evidence", {"items": result["retrieved"]}),
            (
                "answer",
                {
                    "answer": result["answer"],
                    "citations": result["citations"],
                    "mode": result["mode"],
                    "provider": result["provider"],
                    "model": result["model"],
                    "stop_reason": result["stop_reason"],
                },
            ),
            (
                "done",
                {
                    "conversation_id": result["conversation_id"],
                    "stop_reason": result["stop_reason"],
                },
            ),
        )
        for event_name, event_payload in event_payloads:
            encoded = json.dumps(
                event_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"event: {event_name}\ndata: {encoded}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/extractions")
def list_extractions(
    request: Request,
    representation_id: UUID | None = None,
    asset_id: UUID | None = None,
    status: str | None = Query(
        None,
        pattern="^(pending|running|succeeded|failed|cancelled)$",
    ),
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        if asset_id is not None:
            asset_id, _ = _resolve_asset_redirect(session, asset_id)
        statement = (
            select(Job)
            .where(Job.job_type.in_(["extract_representation", "representation.extract"]))
            .order_by(Job.created_at.desc(), Job.id)
        )
        if status is not None:
            statement = statement.where(Job.status == status)
        jobs = list(session.scalars(statement).all())
        job_representation_ids = {
            item.id: value for item in jobs if (value := _job_representation_id(item)) is not None
        }
        representation_ids = list(set(job_representation_ids.values()))
        representations = {
            item.id: item
            for item in (
                session.scalars(
                    select(Representation).where(Representation.id.in_(representation_ids))
                ).all()
                if representation_ids
                else []
            )
        }
        filtered: list[Job] = []
        for job in jobs:
            current_representation_id = job_representation_ids.get(job.id)
            representation = (
                representations.get(current_representation_id)
                if current_representation_id is not None
                else None
            )
            if representation_id is not None and current_representation_id != representation_id:
                continue
            if asset_id is not None and (
                representation is None or representation.asset_id != asset_id
            ):
                continue
            filtered.append(job)
        page = filtered[offset : offset + PAGE_SIZE]
        page_ids = [item.id for item in page]
        activities: dict[UUID, Activity] = {}
        if page_ids:
            for activity in session.scalars(
                select(Activity)
                .where(
                    Activity.job_id.in_(page_ids),
                    Activity.activity_type == "extraction",
                )
                .order_by(Activity.started_at.desc(), Activity.id.desc())
            ).all():
                if activity.job_id is not None:
                    activities.setdefault(activity.job_id, activity)
        page_representation_ids = {
            value for job in page if (value := job_representation_ids.get(job.id)) is not None
        }
        chunk_counts: dict[UUID, int] = {}
        if page_representation_ids:
            chunk_counts = {
                current_representation_id: int(count)
                for current_representation_id, count in session.execute(
                    select(
                        DocumentChunk.representation_id,
                        func.count(DocumentChunk.id),
                    )
                    .where(DocumentChunk.representation_id.in_(page_representation_ids))
                    .group_by(DocumentChunk.representation_id)
                ).all()
            }
        serialized_extractions: list[dict[str, Any]] = []
        for job in page:
            current_representation_id = job_representation_ids.get(job.id)
            serialized_extractions.append(
                _serialize_extraction(
                    job,
                    representation=(
                        representations.get(current_representation_id)
                        if current_representation_id is not None
                        else None
                    ),
                    activity=activities.get(job.id),
                    chunk_count=(
                        chunk_counts.get(current_representation_id, 0)
                        if current_representation_id is not None
                        else 0
                    ),
                )
            )
        payload = _list_payload(
            serialized_extractions,
            offset,
            len(filtered),
        )
        payload["counts"] = dict(Counter(item.status for item in filtered))
        return payload


@router.get("/extractions/{job_id}")
def get_extraction(
    job_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None or job.job_type not in {
            "extract_representation",
            "representation.extract",
        }:
            raise not_found("Extraction", job_id)
        representation_id = _job_representation_id(job)
        representation = (
            session.get(Representation, representation_id)
            if representation_id is not None
            else None
        )
        activity = session.scalar(
            select(Activity)
            .where(
                Activity.job_id == job.id,
                Activity.activity_type == "extraction",
            )
            .order_by(Activity.started_at.desc(), Activity.id.desc())
        )
        chunk_count = 0
        if representation_id is not None:
            chunk_count = int(
                session.scalar(
                    select(func.count(DocumentChunk.id)).where(
                        DocumentChunk.representation_id == representation_id
                    )
                )
                or 0
            )
        result = _serialize_extraction(
            job,
            representation=representation,
            activity=activity,
            chunk_count=chunk_count,
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result


@router.get("/indexes")
def list_indexes(
    request: Request,
    index_name: str | None = None,
    index_kind: str | None = Query(None, pattern="^(keyword|vector|code)$"),
    entity_type: str | None = None,
    entity_id: UUID | None = None,
    status: str | None = Query(
        None,
        pattern="^(pending|indexing|ready|stale|failed)$",
    ),
    version: str | None = None,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        criteria: list[Any] = []
        if index_name is not None:
            criteria.append(IndexState.index_name == index_name)
        if index_kind is not None:
            criteria.append(IndexState.index_kind == index_kind)
        if entity_type is not None:
            criteria.append(IndexState.entity_type == entity_type)
        if entity_id is not None:
            criteria.append(IndexState.entity_id == entity_id)
        if status is not None:
            criteria.append(IndexState.status == status)
        if version is not None:
            criteria.append(IndexState.version == version)
        total = int(session.scalar(select(func.count(IndexState.id)).where(*criteria)) or 0)
        page = list(
            session.scalars(
                select(IndexState)
                .where(*criteria)
                .order_by(IndexState.updated_at.desc(), IndexState.id)
                .offset(offset)
                .limit(PAGE_SIZE)
            ).all()
        )
        payload = _list_payload(
            [_serialize_index_state(item) for item in page],
            offset,
            total,
        )
        payload["counts"] = {
            "by_status": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.status, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.status)
                ).all()
            },
            "by_index": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.index_name, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.index_name)
                ).all()
            },
            "by_version": {
                str(key): int(count)
                for key, count in session.execute(
                    select(IndexState.version, func.count(IndexState.id))
                    .where(*criteria)
                    .group_by(IndexState.version)
                ).all()
            },
        }
        return payload


@router.get("/indexes/{index_state_id}")
def get_index_state(
    index_state_id: UUID,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    with _database(request).session() as session:
        item = session.get(IndexState, index_state_id)
        if item is None:
            raise not_found("IndexState", index_state_id)
        result = _serialize_index_state(item)
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/jobs")
def list_jobs(
    request: Request,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        statement = select(Job).order_by(Job.created_at.desc())
        if status:
            statement = statement.where(Job.status == status)
        jobs = list(session.scalars(statement).all())
        page = jobs[offset : offset + PAGE_SIZE]
        return _list_payload([serialize_job(item) for item in page], offset, len(jobs))


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise not_found("Job", job_id)
        _check_if_match(if_match, job.row_version)
        if job.status not in {JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
            raise conflict("Only failed or cancelled jobs can be retried.")
        before = serialize_job(job)
        job.status = JobStatus.PENDING.value
        job.progress = 0.0
        job.max_attempts = max(job.max_attempts, job.attempts + 1)
        job.cancel_requested = False
        job.error = None
        job.finished_at = None
        job.scheduled_at = datetime.now(UTC)
        job.lease_owner = None
        job.lease_until = None
        session.flush()
        result = serialize_job(job)
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="job.retried",
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result


@router.post("/jobs/{job_id}/cancel")
def cancel_job(
    job_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    with _database(request).session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise not_found("Job", job_id)
        _check_if_match(if_match, job.row_version)
        if job.status in {JobStatus.SUCCEEDED.value, JobStatus.FAILED.value}:
            raise conflict("A finished job cannot be cancelled.")
        before = serialize_job(job)
        if job.status == JobStatus.PENDING.value:
            job.status = JobStatus.CANCELLED.value
            job.finished_at = datetime.now(UTC)
        else:
            job.cancel_requested = True
        session.flush()
        result = serialize_job(job)
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="job.cancel-requested",
        )
        response.headers["ETag"] = _etag(job.row_version)
        return result


@router.get("/sync-conflicts")
def list_sync_conflicts(
    request: Request,
    cursor: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        statement = select(SyncConflict).order_by(SyncConflict.created_at.desc())
        if status:
            statement = statement.where(SyncConflict.status == status)
        conflicts = list(session.scalars(statement).all())
        page = conflicts[offset : offset + PAGE_SIZE]
        return _list_payload(
            [serialize_sync_conflict(item) for item in page],
            offset,
            len(conflicts),
        )


@router.post("/integrations/{provider}/sync/preview")
def preview_integration_sync(
    provider: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    provider = provider.casefold()
    settings = _settings(request)
    if provider == "obsidian":
        with _database(request).session() as session:
            result = preview_obsidian_assets(
                session,
                settings.obsidian_vault_path,
                hub_directory=settings.obsidian_hub_directory,
            )
            response.headers["ETag"] = f'"{result["etag"]}"'
            return result
    if provider != "zotero":
        raise ApiProblem(
            404,
            "未知集成",
            f"The integration provider {provider!r} is not supported.",
            problem_type="urn:research-asset-hub:problem:integration-not-found",
        )

    client = ZoteroLocalClient(settings.zotero_base_url)
    api_ready = client.api_ready()
    items: list[dict[str, Any]] = []
    if api_ready:
        try:
            items = client.list_items()
        except (httpx.HTTPError, ValueError, OSError):
            api_ready = False
    bbt = client.better_bibtex_ready() if api_ready else None
    package_paths = [
        settings.zotero_export_path / "research-assets.csl.json",
        settings.zotero_export_path / "research-assets.bib",
    ]
    package_state = [
        {
            "path": str(path),
            "exists": path.exists(),
            "size": path.stat().st_size if path.exists() else 0,
            "mtime_ns": path.stat().st_mtime_ns if path.exists() else 0,
        }
        for path in package_paths
    ]
    etag = _sync_etag(
        {
            "api_ready": api_ready,
            "item_count": len(items),
            "bbt": bbt,
            "packages": package_state,
        }
    )
    with _database(request).session() as session:
        integration = session.scalar(
            select(Integration).where(
                Integration.provider == "zotero",
                Integration.name == "Local Zotero",
            )
        )
        if integration is None:
            integration = Integration(
                provider="zotero",
                name="Local Zotero",
                config={"base_url": settings.zotero_base_url},
                enabled=True,
                status=(
                    IntegrationStatus.READY.value if api_ready else IntegrationStatus.ERROR.value
                ),
            )
            session.add(integration)
            session.flush()
        else:
            integration.status = (
                IntegrationStatus.READY.value if api_ready else IntegrationStatus.ERROR.value
            )
            integration.error = None if api_ready else {"code": "zotero_not_running"}
        run = SyncRun(
            integration_id=integration.id,
            status=SyncStatus.SUCCEEDED.value,
            mode="read-and-export-preview",
            preview_only=True,
            counters={
                "etag": etag,
                "zotero_items": len(items),
                "packages": package_state,
                "api_ready": api_ready,
                "better_bibtex_ready": bbt is not None,
            },
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        result = {
            "status": run.status,
            "sync_run_id": str(run.id),
            "preview_only": True,
            "etag": etag,
            "api_ready": api_ready,
            "better_bibtex": bbt,
            "zotero_items": len(items),
            "packages": package_state,
            "apply_mode": "reviewed-manual-import",
        }
        response.headers["ETag"] = f'"{etag}"'
        return result


@router.post("/integrations/{provider}/sync/apply")
def apply_integration_sync(
    provider: str,
    payload: SyncApplyRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    provider = provider.casefold()
    settings = _settings(request)
    with _database(request).session() as session:
        preview = session.get(SyncRun, payload.preview_id)
        if preview is None or not preview.preview_only:
            raise not_found("Sync preview", payload.preview_id)
        integration = session.get(Integration, preview.integration_id)
        if integration is None or integration.provider != provider:
            raise not_found("Integration preview", payload.preview_id)
        expected = str((preview.counters or {}).get("etag") or "")
        supplied = (if_match or "").strip().strip('"')
        if not supplied or supplied != expected:
            raise ApiProblem(
                412,
                "同步预览已失效",
                "Apply requires the exact ETag returned by the selected preview.",
                problem_type="urn:research-asset-hub:problem:sync-preview-stale",
                headers={"ETag": f'"{expected}"'} if expected else None,
            )

        if provider == "obsidian":
            current = obsidian_sync_etag(
                session,
                settings.obsidian_vault_path,
                hub_directory=settings.obsidian_hub_directory,
            )
            if current != expected:
                raise ApiProblem(
                    412,
                    "同步预览已失效",
                    "Hub or Obsidian content changed after preview; create a new preview.",
                    problem_type="urn:research-asset-hub:problem:sync-preview-stale",
                    headers={"ETag": f'"{current}"'},
                )
            result = sync_obsidian_assets(
                session,
                settings.obsidian_vault_path,
                hub_directory=settings.obsidian_hub_directory,
            )
            response.headers["ETag"] = f'"{current}"'
            return result

        if provider == "zotero":
            packages = [
                settings.zotero_export_path / "research-assets.csl.json",
                settings.zotero_export_path / "research-assets.bib",
            ]
            if not all(path.is_file() for path in packages):
                raise ApiProblem(
                    409,
                    "Zotero 导入包缺失",
                    "Generate the CSL JSON and BibTeX packages before applying this preview.",
                    problem_type="urn:research-asset-hub:problem:zotero-package-missing",
                )
            run = SyncRun(
                integration_id=integration.id,
                status=SyncStatus.NEEDS_REVIEW.value,
                mode="manual-import-package",
                preview_only=False,
                counters={
                    "packages": [str(path) for path in packages],
                    "external_write": False,
                    "requires_user_confirmation": True,
                },
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
            )
            session.add(run)
            session.flush()
            return {
                "status": run.status,
                "sync_run_id": str(run.id),
                "external_write": False,
                "requires_user_confirmation": True,
                "packages": [str(path) for path in packages],
                "detail": (
                    "Zotero local API is read-only; review and import one of these packages, "
                    "then run preview again to read back the resulting library."
                ),
            }

        raise not_found("Integration", provider)


@router.post("/sync-conflicts/{conflict_id}/resolve")
def resolve_sync_conflict(
    conflict_id: UUID,
    payload: ConflictResolutionRequest,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    mapping = {
        "keep_local": (ConflictResolution.HUB.value, "hub_value"),
        "accept_incoming": (ConflictResolution.EXTERNAL.value, "external_value"),
        "merge": (ConflictResolution.MANUAL.value, "manual"),
    }
    with _database(request).session() as session:
        item = session.get(SyncConflict, conflict_id)
        if item is None:
            raise not_found("Sync conflict", conflict_id)
        _check_if_match(if_match, item.row_version)
        if item.status != SyncConflictStatus.PENDING.value:
            raise conflict("This sync conflict has already been resolved.")
        resolution, source = mapping[payload.strategy]
        if source == "hub_value":
            resolved_value = item.hub_value
        elif source == "external_value":
            resolved_value = item.external_value
        else:
            if payload.resolved_value is None:
                raise ApiProblem(
                    422,
                    "合并值缺失",
                    "The merge strategy requires resolved_value.",
                    problem_type="urn:research-asset-hub:problem:validation",
                )
            resolved_value = payload.resolved_value
        before = serialize_sync_conflict(item)
        item.status = SyncConflictStatus.RESOLVED.value
        item.resolution = resolution
        item.resolved_value = resolved_value
        item.resolved_by = "local"
        item.resolved_at = datetime.now(UTC)
        session.flush()
        result = serialize_sync_conflict(item)
        record_mutation(
            session,
            entity_type="sync_conflict",
            entity_id=item.id,
            action=AuditAction.MERGE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="sync-conflict.resolved",
        )
        response.headers["ETag"] = _etag(item.row_version)
        return result


@router.get("/backups")
def list_backups(
    request: Request,
    cursor: str | None = None,
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    with _database(request).session() as session:
        runs = list(session.scalars(select(BackupRun).order_by(BackupRun.created_at.desc())).all())
        page = runs[offset : offset + PAGE_SIZE]
        return _list_payload(
            [_serialize_backup_run(item) for item in page],
            offset,
            len(runs),
        )


@router.post("/backups", status_code=201)
def create_backup(
    request: Request,
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    database = _database(request)
    with database.session() as session:
        if idempotency_key:
            existing = next(
                (
                    item
                    for item in session.scalars(
                        select(BackupRun).order_by(BackupRun.created_at.desc())
                    )
                    if (item.counters or {}).get("idempotency_key") == idempotency_key
                ),
                None,
            )
            if existing is not None:
                return _serialize_backup_run(existing)
        run = BackupRun(
            status=BackupStatus.RUNNING.value,
            backup_kind="full",
            destination_label=str(Path.home() / "Documents" / "ResearchAssetHubBackups"),
            local_copy_only=True,
            counters={"idempotency_key": idempotency_key},
            started_at=datetime.now(UTC),
        )
        session.add(run)
        session.flush()
        run_id = run.id
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=run.id,
            action=AuditAction.CREATE,
            before=None,
            after=_serialize_backup_run(run),
            request_id=_request_id(request),
            event_type="backup.started",
        )

    try:
        result = create_platform_backup(PROJECT_ROOT)
    except Exception as error:
        with database.session() as session:
            failed = session.get(BackupRun, run_id, with_for_update=True)
            if failed is not None:
                failed.status = BackupStatus.FAILED.value
                failed.error = {
                    "type": type(error).__name__,
                    "detail": " ".join(str(error).split())[:500],
                }
                failed.finished_at = datetime.now(UTC)
        raise ApiProblem(
            503,
            "备份失败",
            "The local encrypted backup could not be completed; inspect the backup run.",
            problem_type="urn:research-asset-hub:problem:backup-failed",
        ) from error

    with database.session() as session:
        completed = session.get(BackupRun, run_id, with_for_update=True)
        if completed is None:
            raise RuntimeError("Backup run disappeared after Restic completed.")
        before = _serialize_backup_run(completed)
        completed.status = BackupStatus.VERIFIED.value
        completed.manifest_path = str(result.get("staging_manifest") or "")
        completed.counters = {
            **dict(completed.counters or {}),
            **result,
        }
        completed.finished_at = datetime.now(UTC)
        completed.verified_at = datetime.now(UTC)
        manifest_path = Path(completed.manifest_path)
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            tree_digests = manifest.get("tree_digests")
            if isinstance(tree_digests, dict):
                completed.export_digest = tree_digests.get("open_catalog")
            files = manifest.get("files")
            if isinstance(files, dict):
                dump = next(
                    (
                        values
                        for path, values in files.items()
                        if str(path).endswith("/postgres.dump")
                    ),
                    None,
                )
                if isinstance(dump, dict):
                    completed.database_digest = str(dump.get("sha256") or "") or None
        session.flush()
        response = _serialize_backup_run(completed)
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=completed.id,
            action=AuditAction.UPDATE,
            before=before,
            after=response,
            request_id=_request_id(request),
            event_type="backup.verified",
        )
        return response


@router.get("/backups/restore-rehearsal/latest")
def latest_restore_rehearsal() -> dict[str, Any]:
    root = PROJECT_ROOT / "work" / "restore-tests"
    reports = sorted(
        root.glob("*/report.json") if root.is_dir() else (),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    if not reports:
        return {
            "status": "not_run",
            "risk": "local-copy-only",
            "detail": "No isolated restore rehearsal report exists yet.",
        }
    report_path = reports[0]
    raw_report = json.loads(report_path.read_text(encoding="utf-8"))
    report: dict[str, Any] = (
        dict(raw_report) if isinstance(raw_report, dict) else {"status": "invalid_report"}
    )
    report["report_path"] = str(report_path)
    return report


@router.post("/backups/{backup_id}/verify")
def verify_backup(
    backup_id: UUID,
    request: Request,
    response: Response,
    if_match: str | None = Header(None, alias="If-Match"),
) -> dict[str, Any]:
    check = BackupManager().check()
    with _database(request).session() as session:
        run = session.get(BackupRun, backup_id, with_for_update=True)
        if run is None:
            raise not_found("Backup run", backup_id)
        _check_if_match(if_match, run.row_version)
        before = _serialize_backup_run(run)
        run.status = BackupStatus.VERIFIED.value
        run.verified_at = datetime.now(UTC)
        run.counters = {**dict(run.counters or {}), "repository_check": check["status"]}
        session.flush()
        result = _serialize_backup_run(run)
        record_mutation(
            session,
            entity_type="backup_run",
            entity_id=run.id,
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_request_id(request),
            event_type="backup.repository-verified",
        )
        response.headers["ETag"] = _etag(run.row_version)
        return result


@router.get("/settings")
def settings_endpoint(request: Request) -> dict[str, Any]:
    settings = _settings(request)
    sources_document = _load_yaml(settings.sources_config_path)
    sources = []
    for item in sources_document.get("sources", []):
        if not isinstance(item, dict):
            continue
        sources.append(
            {
                "id": item.get("id"),
                "label": item.get("label"),
                "domain": item.get("domain"),
                "adapter": item.get("adapter"),
                "read_only": bool(item.get("read_only", True)),
                "expected_records": item.get("expected_records"),
            }
        )
    readiness = _database(request).check()
    with _database(request).session() as session:
        integrations = [
            {
                "id": str(item.id),
                "provider": item.provider,
                "name": item.name,
                "enabled": item.enabled,
                "status": item.status,
                "last_sync_at": (item.last_sync_at.isoformat() if item.last_sync_at else None),
            }
            for item in session.scalars(select(Integration).order_by(Integration.name)).all()
        ]
    return {
        "app": {
            "mode": "local-first",
            "database_status": readiness.status,
            "database_revision": readiness.revision,
            "gateway_origin": settings.gateway_origin,
        },
        "search": {
            "keyword_provider": "meilisearch",
            "vector_provider": "qdrant",
            "api_fallback": "metadata-lexical",
        },
        "rag": {
            "mode": "local-extractive",
            "privacy": "local-only",
        },
        "sources": sources,
        "integrations": integrations,
    }
