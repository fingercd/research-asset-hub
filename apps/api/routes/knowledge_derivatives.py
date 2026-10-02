"""Read-only figure and source-reviewed methodology endpoints."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Query, Request, Response
from sqlalchemy import select

from apps.api import routes_v1 as _v1
from apps.api.code_browser import CodeBrowserError, _safe_child, local_code_repository
from apps.api.domain_views import annotate_presentation_domains
from apps.api.problem import ApiProblem
from apps.api.routes.knowledge import KeywordTerm, QueryConstraint, _service
from apps.asset_gateway.registry import SourceConfigurationError
from packages.domain.models import Asset, Representation, Source
from packages.domain.serving_scope import RetiredRepresentationError, assert_representation_active
from packages.knowledge.derived import DerivativeStore
from packages.knowledge.domains import matches_domain, normalize_domain
from packages.knowledge.query_terms import QueryPlan, contains, plan_query
from packages.knowledge.ranking import lexical_rank
from packages.knowledge.service import (
    KnowledgeError,
    KnowledgeService,
    representation_source_type,
)


def _query_plan(
    service: KnowledgeService,
    query: str,
    domain: str | None,
    keywords: Sequence[str],
    constraints: Sequence[str],
) -> tuple[str | None, QueryPlan | None]:
    try:
        selected = normalize_domain(domain, service.taxonomy_path)
        if len(keywords) > 8 or len(constraints) > 6:
            raise ValueError("At most 8 keywords and 6 constraints are allowed.")
        plan = (
            plan_query(query.strip(), keywords, constraints) if query.strip() or keywords else None
        )
        return selected, plan
    except SourceConfigurationError:
        raise
    except ValueError as error:
        raise KnowledgeError("invalid_query", str(error), 422) from error


def _domain_assets(service: KnowledgeService, asset_ids: Sequence[str]) -> dict[str, Asset]:
    ids = set()
    for asset_id in asset_ids:
        try:
            ids.add(UUID(asset_id))
        except SourceConfigurationError:
            raise
        except (ValueError, TypeError):
            continue  # Individual malformed cards are counted by the evidence gate.
    assets = list(service.session.scalars(select(Asset).where(Asset.id.in_(ids))))
    annotate_presentation_domains(service.session, assets)
    return {str(asset.id): asset for asset in assets}


def _literal_match(payload: dict[str, Any], plan: QueryPlan | None) -> bool:
    if plan is None:
        return True
    text = json.dumps(payload, ensure_ascii=False)
    terms = [*plan.named_terms, *(term for group in plan.concept_groups for term in group.terms)]
    if not terms:
        terms = list(plan.queries)
    return any(contains(text, term) for term in terms)


def _ranked(payload: dict[str, Any], plan: QueryPlan | None) -> tuple[float, dict[str, Any]]:
    if plan is None:
        return 0, payload
    score, details = lexical_rank(
        plan,
        {**payload, "title": payload.get("paper_title") or payload.get("title", "")},
        snippet=json.dumps(payload, ensure_ascii=False),
    )
    return score, {**payload, "lexical_score": score, "ranking": details}


def search_derivatives(
    request: Request,
    query: str,
    domain: str | None,
    asset_type: str,
    source_type: str | None,
    limit: int,
    *,
    keywords: Sequence[str] = (),
    constraints: Sequence[str] = (),
) -> dict[str, Any]:
    if asset_type in {"writing_pattern", "code_pattern"}:
        if source_type == "original":
            with _service(request) as service:
                service.registry.refresh_if_changed()
                domain, plan = _query_plan(service, query, domain, keywords, constraints)
                return {
                    "items": [],
                    "total": 0,
                    "limit": limit,
                    "backend": "derived_cards",
                    "domain": domain,
                    "query_plan": plan.to_dict() if plan else None,
                }
        return get_methodology(
            request,
            kind="writing" if asset_type == "writing_pattern" else "code",
            domain=domain,
            q=query,
            limit=limit,
            keywords=list(keywords),
            constraints=list(constraints),
        )
    with _service(request) as service:
        service.registry.refresh_if_changed()
        domain, plan = _query_plan(service, query, domain, keywords, constraints)
        if source_type == "analysis":
            return {
                "items": [],
                "total": 0,
                "limit": limit,
                "backend": "figure_metadata",
                "domain": domain,
                "query_plan": plan.to_dict() if plan else None,
            }
        matches = []
        unavailable = 0
        verified: dict[tuple[str, str, str], bool] = {}
        figures = DerivativeStore(service.cache_root).list_figures()
        assets = _domain_assets(service, [figure["asset_id"] for figure in figures])
        for figure in figures:
            asset = assets.get(figure["asset_id"])
            if domain and asset is not None and not matches_domain(asset, domain):
                continue
            if not _literal_match(figure, plan):
                continue
            rep_id = (figure["asset_id"], figure["representation_id"], figure["source_hash"])
            if rep_id not in verified:
                try:
                    _verify_figure(service, figure)
                    verified[rep_id] = True
                except SourceConfigurationError:
                    raise
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
                    "domains": figure["domains"],
                }
            )
        ranked = [_ranked(item, plan) for item in matches]
        ranked.sort(key=lambda item: -item[0])
        return {
            "items": [item for _, item in ranked[:limit]],
            "total": len(matches),
            "limit": limit,
            "backend": "figure_metadata",
            "excluded_stale_or_unavailable": unavailable,
            "domain": domain,
            "query_plan": plan.to_dict() if plan else None,
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
        service.registry.refresh_if_changed()
        figure = DerivativeStore(service.cache_root).get_figure(figure_id)
        _verify_figure(service, figure)
        return figure


@_v1.router.get("/knowledge/figures/{figure_id}/image")
def get_figure_image(figure_id: str, request: Request, preview: bool = True) -> Response:
    with _service(request) as service:
        service.registry.refresh_if_changed()
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
    keywords: Annotated[list[KeywordTerm] | None, Query(max_length=8)] = None,
    constraints: Annotated[list[QueryConstraint] | None, Query(max_length=6)] = None,
) -> dict[str, Any]:
    with _service(request) as service:
        service.registry.refresh_if_changed()
        domain, plan = _query_plan(service, q, domain, keywords or (), constraints or ())
        cards = DerivativeStore(service.cache_root).methodology_cards(
            kind=kind,
            include_candidates=include_candidates,
        )
        assets = _domain_assets(
            service, [asset_id for card in cards for asset_id in card.get("asset_ids", [])]
        )
        visible = []
        excluded = 0
        for card in cards:
            if (
                domain
                and card.get("asset_ids")
                and all(
                    asset_id in assets and not matches_domain(assets[asset_id], domain)
                    for asset_id in card["asset_ids"]
                )
            ):
                continue
            if not _literal_match(card, plan):
                continue
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
            except SourceConfigurationError:
                raise
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
        ranked = [_ranked(card, plan) for card in visible]
        ranked.sort(key=lambda item: -item[0])
        return {
            "items": [card for _, card in ranked[:limit]],
            "total": len(visible),
            "limit": limit,
            "excluded_stale_or_unavailable": excluded,
            "review_scope": (
                "Agent source review is distinct from human review or experiment reproduction."
            ),
            "domain": domain,
            "query_plan": plan.to_dict() if plan else None,
        }
