"""Database retrieval and provider wiring for the versioned chat API."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.search_runtime import SearchRuntime
from packages.domain.enums import ConfirmationStatus, UsageKind
from packages.domain.models import (
    Asset,
    DocumentChunk,
    ProviderUsage,
    Relation,
    Representation,
)
from packages.indexing import SearchDocument, SearchHit
from packages.integrations import CredentialStore
from packages.rag import (
    BudgetLedger,
    BudgetPolicy,
    DeepSeekProviderSource,
    HttpxCompletionTransport,
    MiniMaxProviderSource,
    ModelPreference,
    ModelPricing,
    PricingCatalog,
    ProviderChain,
    RagRequest,
    RagService,
    RagServiceResult,
    UnifiedCandidates,
)

_WORD = re.compile(r"[\w\u3400-\u9fff-]+", re.UNICODE)


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            token.casefold()
            for token in _WORD.findall(value)
            if len(token) > 1
        )
    )[:12]


def _representation_digest(representation: Representation) -> str | None:
    for value in (representation.sha256, representation.tree_digest):
        normalized = str(value or "").casefold()
        if len(normalized) == 64 and all(
            character in "0123456789abcdef" for character in normalized
        ):
            return normalized
    return None


def _hit(
    chunk: DocumentChunk,
    representation: Representation,
    asset: Asset,
    *,
    source: str,
    score: float,
    rank: int,
) -> SearchHit | None:
    digest = _representation_digest(representation)
    if digest is None or not chunk.anchor:
        return None
    return SearchHit(
        document=SearchDocument(
            document_id=f"chunk:{chunk.id}",
            asset_id=str(asset.id),
            kind=chunk.chunk_kind,
            title=asset.title_zh or asset.title,
            text=chunk.text_content,
            metadata={
                "representation_id": str(representation.id),
                "representation_digest": digest,
                "current_representation_digest": digest,
                "chunk_id": str(chunk.id),
                "chunk_hash": chunk.content_hash,
                "anchor": dict(chunk.anchor),
                "parser": chunk.parser_name,
                "parser_version": chunk.parser_version,
            },
        ),
        score=score,
        rank=rank,
        source=source,
        exact_match=False,
    )


def _hydrate_remote_hits(
    session: Session,
    hits: list[SearchHit],
    digest_map: dict[str, str],
) -> list[SearchHit]:
    """Replace bounded search payloads with current PostgreSQL chunk text."""

    chunk_ids: set[UUID] = set()
    hit_chunk_ids: dict[str, UUID] = {}
    for hit in hits:
        try:
            chunk_id = UUID(str(hit.document.metadata.get("chunk_id", "")))
        except (TypeError, ValueError):
            continue
        chunk_ids.add(chunk_id)
        hit_chunk_ids[hit.document.document_id] = chunk_id
    if not chunk_ids:
        return hits

    rows = session.execute(
        select(DocumentChunk, Representation, Asset)
        .join(Representation, Representation.id == DocumentChunk.representation_id)
        .join(Asset, Asset.id == DocumentChunk.asset_id)
        .where(
            DocumentChunk.id.in_(chunk_ids),
            Asset.deleted_at.is_(None),
            Representation.deleted_at.is_(None),
        )
    )
    current = {
        chunk.id: (chunk, representation, asset)
        for chunk, representation, asset in rows
    }
    hydrated: list[SearchHit] = []
    for hit in hits:
        current_chunk_id = hit_chunk_ids.get(hit.document.document_id)
        if current_chunk_id is None:
            hydrated.append(hit)
            continue
        row = current.get(current_chunk_id)
        if row is None:
            continue
        chunk, representation, asset = row
        digest = _representation_digest(representation)
        if digest is not None:
            digest_map[str(representation.id)] = digest
        replacement = _hit(
            chunk,
            representation,
            asset,
            source=hit.source,
            score=hit.score,
            rank=hit.rank,
        )
        if replacement is None:
            continue
        hydrated.append(
            SearchHit(
                document=replacement.document,
                score=hit.score,
                rank=hit.rank,
                source=hit.source,
                exact_match=hit.exact_match,
            )
        )
    return hydrated


def retrieve_candidates(
    session: Session,
    question: str,
    *,
    asset_ids: tuple[UUID, ...] = (),
    search_runtime: SearchRuntime | None = None,
) -> tuple[UnifiedCandidates, dict[str, str]]:
    """Collect safe chunk candidates from SQL and graph neighborhoods."""

    terms = _tokens(question)
    base_statement = (
        select(DocumentChunk, Representation, Asset)
        .join(Representation, Representation.id == DocumentChunk.representation_id)
        .join(Asset, Asset.id == DocumentChunk.asset_id)
        .where(
            Asset.deleted_at.is_(None),
            Representation.deleted_at.is_(None),
        )
    )
    if asset_ids:
        base_statement = base_statement.where(DocumentChunk.asset_id.in_(asset_ids))
    statement = base_statement
    if terms:
        statement = statement.where(
            or_(
                *[
                    DocumentChunk.text_content.ilike(f"%{term}%")
                    for term in terms
                ]
            )
        )
    rows = list(session.execute(statement.limit(160)).all())
    if not rows and asset_ids:
        # A scoped “ask this paper” request should still provide evidence when
        # the natural-language question shares no literal token with the PDF.
        rows = list(session.execute(base_statement.limit(80)).all())

    keyword: list[SearchHit] = []
    structured: list[SearchHit] = []
    digest_map: dict[str, str] = {}
    question_folded = question.casefold().strip()
    for chunk, representation, asset in rows:
        digest = _representation_digest(representation)
        if digest is not None:
            digest_map[str(representation.id)] = digest
        text = chunk.text_content.casefold()
        matches = sum(term in text for term in terms)
        score = float(matches) + min(len(chunk.text_content), 2_000) / 100_000
        candidate = _hit(
            chunk,
            representation,
            asset,
            source="keyword",
            score=score,
            rank=len(keyword) + 1,
        )
        if candidate is not None:
            keyword.append(candidate)
        title = f"{asset.title} {asset.title_zh or ''}".casefold()
        if question_folded and (
            question_folded in title or title in question_folded
        ):
            exact = _hit(
                chunk,
                representation,
                asset,
                source="structured",
                score=100.0,
                rank=len(structured) + 1,
            )
            if exact is not None:
                structured.append(
                    SearchHit(
                        document=exact.document,
                        score=exact.score,
                        rank=exact.rank,
                        source=exact.source,
                        exact_match=True,
                    )
                )

    keyword.sort(key=lambda item: item.score, reverse=True)
    keyword = [
        SearchHit(
            document=item.document,
            score=item.score,
            rank=index,
            source=item.source,
            exact_match=item.exact_match,
        )
        for index, item in enumerate(keyword[:100], 1)
    ]

    graph: list[SearchHit] = []
    if asset_ids:
        neighbor_ids: set[UUID] = set()
        for relation in session.scalars(
            select(Relation).where(
                Relation.deleted_at.is_(None),
                Relation.confirmation_status != ConfirmationStatus.REJECTED.value,
                or_(
                    Relation.subject_asset_id.in_(asset_ids),
                    Relation.object_asset_id.in_(asset_ids),
                ),
            )
        ):
            if relation.subject_asset_id not in asset_ids:
                neighbor_ids.add(relation.subject_asset_id)
            if relation.object_asset_id not in asset_ids:
                neighbor_ids.add(relation.object_asset_id)
        if neighbor_ids:
            graph_rows = session.execute(
                select(DocumentChunk, Representation, Asset)
                .join(
                    Representation,
                    Representation.id == DocumentChunk.representation_id,
                )
                .join(Asset, Asset.id == DocumentChunk.asset_id)
                .where(
                    DocumentChunk.asset_id.in_(neighbor_ids),
                    Asset.deleted_at.is_(None),
                    Representation.deleted_at.is_(None),
                )
                .limit(40)
            )
            for chunk, representation, asset in graph_rows:
                digest = _representation_digest(representation)
                if digest is not None:
                    digest_map[str(representation.id)] = digest
                candidate = _hit(
                    chunk,
                    representation,
                    asset,
                    source="graph",
                    score=1.0,
                    rank=len(graph) + 1,
                )
                if candidate is not None:
                    graph.append(candidate)

    remote_keyword: list[SearchHit] = []
    remote_vector: list[SearchHit] = []
    if search_runtime is not None and question.strip():
        try:
            with search_runtime.activity():
                for index_name in ("documents", "code", "annotations"):
                    remote_keyword.extend(
                        search_runtime.suite.keyword_indexes[index_name].search(
                            question,
                            limit=100,
                        )
                    )
                embedder = search_runtime.service.embedder
                if embedder is not None:
                    query_vector = embedder.encode([question])[0]
                    remote_vector.extend(
                        search_runtime.suite.chunk_vectors.search(
                            query_vector,
                            limit=100,
                        )
                    )
        except Exception:
            remote_keyword = []
            remote_vector = []
    if asset_ids:
        allowed = {str(item) for item in asset_ids}
        remote_keyword = [
            item for item in remote_keyword if item.document.asset_id in allowed
        ]
        remote_vector = [
            item for item in remote_vector if item.document.asset_id in allowed
        ]
    remote_keyword = _hydrate_remote_hits(session, remote_keyword, digest_map)
    remote_vector = _hydrate_remote_hits(session, remote_vector, digest_map)

    return (
        UnifiedCandidates(
            structured=tuple(structured[:30]),
            keyword=tuple([*remote_keyword, *keyword][:100]),
            vector=tuple(remote_vector[:100]),
            graph=tuple(graph),
            index_versions={
                "postgres": "0003_search_rag_integrations",
                "chunks": "parser-versioned",
                "meilisearch": "v1",
                "qdrant": (
                    search_runtime.suite.chunk_collection
                    if search_runtime is not None
                    else "unavailable"
                ),
            },
        ),
        digest_map,
    )


def _pricing_catalog(path: Path) -> PricingCatalog:
    document = json.loads(path.read_text(encoding="utf-8"))
    version = str(document.get("version") or "")
    if not version:
        return PricingCatalog({})
    currency = str(document.get("currency") or "CNY")
    prices: dict[tuple[str, str], ModelPricing] = {}
    providers = document.get("providers")
    if not isinstance(providers, dict):
        return PricingCatalog({})
    for provider, models in providers.items():
        if not isinstance(models, dict):
            continue
        for model, values in models.items():
            if not isinstance(values, dict):
                continue
            prices[(str(provider), str(model))] = ModelPricing(
                input_per_million=float(values["input_per_million"]),
                output_per_million=float(values["output_per_million"]),
                currency=currency,
                version=version,
            )
    return PricingCatalog(prices)


def _budget_ledger(session: Session, settings: ApiSettings) -> BudgetLedger:
    ledger = BudgetLedger(
        BudgetPolicy(
            max_request_cost=settings.rag_task_budget_cny,
            max_daily_cost=settings.rag_daily_budget_cny,
            max_monthly_cost=settings.rag_monthly_budget_cny,
            safety_margin=0.20,
        )
    )
    now = datetime.now(UTC)
    month_start = datetime(now.year, now.month, 1, tzinfo=UTC)
    day_start = datetime(now.year, now.month, now.day, tzinfo=UTC)
    monthly = session.execute(
        select(
            func.coalesce(func.sum(ProviderUsage.input_tokens), 0),
            func.coalesce(func.sum(ProviderUsage.output_tokens), 0),
            func.coalesce(func.sum(ProviderUsage.actual_cost), 0),
        ).where(
            ProviderUsage.usage_kind == UsageKind.CHAT.value,
            ProviderUsage.occurred_at >= month_start,
        )
    ).one()
    daily_cost = session.scalar(
        select(func.coalesce(func.sum(ProviderUsage.actual_cost), 0)).where(
            ProviderUsage.usage_kind == UsageKind.CHAT.value,
            ProviderUsage.occurred_at >= day_start,
        )
    )
    ledger.month_tokens = int(monthly[0]) + int(monthly[1])
    ledger.month_cost = float(monthly[2])
    ledger.day_cost = float(daily_cost or 0)
    return ledger


def answer_with_rag(
    session: Session,
    settings: ApiSettings,
    question: str,
    *,
    asset_ids: tuple[UUID, ...] = (),
    request_id: str | None = None,
    search_runtime: SearchRuntime | None = None,
) -> RagServiceResult:
    candidates, digests = retrieve_candidates(
        session,
        question,
        asset_ids=asset_ids,
        search_runtime=search_runtime,
    )
    key = CredentialStore().get(settings.deepseek_credential_account)
    transport = HttpxCompletionTransport(timeout=60)
    deepseek = DeepSeekProviderSource(
        chat_endpoint=f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
        models_endpoint=f"{settings.deepseek_base_url.rstrip('/')}/models",
        api_key=key,
        preference=ModelPreference(primary=("pro",), fallback=("flash",)),
        transport=transport,
        discovery_transport=transport,
    )
    minimax = MiniMaxProviderSource(
        chat_endpoint="https://api.minimax.chat/v1/text/chatcompletion_v2",
        models_endpoint="https://api.minimax.chat/v1/models",
        api_key=None,
        preference=ModelPreference(primary=("pro",), fallback=("flash",)),
        transport=transport,
        discovery_transport=transport,
        enabled=False,
    )
    service = RagService(
        ProviderChain((deepseek, minimax)),
        _pricing_catalog(settings.model_pricing_path),
        budget=_budget_ledger(session, settings),
        current_digest_resolver=digests.get,
    )
    try:
        result = service.answer(
            RagRequest(
                question=question,
                candidates=candidates,
                filters={"asset_ids": [str(item) for item in asset_ids]},
                max_evidence=8,
                max_output_tokens=settings.rag_max_output_tokens,
            )
        )
    finally:
        transport.close()
    if result.answer.input_tokens or result.answer.output_tokens:
        attempts = result.audit.get("provider_attempts")
        completed = (
            attempts[-1]
            if isinstance(attempts, list)
            and attempts
            and isinstance(attempts[-1], dict)
            else {}
        )
        actual_cost = Decimal(str(completed.get("actual_cost") or "0"))
        session.add(
            ProviderUsage(
                provider=result.answer.provider,
                usage_kind=UsageKind.CHAT.value,
                model=result.answer.model,
                request_id=request_id,
                input_tokens=result.answer.input_tokens,
                output_tokens=result.answer.output_tokens,
                document_count=len(result.retrieved),
                estimated_cost=actual_cost,
                actual_cost=actual_cost,
                currency="CNY",
                privacy_scope="private",
                details={
                    "prompt_version": result.audit.get("prompt_version"),
                    "question_hash": result.audit.get("question_hash"),
                    "filters_hash": result.audit.get("filters_hash"),
                    "stop_reason": result.stop_reason,
                    "pricing_version": completed.get("pricing_version"),
                },
            )
        )
    return result
