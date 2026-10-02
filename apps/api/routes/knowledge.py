"""GET-only knowledge tools backed by the formal library."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Query, Request, Response
from pydantic import Field

from apps.api import routes_v1 as _v1
from apps.api.config import PROJECT_ROOT
from apps.api.problem import ApiProblem
from apps.asset_gateway.registry import SourceConfigurationError
from packages.domain.serving_scope import RetiredRepresentationError
from packages.knowledge.domains import normalize_domain
from packages.knowledge.service import KnowledgeError, KnowledgeService
from packages.knowledge.status import build_knowledge_status

KeywordTerm = Annotated[str, Field(min_length=1, max_length=80)]
QueryConstraint = Annotated[str, Field(min_length=1, max_length=160)]


@contextmanager
def _service(request: Request) -> Iterator[KnowledgeService]:
    try:
        with _v1._database(request).session() as session:
            registry = _v1._source_registry(request)
            registry.refresh_if_changed()
            yield KnowledgeService(
                session,
                registry,
                PROJECT_ROOT / "outputs" / "knowledge-pilot" / "v1",
                asset_loader=_v1._live_asset,
                hidden_asset=_v1._is_system_hidden_asset,
                keyword_search=lambda: _v1._search_runtime(request).service,
                taxonomy_path=_v1._settings(request).taxonomy_config_path,
            )
    except SourceConfigurationError as error:
        raise ApiProblem(
            503,
            "来源配置暂不可用",
            "来源配置更新或验证失败；未将其作为论文缺失处理。",
            code="source_configuration_invalid",
        ) from error
    except RetiredRepresentationError as error:
        raise _v1._retired_representation_problem(error) from error
    except KnowledgeError as error:
        raise ApiProblem(
            error.status,
            "知识证据不可用",
            str(error),
            code=error.code,
            problem_type=f"urn:research-asset-hub:problem:{error.code}",
        ) from error


@_v1.router.get("/knowledge/search")
def search_assets(
    request: Request,
    query: str = Query(default="", max_length=500),
    domain: str | None = None,
    asset_type: str | None = None,
    source_type: Literal["original", "analysis"] | None = None,
    limit: int = Query(default=20, ge=1, le=20),
    keywords: Annotated[list[KeywordTerm] | None, Query(max_length=8)] = None,
    constraints: Annotated[list[QueryConstraint] | None, Query(max_length=6)] = None,
) -> dict[str, object]:
    if asset_type in {"figure", "writing_pattern", "code_pattern"}:
        from .knowledge_derivatives import search_derivatives

        return search_derivatives(
            request,
            query,
            domain,
            asset_type,
            source_type,
            limit,
            keywords=keywords or (),
            constraints=constraints or (),
        )
    with _service(request) as service:
        return service.search_assets(
            query,
            domain=domain,
            asset_type=asset_type,
            source_type=source_type,
            limit=limit,
            keywords=keywords or (),
            constraints=constraints or (),
        )


@_v1.router.get("/knowledge/papers/{asset_id}")
def get_paper(asset_id: UUID, request: Request, response: Response) -> dict[str, object]:
    with _service(request) as service:
        result = service.get_paper(asset_id)
        _v1._set_canonical_asset_headers(
            response, requested_id=asset_id, canonical_id=UUID(result["asset_id"])
        )
        return result


@_v1.router.get("/knowledge/papers/{asset_id}/evidence")
def read_evidence(
    asset_id: UUID,
    request: Request,
    response: Response,
    representation_id: UUID | None = None,
    source_type: Literal["original", "analysis"] = "original",
    page_start: int = Query(default=1, ge=1),
    page_end: int | None = Query(default=None, ge=1),
    offset: int = Query(default=0, ge=0),
    max_chars: int = Query(default=16000, ge=1, le=16000),
) -> dict[str, object]:
    with _service(request) as service:
        result = service.read_evidence(
            asset_id,
            representation_id=representation_id,
            source_type=source_type,
            page_start=page_start,
            page_end=page_end,
            offset=offset,
            max_chars=max_chars,
        )
        _v1._set_canonical_asset_headers(
            response, requested_id=asset_id, canonical_id=UUID(result["asset_id"])
        )
        return result


@_v1.router.get("/knowledge/status")
def knowledge_status(request: Request, domain: str | None = None) -> dict[str, object]:
    with _service(request) as service:
        settings = _v1._settings(request)
        try:
            selected = normalize_domain(domain, settings.taxonomy_config_path)
        except ValueError as error:
            raise KnowledgeError("invalid_domain", str(error), 422) from error
        return build_knowledge_status(
            service.session,
            service.registry,
            service.cache_root,
            settings,
            domain=selected,
        )
