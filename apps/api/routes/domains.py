"""领域路由：领域目录与门户。"""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response

from apps.api import routes_v1 as _v1
from apps.api.catalog_queries import domain_counts
from apps.api.problem import ApiProblem, not_found
from apps.api.routes_v1 import (
    router,
)
from packages.domain_portals import DomainPortalLoadError, load_domain_portal, load_portal_index


@router.get("/domains")
def list_domains(request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        items = domain_counts(session, _v1._settings(request))
        return {"items": items, "next_cursor": None, "total": len(items)}


@router.get("/domains/{slug}/portal")
def get_domain_portal(
    slug: str,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    settings = _v1._settings(request)
    with _v1._database(request).session() as session:
        domain = next(
            (
                item
                for item in domain_counts(
                    session,
                    settings,
                    include_overview=True,
                    slug=slug,
                )
                if item["slug"] == slug
            ),
            None,
        )
    if domain is None:
        raise not_found("Domain", slug)
    if settings.domain_portal_renderer == "v1":
        response.headers["Cache-Control"] = "no-store"
        return {
            "schema": "research-asset-hub.domain-portal/v1",
            "renderer": "v1",
            "domain": domain,
            "portal": None,
        }
    try:
        index_path = settings.domain_portal_config_path / "portal-index.yaml"
        registered = index_path.is_file() and any(
            item.slug == slug
            for item in load_portal_index(settings.domain_portal_config_path).portals
        )
        if not registered:
            response.headers["Cache-Control"] = "no-store"
            return {
                "schema": "research-asset-hub.domain-portal-response/v2",
                "renderer": "v2",
                "domain": domain,
                "portal": None,
            }
        portal = load_domain_portal(
            settings.domain_portal_config_path,
            settings.host_workspace_root.parent,
            slug,
            defer_source_validation=True,
        )
    except DomainPortalLoadError as error:
        raise ApiProblem(
            503,
            "领域门户内容尚未就绪",
            str(error),
            problem_type="urn:research-asset-hub:problem:domain-portal-invalid",
            code="domain_portal_invalid",
        ) from error
    response.headers["Cache-Control"] = "no-store"
    return {
        "schema": "research-asset-hub.domain-portal-response/v2",
        "renderer": "v2",
        "domain": domain,
        "portal": portal.model_dump(mode="json", by_alias=True),
    }


@router.get("/domains/{slug}")
def get_domain(slug: str, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        for item in domain_counts(
            session,
            _v1._settings(request),
            include_overview=True,
            slug=slug,
        ):
            if item["slug"] == slug:
                return item
    raise not_found("Domain", slug)
