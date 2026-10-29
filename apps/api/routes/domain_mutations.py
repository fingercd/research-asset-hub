from __future__ import annotations

from typing import Any
from uuid import NAMESPACE_URL, uuid5

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from apps.api import routes_v1 as _v1
from apps.api.events import record_mutation
from apps.api.problem import not_found
from packages.domain.enums import AuditAction
from packages.domain_catalog.service import create_domain, patch_domain


class DomainBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @field_validator("label", check_fields=False)
    @classmethod
    def nonblank_label(cls, value: str) -> str:
        if value is None or not value.strip():
            raise ValueError("领域名称不能为空")
        return value.strip()


class DomainCreate(DomainBody):
    label: str = Field(min_length=1, max_length=255)


class DomainPatch(DomainBody):
    label: str | None = Field(default=None, min_length=1, max_length=255)


def _payload(row: Any) -> dict[str, Any]:
    return {
        "slug": row.slug,
        "label": row.label,
        "description": row.description,
        "aliases": row.aliases,
        "tags": row.tags,
    }


@_v1.router.post("/domains", status_code=201)
def post_domain(payload: DomainCreate, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        row = create_domain(session, _v1._settings(request), payload.model_dump())
        result = _payload(row)
        record_mutation(
            session,
            entity_type="domain",
            entity_id=uuid5(NAMESPACE_URL, "research-asset-hub:domain:" + row.slug),
            action=AuditAction.CREATE,
            before=None,
            after=result,
            request_id=_v1._request_id(request),
            event_type="domain.created",
        )
        return result


@_v1.router.patch("/domains/{slug}")
def update_domain(slug: str, payload: DomainPatch, request: Request) -> dict[str, Any]:
    with _v1._database(request).session() as session:
        values = payload.model_dump(exclude_unset=True)
        if values.get("label", "") is None:
            from apps.api.problem import ApiProblem

            raise ApiProblem(422, "领域名称无效", "领域名称不能为空")
        from packages.domain_catalog.service import list_domain_definitions

        before = next(
            (
                item
                for item in list_domain_definitions(
                    session, _v1._settings(request), include_hidden=True
                )
                if item["slug"] == slug
            ),
            None,
        )
        row = patch_domain(session, _v1._settings(request), slug, values)
        if row is None:
            raise not_found("Domain", slug)
        result = _payload(row)
        record_mutation(
            session,
            entity_type="domain",
            entity_id=uuid5(NAMESPACE_URL, "research-asset-hub:domain:" + row.slug),
            action=AuditAction.UPDATE,
            before=before,
            after=result,
            request_id=_v1._request_id(request),
            event_type="domain.updated",
        )
        return result
