"""Run the fixed 30-question, citation-first production RAG evaluation.

The JSON report deliberately excludes generated answer text and retrieved
private chunks.  It stores only hashes, counts, provider/model identifiers,
cost, latency, and citation integrity results.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager
from apps.api.rag_runtime import answer_with_rag
from apps.api.search_runtime import SearchRuntime
from packages.domain.models import Asset, DocumentChunk, Representation
from packages.rag import Citation, RagServiceResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QUESTIONS_PATH = PROJECT_ROOT / "tests" / "search-evaluation" / "rag-questions.yaml"
DEFAULT_REPORT_PATH = PROJECT_ROOT / "work" / "validation" / "rag-evaluation.json"
BLOCKING_STOP_REASONS = frozenset(
    {"no_model", "unknown_pricing", "budget_exceeded", "provider_error"}
)
BLOCKING_MESSAGES = {
    "no_model": (
        "No enabled provider model was available. Configure the credential in "
        "Windows Credential Manager and verify model discovery."
    ),
    "unknown_pricing": (
        "The discovered model has no entry in the versioned pricing catalog; "
        "generation was correctly refused."
    ),
    "budget_exceeded": (
        "The configured per-task, daily, or monthly budget gate refused generation."
    ),
    "provider_error": ("All permitted provider attempts failed; retrieval evidence was retained."),
}
_CITATION_MARKER = re.compile(r"\[citation:([^\]\s]+)\]")
_INSUFFICIENT = "资料库证据不足"


@dataclass(frozen=True, slots=True)
class RagEvaluationQuestion:
    question_id: str
    question: str
    asset_ids: tuple[UUID, ...]
    category: str


@dataclass(frozen=True, slots=True)
class RagEvaluationRecord:
    question_id: str
    question: str
    category: str
    asset_ids: tuple[str, ...]
    stop_reason: str
    provider: str
    model: str
    answer_sha256: str
    answer_characters: int
    factual_claims: int
    cited_claims: int
    citation_coverage: float
    citation_count: int
    invalid_citations: int
    invalid_reasons: tuple[str, ...]
    retrieved_count: int
    cost_cny: float
    latency_ms: float


CitationValidator = Callable[[Citation], Sequence[str]]


def _mapping(value: object, *, location: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{location} must be a mapping")
    return {str(key): item for key, item in value.items()}


def load_questions(path: Path) -> tuple[RagEvaluationQuestion, ...]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    root = _mapping(document, location="root")
    if root.get("schema_version") != 1:
        raise ValueError("RAG fixture schema_version must be 1")
    raw_questions = root.get("questions")
    if not isinstance(raw_questions, Sequence) or isinstance(raw_questions, (str, bytes)):
        raise ValueError("questions must be a sequence")
    questions: list[RagEvaluationQuestion] = []
    for index, raw_question in enumerate(raw_questions):
        item = _mapping(raw_question, location=f"questions[{index}]")
        question_id = str(item.get("id") or "").strip()
        question = str(item.get("question") or "").strip()
        category = str(item.get("category") or "").strip()
        raw_asset_ids = item.get("asset_ids")
        if not isinstance(raw_asset_ids, Sequence) or isinstance(raw_asset_ids, (str, bytes)):
            raise ValueError(f"questions[{index}].asset_ids must be a sequence")
        try:
            asset_ids = tuple(UUID(str(value)) for value in raw_asset_ids)
        except ValueError as error:
            raise ValueError(f"questions[{index}] has an invalid asset ID") from error
        if not question_id or not question or not category or not asset_ids:
            raise ValueError(f"questions[{index}] requires id, question, category and asset_ids")
        questions.append(
            RagEvaluationQuestion(
                question_id=question_id,
                question=question,
                asset_ids=asset_ids,
                category=category,
            )
        )
    if len(questions) != 30 or len({item.question_id for item in questions}) != 30:
        raise ValueError("RAG evaluation requires exactly 30 uniquely identified questions")
    return tuple(questions)


def _substantive_lines(text: str) -> tuple[str, ...]:
    if text.strip() == _INSUFFICIENT:
        return ()
    return tuple(
        line.strip()
        for line in text.splitlines()
        if _CITATION_MARKER.search(line) or len(_CITATION_MARKER.sub("", line).strip(" -*#")) >= 8
    )


def _actual_cost(result: RagServiceResult) -> float:
    attempts = result.audit.get("provider_attempts")
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return 0.0
    costs: list[float] = []
    for attempt in attempts:
        if not isinstance(attempt, Mapping):
            continue
        value = attempt.get("actual_cost")
        if value is None:
            continue
        try:
            costs.append(float(value))
        except (TypeError, ValueError):
            continue
    return sum(costs)


def inspect_result(
    question: RagEvaluationQuestion,
    result: RagServiceResult,
    *,
    latency_ms: float,
    citation_validator: CitationValidator,
) -> RagEvaluationRecord:
    """Reduce a private RAG result to release-safe deterministic measurements."""

    text = result.answer.text
    substantive = _substantive_lines(text)
    available_ids = {citation.citation_id for citation in result.answer.citations}
    invalid_reasons: list[str] = []
    cited_claims = 0
    for line in substantive:
        markers = _CITATION_MARKER.findall(line)
        if markers and all(marker in available_ids for marker in markers):
            cited_claims += 1
        else:
            invalid_reasons.append("uncited_or_unknown_marker")
    answer_markers = tuple(dict.fromkeys(_CITATION_MARKER.findall(text)))
    if answer_markers != tuple(citation.citation_id for citation in result.answer.citations):
        invalid_reasons.append("answer_citation_list_mismatch")
    for citation in result.answer.citations:
        invalid_reasons.extend(citation_validator(citation))
    if result.stop_reason == "invalid_citation":
        invalid_reasons.append("provider_citation_validation_failed")
    if result.stop_reason == "completed" and not substantive:
        invalid_reasons.append("completed_answer_has_no_factual_content")
    factual_claims = len(substantive)
    coverage = cited_claims / factual_claims if factual_claims else 1.0
    unique_invalid_reasons = tuple(sorted(set(invalid_reasons)))
    return RagEvaluationRecord(
        question_id=question.question_id,
        question=question.question,
        category=question.category,
        asset_ids=tuple(str(item) for item in question.asset_ids),
        stop_reason=result.stop_reason,
        provider=result.answer.provider,
        model=result.answer.model,
        answer_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        answer_characters=len(text),
        factual_claims=factual_claims,
        cited_claims=cited_claims,
        citation_coverage=coverage,
        citation_count=len(result.answer.citations),
        invalid_citations=len(unique_invalid_reasons),
        invalid_reasons=unique_invalid_reasons,
        retrieved_count=len(result.retrieved),
        cost_cny=_actual_cost(result),
        latency_ms=latency_ms,
    )


def aggregate_records(
    records: Sequence[RagEvaluationRecord],
    *,
    expected_question_count: int,
    backend_degraded: bool,
    blocked_reason: str | None = None,
) -> dict[str, Any]:
    factual_claims = sum(item.factual_claims for item in records)
    cited_claims = sum(item.cited_claims for item in records)
    coverage = cited_claims / factual_claims if factual_claims else 1.0
    invalid_citations = sum(item.invalid_citations for item in records)
    stop_reasons = Counter(item.stop_reason for item in records)
    providers = Counter(
        f"{item.provider}/{item.model}"
        for item in records
        if item.provider != "none" or item.model != "none"
    )
    latencies = sorted(item.latency_ms for item in records)
    p95_latency = (
        latencies[max(0, min(len(latencies) - 1, int(math_ceil_95(len(latencies)))))]
        if latencies
        else 0.0
    )
    checks = {
        "all_30_questions_attempted": len(records) == expected_question_count == 30,
        "citation_coverage_at_least_95_percent": coverage >= 0.95,
        "zero_invalid_citations": invalid_citations == 0,
        "retrieval_backends_not_degraded": not backend_degraded,
        "not_blocked": blocked_reason is None,
    }
    if blocked_reason is not None:
        status = "blocked"
    else:
        status = "passed" if all(checks.values()) else "failed"
    return {
        "status": status,
        "checks": checks,
        "metrics": {
            "attempted_questions": len(records),
            "expected_questions": expected_question_count,
            "answered_questions": stop_reasons.get("completed", 0),
            "evidence_insufficient_questions": stop_reasons.get("evidence_insufficient", 0),
            "factual_claims": factual_claims,
            "cited_claims": cited_claims,
            "citation_coverage": coverage,
            "invalid_citations": invalid_citations,
            "total_cost_cny": sum(item.cost_cny for item in records),
            "mean_latency_ms": (
                sum(item.latency_ms for item in records) / len(records) if records else 0.0
            ),
            "p95_latency_ms": p95_latency,
            "stop_reasons": dict(sorted(stop_reasons.items())),
            "provider_models": dict(sorted(providers.items())),
        },
        "blocked": (
            {
                "reason": blocked_reason,
                "message": BLOCKING_MESSAGES.get(
                    blocked_reason,
                    "The evaluation stopped before all 30 questions could run.",
                ),
            }
            if blocked_reason is not None
            else None
        ),
        "questions": [asdict(item) for item in records],
    }


def math_ceil_95(length: int) -> int:
    """Return the zero-based nearest-rank P95 index before bounds clipping."""

    import math

    return math.ceil(0.95 * length) - 1


def validate_citation_in_database(
    session: Session,
    citation: Citation,
) -> tuple[str, ...]:
    """Verify that a cited anchor still resolves to the current content snapshot."""

    reasons: list[str] = []
    try:
        chunk_id = UUID(str(citation.chunk_id))
        representation_id = UUID(str(citation.representation_id))
        asset_id = UUID(citation.asset_id)
    except (TypeError, ValueError):
        return ("citation_has_invalid_resource_id",)
    chunk = session.get(DocumentChunk, chunk_id)
    representation = session.get(Representation, representation_id)
    if chunk is None:
        return ("citation_chunk_missing",)
    if representation is None or representation.deleted_at is not None:
        return ("citation_representation_missing",)
    if chunk.asset_id != asset_id or chunk.representation_id != representation_id:
        reasons.append("citation_resource_mismatch")
    if chunk.content_hash != citation.chunk_hash:
        reasons.append("citation_chunk_hash_stale")
    actual_chunk_hash = hashlib.sha256(chunk.text_content.encode("utf-8")).hexdigest()
    if actual_chunk_hash != chunk.content_hash:
        reasons.append("database_chunk_hash_invalid")
    current_digest = str(representation.sha256 or representation.tree_digest or "").lower()
    if not current_digest or current_digest != str(citation.representation_digest).lower():
        reasons.append("citation_representation_digest_stale")
    if dict(chunk.anchor or {}) != dict(citation.anchor or {}):
        reasons.append("citation_anchor_stale")
    if not citation.url.startswith(f"/assets/{citation.asset_id}/"):
        reasons.append("citation_url_not_openable")
    return tuple(reasons)


def _backend_snapshot(runtime: SearchRuntime | None) -> dict[str, Any]:
    if runtime is None:
        return {"degraded": True, "error": "search_runtime_unavailable"}
    keyword: dict[str, Any] = {}
    for name, index in runtime.suite.keyword_indexes.items():
        health = index.health
        keyword[name] = {
            "state": health.state.value,
            "document_count": runtime.suite.meili_admin.document_count(name),
        }
    vector = {
        "assets": {
            "state": runtime.suite.asset_vectors.health.state.value,
            "collection": runtime.suite.asset_collection,
            "point_count": runtime.suite.qdrant_admin.point_count(runtime.suite.asset_collection),
        },
        "chunks": {
            "state": runtime.suite.chunk_vectors.health.state.value,
            "collection": runtime.suite.chunk_collection,
            "point_count": runtime.suite.qdrant_admin.point_count(runtime.suite.chunk_collection),
        },
    }
    degraded = (
        any(item["state"] != "healthy" for item in keyword.values())
        or any(item["state"] != "healthy" for item in vector.values())
        or any(
            int(keyword[name]["document_count"]) == 0
            for name in ("assets", "documents", "code")
        )
        or any(int(item["point_count"]) == 0 for item in vector.values())
    )
    return {"degraded": degraded, "keyword": keyword, "vector": vector}


def run_live_evaluation(
    settings: ApiSettings,
    questions: Sequence[RagEvaluationQuestion],
) -> dict[str, Any]:
    run_id = str(uuid4())
    database = DatabaseManager(settings)
    runtime: SearchRuntime | None = None
    runtime_error: str | None = None
    try:
        try:
            runtime = SearchRuntime.connect(settings)
        except Exception as error:
            runtime_error = type(error).__name__
        backend = _backend_snapshot(runtime)
        if runtime_error is not None:
            backend["runtime_error"] = runtime_error

        expected_asset_ids = {asset_id for question in questions for asset_id in question.asset_ids}
        with database.session() as session:
            present_asset_ids = set(
                session.scalars(
                    select(Asset.id).where(
                        Asset.id.in_(expected_asset_ids),
                        Asset.deleted_at.is_(None),
                    )
                ).all()
            )
            chunk_counts = {
                str(asset_id): int(count)
                for asset_id, count in session.execute(
                    select(DocumentChunk.asset_id, func.count())
                    .where(DocumentChunk.asset_id.in_(expected_asset_ids))
                    .group_by(DocumentChunk.asset_id)
                ).all()
            }
        missing_assets = sorted(str(item) for item in expected_asset_ids - present_asset_ids)
        missing_chunks = sorted(
            str(item) for item in expected_asset_ids if int(chunk_counts.get(str(item), 0)) == 0
        )
        if missing_assets or missing_chunks:
            return {
                "status": "blocked",
                "run_id": run_id,
                "generated_at": datetime.now(UTC).isoformat(),
                "backend": backend,
                "fixture_validation": {
                    "missing_assets": missing_assets,
                    "assets_without_chunks": missing_chunks,
                },
                "blocked": {
                    "reason": "fixture_assets_unavailable",
                    "message": (
                        "One or more fixed evaluation assets or their extracted chunks "
                        "are unavailable."
                    ),
                },
                "questions": [],
            }

        records: list[RagEvaluationRecord] = []
        blocked_reason: str | None = None
        for question in questions:
            with database.session() as session:
                started = perf_counter()
                result = answer_with_rag(
                    session,
                    settings,
                    question.question,
                    asset_ids=question.asset_ids,
                    request_id=f"evaluation:{run_id}:{question.question_id}",
                    search_runtime=runtime,
                )
                latency_ms = (perf_counter() - started) * 1_000
                record = inspect_result(
                    question,
                    result,
                    latency_ms=latency_ms,
                    citation_validator=lambda citation: validate_citation_in_database(
                        session, citation
                    ),
                )
            records.append(record)
            if result.stop_reason in BLOCKING_STOP_REASONS:
                blocked_reason = result.stop_reason
                break

        aggregate = aggregate_records(
            records,
            expected_question_count=len(questions),
            backend_degraded=bool(backend.get("degraded", True)),
            blocked_reason=blocked_reason,
        )
        aggregate.update(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "run_id": run_id,
                "backend": backend,
                "fixture_validation": {
                    "missing_assets": [],
                    "assets_without_chunks": [],
                },
                "privacy": {
                    "answer_text_recorded": False,
                    "retrieved_chunk_text_recorded": False,
                    "credentials_recorded": False,
                },
            }
        )
        return aggregate
    finally:
        if runtime is not None:
            runtime.close()
        database.dispose()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run 30 grounded questions through the production RAG pipeline. "
            "Provider calls remain subject to the configured hard budgets."
        )
    )
    parser.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS_PATH)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT_PATH)
    return parser.parse_args()


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


def main() -> int:
    args = _arguments()
    fixture_path: Path = args.questions
    try:
        questions = load_questions(fixture_path)
        report = run_live_evaluation(ApiSettings(), questions)
        report["fixture"] = {
            "path": str(fixture_path.resolve()),
            "sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
            "question_count": len(questions),
        }
    except Exception as error:
        report = {
            "status": "unavailable",
            "generated_at": datetime.now(UTC).isoformat(),
            "error": {
                "type": type(error).__name__,
                "message": "RAG evaluation could not reach or validate a required local component.",
            },
        }
    _write_report(args.report, report)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
