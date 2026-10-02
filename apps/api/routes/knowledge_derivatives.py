"""Read-only figure and source-reviewed methodology endpoints."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from fastapi import Query, Request, Response

from apps.api import routes_v1 as _v1
from apps.api.code_browser import CodeBrowserError, _safe_child, local_code_repository
from apps.api.problem import ApiProblem
from apps.api.routes.knowledge import _service
from packages.domain.models import Representation, Source
from packages.domain.serving_scope import RetiredRepresentationError, assert_representation_active
from packages.knowledge.derived import DerivativeStore
from packages.knowledge.service import (
    KnowledgeError,
    KnowledgeService,
    representation_source_type,
)


def search_derivatives(
    request: Request,
    query: str,
    domain: str | None,
    asset_type: str,
    source_type: str | None,
    limit: int,
) -> dict[str, Any]:
    if asset_type in {"writing_pattern", "code_pattern"}:
        if source_type == "original":
            return {"items": [], "total": 0, "limit": limit, "backend": "derived_cards"}
        return get_methodology(
            request,
            kind="writing" if asset_type == "writing_pattern" else "code",
            domain=domain,
            q=query,
            limit=limit,
        )
    if source_type == "analysis":
        return {"items": [], "total": 0, "limit": limit, "backend": "figure_metadata"}
    with _service(request) as service:
        matches = []
        unavailable = 0
        verified: dict[tuple[str, str, str], bool] = {}
        for figure in DerivativeStore(service.cache_root).list_figures():
            if domain and domain not in figure["domains"]:
                continue
            if query.strip().casefold() not in json.dumps(figure, ensure_ascii=False).casefold():
                continue
            rep_id = (figure["asset_id"], figure["representation_id"], figure["source_hash"])
            if rep_id not in verified:
                try:
                    _verify_figure(service, figure)
                    verified[rep_id] = True
                except (KnowledgeError, ApiProblem, RetiredRepresentationError):
                    verified[rep_id] = False
            if not verified[rep_id]:
                unavailable += 1
                continue
            matches.append(
                {
                    "asset_type": "figure",
                    "asset_id": figure["asset_id"],
                    "figure_id": figure["figure_id"],
                    "paper_title": figure["paper_title"],
                    "figure_number": figure.get("figure_number"),
                    "page": figure["page"],
                    "caption": figure["caption"],
                    "quality_status": figure["quality_status"],
                }
            )
        return {
            "items": matches[:limit],
            "total": len(matches),
            "limit": limit,
            "backend": "figure_metadata",
            "excluded_stale_or_unavailable": unavailable,
        }


def _verify_figure(service: KnowledgeService, figure: dict[str, Any]) -> None:
    _, representation, digest = service.require_representation(
        UUID(figure["asset_id"]), UUID(figure["representation_id"])
    )
    if digest != figure["source_hash"]:
        raise KnowledgeError("stale_figure", "The figure belongs to another source version.")
    if (
        representation.representation_type != "pdf"
        or representation_source_type(representation) != "original"
    ):
        raise KnowledgeError("invalid_figure_source", "A figure requires an original PDF source.")


@_v1.router.get("/knowledge/figures/{figure_id}")
def get_figure(figure_id: str, request: Request) -> dict[str, Any]:
    with _service(request) as service:
        figure = DerivativeStore(service.cache_root).get_figure(figure_id)
        _verify_figure(service, figure)
        return figure


@_v1.router.get("/knowledge/figures/{figure_id}/image")
def get_figure_image(figure_id: str, request: Request, preview: bool = True) -> Response:
    with _service(request) as service:
        store = DerivativeStore(service.cache_root)
        figure = store.get_figure(figure_id)
        _verify_figure(service, figure)
        data, mime = store.image(figure, preview=preview)
        return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store"})


@_v1.router.get("/knowledge/methodology")
def get_methodology(
    request: Request,
    kind: Literal["writing", "figure", "code"] | None = None,
    domain: str | None = None,
    q: str = Query(default="", max_length=500),
    limit: int = Query(default=10, ge=1, le=20),
    include_candidates: bool = False,
) -> dict[str, Any]:
    with _service(request) as service:
        cards = DerivativeStore(service.cache_root).methodology_cards(
            kind=kind,
            domain=domain,
            query=q,
            include_candidates=include_candidates,
        )
        visible = []
        excluded = 0
        for card in cards:
            try:
                if not card.get("asset_ids") or not card.get("evidence"):
                    raise KnowledgeError(
                        "unsupported_methodology", "A method needs source evidence."
                    )
                # Verify paper visibility even when code evidence points at a repository.
                for asset_id in card["asset_ids"]:
                    service._asset(UUID(asset_id))
                for evidence in card["evidence"]:
                    if card["kind"] == "code":
                        rep = service.session.get(
                            Representation, UUID(evidence["representation_id"])
                        )
                        if rep is None or str(rep.asset_id) != evidence["asset_id"]:
                            raise KnowledgeError(
                                "stale_methodology", "Code evidence is unavailable."
                            )
                        assert_representation_active(service.session, rep)
                        source = service.session.get(Source, rep.source_id)
                        if (
                            source is None
                            or not source.enabled
                            or rep.deleted_at
                            or rep.availability != "available"
                        ):
                            raise KnowledgeError("stale_methodology", "Code source is unavailable.")
                        service._asset(rep.asset_id)
                        root = local_code_repository(
                            service.registry,
                            rep,
                            source_alias=source.alias,
                        )
                        file_path = _safe_child(root, evidence["path"])
                        with file_path.open("rb") as stream:
                            digest = hashlib.file_digest(stream, "sha256").hexdigest()
                        if digest != evidence["source_hash"]:
                            raise KnowledgeError("stale_methodology", "Code evidence has changed.")
                    else:
                        _, _, digest = service.require_representation(
                            UUID(evidence["asset_id"]), UUID(evidence["representation_id"])
                        )
                        if digest != evidence["source_hash"]:
                            raise KnowledgeError(
                                "stale_methodology", "Evidence version has changed."
                            )
            except (
                KnowledgeError,
                ApiProblem,
                RetiredRepresentationError,
                CodeBrowserError,
                OSError,
                KeyError,
                TypeError,
                ValueError,
            ):
                excluded += 1
                continue
            visible.append(card)
        return {
            "items": visible[:limit],
            "total": len(visible),
            "limit": limit,
            "excluded_stale_or_unavailable": excluded,
            "review_scope": (
                "Agent source review is distinct from human review or experiment reproduction."
            ),
        }
