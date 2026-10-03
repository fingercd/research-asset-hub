from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from apps.api.config import ApiSettings
from apps.api.database import DatabaseManager, create_database_engine
from apps.asset_gateway.registry import SourceRegistry
from apps.worker.handlers import RuntimeHandlers
from apps.worker.lease import (
    ClaimedJob,
    claim_next_job,
    fail_job,
    finish_job,
    renew_lease,
)
from apps.worker.main import LeaseHeartbeat
from packages.domain.models import (
    Activity,
    Annotation,
    Asset,
    Base,
    DocumentChunk,
    Job,
    JobAttempt,
    Representation,
    Source,
)
from packages.indexing.builder import KeywordBuildReport


@pytest.fixture
def database() -> Iterator[DatabaseManager]:
    settings = ApiSettings(
        database_url="sqlite+pysqlite:///:memory:",
        database_require_migrations=False,
    )
    engine = create_database_engine(settings)
    Base.metadata.create_all(engine)
    manager = DatabaseManager(settings, engine=engine, require_migrations=False)
    yield manager
    engine.dispose()


@pytest.mark.parametrize(
    "job_type",
    [
        "index_asset",
        "asset.index",
        "index_representation",
        "representation.index",
        "delete_asset_index",
        "asset.index-delete",
    ],
)
@pytest.mark.parametrize("code_enabled", [False, True])
def test_worker_index_jobs_use_shared_scoped_builder(
    database: DatabaseManager,
    monkeypatch: pytest.MonkeyPatch,
    job_type: str,
    code_enabled: bool,
) -> None:
    with database.session() as session:
        asset = Asset(asset_type="paper", title="Scoped worker")
        session.add(asset)
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            representation_type="markdown",
            role="original",
            availability="available",
            path_ref="paper.md",
        )
        session.add(representation)
        session.flush()
        asset_id, representation_id = asset.id, representation.id
    selected_indexes = (
        ("assets", "documents", "code", "annotations")
        if job_type in {"delete_asset_index", "asset.index-delete"}
        else ("assets", "annotations", "code")
        if code_enabled
        else ("assets", "annotations")
    )
    captured: list[dict[str, Any]] = []

    class Suite:
        closed = False

        def close(self) -> None:
            self.closed = True

    suite = Suite()

    class Builder:
        def __init__(self, actual_suite: Any) -> None:
            assert actual_suite is suite

        def refresh_keywords(self, session: Any, **kwargs: Any) -> KeywordBuildReport:
            assert session.get(Asset, asset_id) is not None
            captured.append(kwargs)
            counts = {name: 1 for name in selected_indexes}
            return KeywordBuildReport(
                index_version="v1",
                keyword_counts_before=counts,
                keyword_counts_after=counts,
                keyword_documents_upserted=counts,
                stale_keyword_documents_removed=counts,
                asset_ids=(str(asset_id),),
                operation="refresh",
                selected_indexes=selected_indexes,
            )

    def connect(**kwargs: Any) -> Suite:
        assert kwargs["index_names"] == selected_indexes
        return suite

    monkeypatch.setattr("apps.worker.handlers.RemoteIndexSuite.connect", connect)
    monkeypatch.setattr("apps.worker.handlers.IndexBuilder", Builder)
    handlers = RuntimeHandlers(
        database,
        database.settings.model_copy(update={"search_code_index_enabled": code_enabled}),
        SourceRegistry({}),
    )
    parameters: dict[str, object] = (
        {"representation_id": str(representation_id)}
        if "representation" in job_type
        else {"asset_id": str(asset_id)}
    )
    result = handlers.dispatch(job_type, parameters)
    assert captured == [{"asset_ids": (asset_id,), "index_names": selected_indexes}]
    assert result["index_version"] == "v1"
    assert result["operation"] == "refresh"
    assert suite.closed is True


def test_job_claim_renew_finish_and_retry(database: DatabaseManager) -> None:
    with database.session() as session:
        first = Job(job_type="noop", parameters={}, priority=10)
        second = Job(job_type="noop", parameters={}, priority=0)
        session.add_all([first, second])
        session.flush()
        first_id = first.id
        second_id = second.id

    with database.session() as session:
        claim = claim_next_job(session, worker_id="worker-a", lease_seconds=60)
    assert claim is not None
    assert claim.job_id == first_id

    with database.session() as session:
        assert renew_lease(
            session,
            claim,
            worker_id="worker-a",
            lease_seconds=90,
        )
    with database.session() as session:
        assert finish_job(session, claim, worker_id="worker-a") == "succeeded"

    with database.session() as session:
        second_claim = claim_next_job(session, worker_id="worker-a", lease_seconds=60)
    assert second_claim is not None
    assert second_claim.job_id == second_id
    with database.session() as session:
        state = fail_job(
            session,
            second_claim,
            worker_id="worker-a",
            error=RuntimeError("temporary"),
        )
    assert state == "pending"


