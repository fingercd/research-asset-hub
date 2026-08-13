"""领域路由：设置。"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.routes_v1 import (
    _load_yaml,
    router,
)
from packages.domain.models import Integration


@router.get("/settings")
def settings_endpoint(request: Request) -> dict[str, Any]:
    settings = _v1._settings(request)
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
    readiness = _v1._database(request).check()
    with _v1._database(request).session() as session:
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
            "gitnexus_url": settings.gitnexus_url,
        },
        "search": {
            "keyword_provider": "meilisearch",
            "api_fallback": "metadata-lexical",
        },
        "sources": sources,
        "integrations": integrations,
    }
