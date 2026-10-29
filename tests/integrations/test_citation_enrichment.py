from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from packages.domain.identifiers import new_uuid7
from packages.domain.models import (
    Asset,
    AuditEvent,
    Base,
    ExternalIdentifierModel,
    OutboxEvent,
    Relation,
    RelationEvidenceModel,
)
from packages.integrations.citation_enrichment import (
    CitationFetchResult,
    CitationQuery,
    CitationReference,
    CrossrefCitationProvider,
    OpenAlexCitationProvider,
    enrich_citations,
)


def _session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 'citation.db'}")
    Base.metadata.create_all(engine)
    return Session(engine)


def _paper(
    session: Session,
    title: str,
    *,
    doi: str | None = None,
    arxiv: str | None = None,
) -> Asset:
    asset = Asset(id=new_uuid7(), asset_type="paper", title=title)
    session.add(asset)
    for scheme, value in (("doi", doi), ("arxiv", arxiv)):
        if value:
            session.add(
                ExternalIdentifierModel(
                    asset_id=asset.id,
                    scheme=scheme,
                    value=value,
                    canonical=value,
                    is_identity=True,
                )
            )
    session.flush()
    return asset


def test_crossref_and_openalex_contracts_are_idempotent(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = _paper(session, "Source Paper", doi="10.1000/source")
    target = _paper(session, "Target Paper", doi="10.1000/target")
    session.commit()

    crossref_requests: list[httpx.Request] = []

    def crossref_handler(request: httpx.Request) -> httpx.Response:
        crossref_requests.append(request)
        assert str(source.id) not in str(request.url)
        if b"/works/10.1000%2Ftarget" in request.url.raw_path:
            return httpx.Response(
                200,
                json={
                    "message": {
                        "DOI": "10.1000/target",
                        "title": ["Target Paper"],
                        "reference": [],
                    }
                },
            )
        assert b"/works/10.1000%2Fsource" in request.url.raw_path
        return httpx.Response(
            200,
            json={
                "message": {
                    "DOI": "10.1000/source",
                    "title": ["Source Paper"],
                    "reference": [
                        {
                            "DOI": "https://doi.org/10.1000/target",
                            "article-title": "Target Paper",
                        }
                    ],
                }
            },
        )

    openalex_requests: list[httpx.Request] = []

    def openalex_handler(request: httpx.Request) -> httpx.Response:
        openalex_requests.append(request)
        assert str(source.id) not in str(request.url)
        if "/works/doi:" in request.url.path:
            if "target" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "id": "https://openalex.org/W2",
                        "doi": "https://doi.org/10.1000/target",
                        "title": "Target Paper",
                        "referenced_works": [],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "https://openalex.org/W1",
                    "doi": "https://doi.org/10.1000/source",
                    "title": "Source Paper",
                    "referenced_works": ["https://openalex.org/W2"],
                },
            )
        assert request.url.params["filter"] == "openalex_id:W2"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W2",
                        "doi": "https://doi.org/10.1000/target",
                        "title": "Target Paper",
                        "ids": {"openalex": "https://openalex.org/W2"},
                        "locations": [],
                    }
                ]
            },
        )

    crossref = CrossrefCitationProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(crossref_handler),
            base_url="https://api.crossref.org",
        ),
        max_attempts=1,
    )
    openalex = OpenAlexCitationProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(openalex_handler),
            base_url="https://api.openalex.org",
        ),
        api_key="test-key",
        max_attempts=1,
    )
    providers = [openalex, crossref]

    first = enrich_citations(session, providers=providers)
    session.commit()
    assert first.candidate_edges_created == 1
    assert first.evidence_created == 2

    relation = session.scalar(select(Relation))
    assert relation is not None
    assert relation.subject_asset_id == source.id
    assert relation.object_asset_id == target.id
    assert relation.confirmation_status == "candidate"
    evidence = list(
        session.scalars(select(RelationEvidenceModel).order_by(RelationEvidenceModel.provider))
    )
    assert [item.provider for item in evidence] == ["crossref", "openalex"]
    assert all(item.fetched_at is not None for item in evidence)
    assert all("matched_by" in item.evidence for item in evidence)

    second = enrich_citations(session, providers=providers)
    session.commit()
    assert second.candidate_edges_created == 0
    assert second.evidence_created == 0
    assert second.evidence_existing == 2
    assert len(list(session.scalars(select(Relation)))) == 1
    assert len(list(session.scalars(select(RelationEvidenceModel)))) == 2
    assert len(list(session.scalars(select(AuditEvent)))) == 3
    assert len(list(session.scalars(select(OutboxEvent)))) == 3
    assert crossref_requests
    assert openalex_requests