def test_stale_job_lease_is_reclaimed(database: DatabaseManager) -> None:
    now = datetime.now(UTC)
    with database.session() as session:
        job = Job(
            job_type="noop",
            parameters={},
            status="running",
            attempts=1,
            lease_owner="dead-worker",
            lease_until=now - timedelta(seconds=1),
        )
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                job_id=job.id,
                attempt_number=1,
                worker_id="dead-worker",
                status="running",
            )
        )
        session.add(
            Activity(
                job_id=job.id,
                activity_type="extraction",
                tool_name="test",
                status="running",
            )
        )
        job_id = job.id

    with database.session() as session:
        claim = claim_next_job(
            session,
            worker_id="replacement",
            lease_seconds=60,
            now=now,
        )
    assert claim is not None
    assert claim.job_id == job_id
    assert claim.attempt_number == 2
    with database.session() as session:
        attempts = list(
            session.scalars(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(JobAttempt.attempt_number)
            ).all()
        )
        activity = session.scalar(select(Activity).where(Activity.job_id == job_id))
    assert [item.status for item in attempts] == ["abandoned", "running"]
    assert activity is not None
    assert activity.status == "failed"
    assert activity.result["error"]["type"] == "WorkerLeaseExpired"


def test_lease_heartbeat_retries_after_transient_database_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class SessionContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            return None

    class FakeDatabase:
        def session(self) -> SessionContext:
            return SessionContext()

    def flaky_renew(*args: Any, **kwargs: Any) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("temporary database restart")
        return False

    monkeypatch.setattr("apps.worker.main.renew_lease", flaky_renew)
    claim = ClaimedJob(
        job_id=uuid4(),
        attempt_id=uuid4(),
        job_type="noop",
        parameters={},
        attempt_number=1,
    )
    heartbeat = LeaseHeartbeat(
        FakeDatabase(),  # type: ignore[arg-type]
        claim,
        worker_id="worker-test",
        lease_seconds=90,
        interval_seconds=0.01,
    )
    with heartbeat:
        deadline = time.monotonic() + 1
        while calls < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert calls >= 2
    assert isinstance(heartbeat.last_error, ConnectionError)


def test_extraction_handler_persists_chunks_and_enqueues_index(
    database: DatabaseManager,
    tmp_path: Path,
) -> None:
    root = tmp_path / "papers"
    root.mkdir()
    markdown = root / "paper.md"
    markdown.write_text(
        "# Method\n\nThis paper introduces a reliable graph planning method.\n",
        encoding="utf-8",
    )
    with database.session() as session:
        source = Source(
            alias="demo",
            label="Demo",
            adapter="generic",
            root_path=str(root),
        )
        asset = Asset(asset_type="paper", title="Graph Planning")
        session.add_all([source, asset])
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="markdown",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="paper",
            path_ref="paper.md",
        )
        session.add(representation)
        session.flush()
        representation_id = representation.id
    settings = database.settings
    registry = SourceRegistry({("demo", "paper"): root.resolve()})
    result = RuntimeHandlers(database, settings, registry).extract_representation(
        {"representation_id": str(representation_id)}
    )
    assert result["status"] == "succeeded"
    assert isinstance(result["chunk_count"], int)
    assert result["chunk_count"] >= 1
    with database.session() as session:
        chunks = list(
            session.scalars(
                select(DocumentChunk).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )
        index_job = session.scalar(select(Job).where(Job.job_type == "index_representation"))
    assert chunks
    assert index_job is not None


def test_extraction_keeps_stable_chunk_ids_and_marks_changed_anchors_stale(
    database: DatabaseManager,
    tmp_path: Path,
) -> None:
    root = tmp_path / "stable"
    root.mkdir()
    markdown = root / "paper.md"
    markdown.write_text("# Method\n\nStable content.\n", encoding="utf-8")
    with database.session() as session:
        source = Source(
            alias="stable",
            label="Stable",
            adapter="generic",
            root_path=str(root),
        )
        asset = Asset(asset_type="paper", title="Stable paper")
        session.add_all([source, asset])
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="markdown",
            role="primary",
            materialization_kind="file",
            availability="available",
            root_key="stable",
            path_ref="paper.md",
        )
        session.add(representation)
        session.flush()
        asset_id = asset.id
        representation_id = representation.id
    registry = SourceRegistry({("stable", "stable"): root.resolve()})
    handlers = RuntimeHandlers(database, database.settings, registry)

    handlers.extract_representation({"representation_id": str(representation_id)})
    with database.session() as session:
        first_ids = set(
            session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )

    second = handlers.extract_representation({"representation_id": str(representation_id)})
    with database.session() as session:
        second_ids = set(
            session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )
        annotation = Annotation(
            asset_id=asset_id,
            representation_id=representation_id,
            body_markdown="Keep this anchor reviewable.",
            anchor={"kind": "line", "start_line": 1, "end_line": 2},
            anchor_kind="text_lines",
            anchor_digest=str(second["representation_hash"]),
        )
        session.add(annotation)
        session.flush()
        annotation_id = annotation.id
    assert first_ids == second_ids

    markdown.write_text("# Method\n\nChanged content.\n", encoding="utf-8")
    handlers.extract_representation({"representation_id": str(representation_id)})
    with database.session() as session:
        changed_ids = set(
            session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )
        changed_annotation = session.get(Annotation, annotation_id)
    assert changed_ids != second_ids
    assert changed_annotation is not None
    assert changed_annotation.anchor_status == "stale"


def test_directory_extraction_is_bounded_and_preserves_source_paths(
    database: DatabaseManager,
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    (root / "README.md").write_text("# Demo\n\nRepository overview.\n", encoding="utf-8")
    package = root / "src"
    package.mkdir()
    (package / "main.py").write_text(
        "def answer() -> int:\n    return 42\n",
        encoding="utf-8",
    )
    ignored = root / "node_modules"
    ignored.mkdir()
    (ignored / "noise.js").write_text("const secret = false;", encoding="utf-8")
    with database.session() as session:
        source = Source(
            alias="repository",
            label="Repository",
            adapter="generic",
            root_path=str(root),
        )
        asset = Asset(asset_type="repository", title="Demo repository")
        session.add_all([source, asset])
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="repository",
            role="primary",
            materialization_kind="directory",
            availability="available",
            root_key="repository",
            path_ref=".",
        )
        session.add(representation)
        session.flush()
        representation_id = representation.id
    registry = SourceRegistry({("repository", "repository"): root.resolve()})
    result = RuntimeHandlers(database, database.settings, registry).extract_representation(
        {"representation_id": str(representation_id)}
    )
    assert result["file_count"] == 2
    with database.session() as session:
        chunks = list(
            session.scalars(
                select(DocumentChunk).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )
    paths = {str((chunk.anchor or {}).get("source_path")) for chunk in chunks}
    assert {"README.md", "src/main.py"} <= paths
    assert all("node_modules" not in path for path in paths)


def test_directory_reextraction_stages_shifted_chunk_indexes(
    database: DatabaseManager,
    tmp_path: Path,
) -> None:
    root = tmp_path / "shifted-indexes"
    root.mkdir()
    (root / "b.md").write_text("# B\n\nStable B.\n", encoding="utf-8")
    (root / "c.md").write_text("# C\n\nStable C.\n", encoding="utf-8")
    with database.session() as session:
        source = Source(
            alias="shifted-indexes",
            label="Shifted indexes",
            adapter="generic",
            root_path=str(root),
        )
        asset = Asset(asset_type="repository", title="Shifted chunk indexes")
        session.add_all([source, asset])
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="repository",
            role="primary",
            materialization_kind="directory",
            availability="available",
            root_key="repository",
            path_ref=".",
        )
        session.add(representation)
        session.flush()
        representation_id = representation.id

    registry = SourceRegistry({("shifted-indexes", "repository"): root.resolve()})
    handlers = RuntimeHandlers(database, database.settings, registry)
    handlers.extract_representation({"representation_id": str(representation_id)})
    with database.session() as session:
        first_ids = set(
            session.scalars(
                select(DocumentChunk.id).where(DocumentChunk.representation_id == representation_id)
            ).all()
        )

    # This file sorts ahead of both existing files, shifting every stable
    # chunk's global index while retaining the old chunk IDs.
    (root / "a.md").write_text("# A\n\nNew leading file.\n", encoding="utf-8")
    second = handlers.extract_representation({"representation_id": str(representation_id)})

    with database.session() as session:
        shifted_chunks = list(
            session.scalars(
                select(DocumentChunk)
                .where(DocumentChunk.representation_id == representation_id)
                .order_by(DocumentChunk.chunk_index)
            ).all()
        )
    assert second["status"] == "succeeded"
    assert first_ids < {chunk.id for chunk in shifted_chunks}
    assert [chunk.chunk_index for chunk in shifted_chunks] == list(range(len(shifted_chunks)))