class _StaticProvider:
    name = "static"

    def __init__(self, target_doi: str) -> None:
        self.target_doi = target_doi

    def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
        return CitationFetchResult(
            provider=self.name,
            source_key=query.doi,
            references=(
                CitationReference(
                    provider_reference_id=self.target_doi,
                    doi=self.target_doi,
                    evidence={"reference_doi": self.target_doi},
                ),
            ),
            fetched_at=datetime.now(UTC),
        )

    def close(self) -> None:
        pass


def test_rejected_relation_is_never_resurrected(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = _paper(session, "Rejected Source", doi="10.1000/rejected-source")
    target = _paper(session, "Rejected Target", doi="10.1000/rejected-target")
    session.add(
        Relation(
            subject_asset_id=source.id,
            object_asset_id=target.id,
            relation_type="cites",
            source_key="manual",
            confirmation_status="rejected",
        )
    )
    session.commit()

    report = enrich_citations(
        session,
        providers=[_StaticProvider("10.1000/rejected-target")],
    )
    session.commit()
    assert report.rejected_edges_skipped == 1
    assert report.candidate_edges_created == 0
    assert report.evidence_created == 0
    relation = session.scalar(select(Relation))
    assert relation is not None
    assert relation.confirmation_status == "rejected"


def test_dry_run_and_limit_do_not_write(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _paper(session, "First", doi="10.1000/first")
    _paper(session, "Second", doi="10.1000/second")
    _paper(session, "Target", doi="10.1000/target")
    session.commit()
    calls: list[UUID] = []

    class RecordingProvider(_StaticProvider):
        def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
            calls.append(query.asset_id)
            return super().fetch_references(query)

    report = enrich_citations(
        session,
        providers=[RecordingProvider("10.1000/target")],
        dry_run=True,
        limit=1,
    )
    assert report.sources_considered == 1
    assert len(calls) == 1
    assert len(list(session.scalars(select(Relation)))) == 0
    assert len(list(session.scalars(select(RelationEvidenceModel)))) == 0


def test_asset_ids_only_fetch_requested_sources_and_keep_full_match_index(tmp_path: Path) -> None:
    session = _session(tmp_path)
    source = _paper(session, "Selected Source", doi="10.1000/selected")
    unselected = _paper(session, "Unselected Source", doi="10.1000/unselected")
    _paper(session, "Target", doi="10.1000/target")
    session.commit()
    calls: list[UUID] = []

    class RecordingProvider(_StaticProvider):
        def fetch_references(self, query: CitationQuery) -> CitationFetchResult:
            calls.append(query.asset_id)
            return super().fetch_references(query)

    report = enrich_citations(
        session,
        providers=[RecordingProvider("10.1000/target")],
        asset_ids={source.id},
    )
    session.commit()

    assert report.sources_considered == 1
    assert report.provider_calls == 1
    assert calls == [source.id]
    assert unselected.id not in calls
    assert report.candidate_edges_created == 1


def test_rate_limit_retries_without_losing_the_source(tmp_path: Path) -> None:
    session = _session(tmp_path)
    _paper(session, "Retry Source", doi="10.1000/retry-source")
    _paper(session, "Retry Target", doi="10.1000/retry-target")
    session.commit()
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(
            200,
            json={
                "message": {
                    "DOI": "10.1000/retry-source",
                    "title": ["Retry Source"],
                    "reference": [{"DOI": "10.1000/retry-target"}],
                }
            },
        )

    provider = CrossrefCitationProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.crossref.org",
        ),
        max_attempts=2,
        sleep=lambda _: None,
    )
    report = enrich_citations(session, providers=[provider], limit=1)
    session.commit()
    assert attempts == 2
    assert report.status == "completed"
    assert report.candidate_edges_created == 1


def test_openalex_uses_arxiv_exact_filter_before_title_search() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["filter"] == (
            "locations.landing_page_url:https://arxiv.org/abs/2401.12345"
        )
        assert "search" not in request.url.params
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "id": "https://openalex.org/W42",
                        "title": "An arXiv Paper",
                        "referenced_works": [],
                        "locations": [{"landing_page_url": ("https://arxiv.org/abs/2401.12345")}],
                    }
                ]
            },
        )

    provider = OpenAlexCitationProvider(
        client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="https://api.openalex.org",
        ),
        max_attempts=1,
    )
    result = provider.fetch_references(
        CitationQuery(
            asset_id=new_uuid7(),
            doi=None,
            arxiv="2401.12345",
            title="An arXiv Paper",
        )
    )
    assert result.source_found
    assert result.source_key == "W42"
    assert len(requests) == 1
