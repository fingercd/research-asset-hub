from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from apps.api.events import record_mutation, stable_hash
from packages.domain.enums import AuditAction
from packages.domain.models import (
    Asset,
    Job,
    Relation,
    RelationEvidenceModel,
    RepositoryProfile,
    Representation,
    Source,
)
from packages.domain.serving_scope import assert_representation_active
from packages.ingestion.source_integrity import _directory_tree_digest
from packages.managed_sources.service import canonical_bytes, managed_path
from packages.repository_snapshots.download import (
    ENTRY_LIMIT,
    FILE_LIMIT,
    SNAPSHOT_SCOPE,
    TREE_LIMIT,
    SnapshotCancelled,
    SnapshotError,
    canonical_url,
    download_archive,
    extract_archive,
    public_repository,
    resolve_commit,
)


class SnapshotUnavailable(SnapshotError):
    """A missing snapshot may be restored as new, audited immutable evidence."""


def _lock(session: Session, key: str) -> None:
    if session.get_bind().dialect.name == "postgresql":
        value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        session.execute(text("SELECT pg_advisory_xact_lock(:value)"), {"value": value})


def _audit(
    session: Session,
    entity_type: str,
    entity_id: UUID,
    after: dict[str, Any],
    request_id: str | None = None,
) -> None:
    record_mutation(
        session,
        entity_type=entity_type,
        entity_id=entity_id,
        action=AuditAction.CREATE,
        before=None,
        after=after,
        request_id=request_id,
        event_type=f"{entity_type}.created",
    )


def _asset(session: Session, asset_id: UUID, expected_type: str) -> Asset:
    asset = session.get(Asset, asset_id)
    if asset is None or asset.deleted_at is not None:
        raise SnapshotError(f"The {expected_type} asset was not found or is deleted.")
    if asset.asset_type != expected_type:
        raise SnapshotError(f"The selected asset must be a {expected_type}.")
    return asset


def _reuse_snapshot(
    session: Session, representation: Representation, data_dir: Path, registry: Any
) -> dict[str, object]:
    assert_representation_active(session, representation)
    source = session.get(Source, representation.source_id)
    if source is None or not source.enabled:
        raise SnapshotError("The immutable snapshot source is unavailable or disabled.")
    if representation.availability not in {"available", "missing"}:
        raise SnapshotError(
            "The existing immutable snapshot is unavailable; it cannot be overwritten."
        )
    from packages.managed_sources.service import managed_source_origin, relocated_managed_path

    frozen = source.config
    origin = managed_source_origin(frozen)
    source_root = relocated_managed_path(source.root_path, origin, data_dir)
    manifest = managed_path(
        data_dir, *source_root.relative_to(data_dir.resolve()).parts, "manifest.json"
    )
    declaration = {key: value for key, value in frozen.items() if not key.startswith("_frozen_")}
    if (
        hashlib.sha256(canonical_bytes(declaration)).hexdigest()
        != frozen.get("_frozen_source_config_sha256")
        or not manifest.is_file()
        or hashlib.sha256(manifest.read_bytes()).hexdigest()
        != frozen.get("_frozen_manifest_sha256")
    ):
        raise SnapshotError("The existing immutable snapshot source evidence has changed.")
    selected_root = relocated_managed_path(frozen["asset_roots"]["repository"], origin, data_dir)
    relative = selected_root.relative_to(data_dir.resolve()).parts
    path = managed_path(data_dir, *relative, str(representation.path_ref))
    registered = registry.resolve(source.alias, representation.root_key, representation.path_ref)
    if registered != path:
        raise SnapshotError(
            "The registered immutable snapshot root does not match source evidence."
        )
    if not path.is_dir():
        if representation.availability != "missing":
            representation.availability = "missing"
            record_mutation(
                session,
                entity_type="representation",
                entity_id=representation.id,
                action=AuditAction.UPDATE,
                before={"availability": "available"},
                after={"availability": "missing", "asset_id": str(representation.asset_id)},
                request_id=None,
                event_type="repository-snapshot.missing",
            )
        raise SnapshotUnavailable("The existing immutable snapshot directory is missing.")
    if _directory_tree_digest(path) != representation.tree_digest:
        raise SnapshotError("The existing immutable snapshot directory has changed.")
    if path.stat().st_mtime_ns != representation.attributes.get("_source_mtime_ns"):
        raise SnapshotError("The existing immutable snapshot directory metadata has changed.")
    if representation.availability == "missing":
        representation.availability = "available"
        record_mutation(
            session,
            entity_type="representation",
            entity_id=representation.id,
            action=AuditAction.UPDATE,
            before={"availability": "missing"},
            after={"availability": "available", "asset_id": str(representation.asset_id)},
            request_id=None,
            event_type="repository-snapshot.revalidated",
        )
    return {
        "status": "reused",
        "representation_id": str(representation.id),
        "repository_asset_id": str(representation.asset_id),
        "revision": representation.revision,
        "scope": SNAPSHOT_SCOPE,
    }


@contextmanager
def _snapshot_staging(data_dir: Path, base_parts: tuple[str, ...]) -> Iterator[Path]:
    base = managed_path(data_dir, *base_parts)
    base.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=base))
    try:
        yield managed_path(data_dir, *base_parts, temporary.name)
    finally:
        # Do not recursively delete through a replaced root, symlink or junction.
        checked = managed_path(data_dir, *base_parts, temporary.name)
        shutil.rmtree(checked)


def _remember_recovery_path(database: Any, job_id: UUID | None, commit: str) -> str:
    if job_id is None:
        return f"{commit}-restored-{uuid4().hex[:12]}"
    with database.session() as session:
        job = session.get(Job, job_id, with_for_update=True)
        if job is None:
            raise SnapshotError("Repository snapshot job no longer exists.")
        existing = job.parameters.get("recovery_path_ref")
        if existing:
            return str(existing)
        path_ref = f"{commit}-restored-{uuid4().hex[:12]}"
        before = dict(job.parameters)
        job.parameters = {**before, "recovery_path_ref": path_ref}
        record_mutation(
            session,
            entity_type="job",
            entity_id=job.id,
            action=AuditAction.UPDATE,
            before=before,
            after=job.parameters,
            request_id=None,
            event_type="repository-snapshot.recovery-planned",
        )
        return path_ref


def _verify_published_tree(data_dir: Path, parts: tuple[str, ...], expected_digest: str) -> None:
    root = managed_path(data_dir, *parts)
    digest = hashlib.sha256()
    total = 0
    children = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    if len(children) > ENTRY_LIMIT:
        raise SnapshotError("Unpublished snapshot exceeds the entry limit.")
    for child in children:
        relative = child.relative_to(root)
        checked = managed_path(data_dir, *parts, *relative.parts)
        if checked.is_dir() or relative.as_posix() == ".hub-snapshot.json":
            continue
        size = checked.stat().st_size
        total += size
        if size > FILE_LIMIT or total > TREE_LIMIT:
            raise SnapshotError("Unpublished snapshot exceeds the file or tree byte limit.")
        file_digest = hashlib.sha256()
        with checked.open("rb") as handle:
            for block in iter(lambda: handle.read(64 * 1024), b""):
                file_digest.update(block)
        digest.update(f"{relative.as_posix()}\0{file_digest.hexdigest()}\n".encode())
    if digest.hexdigest() != expected_digest:
        raise SnapshotError(
            "Unpublished immutable snapshot content does not match archive evidence."
        )


def queue_snapshot(
    session: Session, repository_asset_id: UUID, revision: str | None, request_id: str | None = None
) -> Job:
    _asset(session, repository_asset_id, "repository")
    profile = session.get(RepositoryProfile, repository_asset_id)
    if profile is None or not profile.canonical_url:
        raise SnapshotError("This repository has no public URL for snapshot download.")
    public_repository(profile.canonical_url)
    if revision is not None and (not revision.strip() or len(revision) > 255 or "\0" in revision):
        raise SnapshotError("Revision must contain between 1 and 255 characters.")
    _lock(session, f"snapshot-job:{repository_asset_id}:{revision or 'HEAD'}")
    for existing in session.scalars(
        select(Job).where(
            Job.job_type == "repository.snapshot", Job.status.in_(["pending", "running"])
        )
    ):
        if (
            existing.parameters.get("repository_asset_id") == str(repository_asset_id)
            and existing.parameters.get("revision") == revision
            and not existing.cancel_requested
        ):
            return existing
    job = Job(
        job_type="repository.snapshot",
        parameters={"repository_asset_id": str(repository_asset_id), "revision": revision},
        status="pending",
        max_attempts=3,
    )
    session.add(job)
    session.flush()
    _audit(
        session,
        "job",
        job.id,
        {"job_type": job.job_type, "parameters": job.parameters, "status": job.status},
        request_id,
    )
    return job


def _managed_source(
    session: Session,
    data_dir: Path,
    *,
    alias: str,
    label: str,
    domain: str,
    root: Path,
    manifest: dict[str, Any],
    request_id: str | None = None,
) -> Source:
    from packages.managed_sources import register_managed_source
    from packages.managed_sources.service import canonical_bytes, frozen_source_config

    existing = session.scalar(select(Source).where(Source.alias == alias))
    if existing is not None:
        if (
            existing.config.get("asset_roots", {}).get("repository") != root.resolve().as_posix()
            or existing.config.get("_frozen_manifest_sha256")
            != hashlib.sha256(canonical_bytes(manifest)).hexdigest()
        ):
            raise SnapshotError("Immutable managed source evidence does not match this request.")
        return existing
    configuration = register_managed_source(
        data_dir,
        {
            "id": alias,
            "label": label,
            "domain": domain,
            "adapter": "managed_repository",
            "read_only": True,
            "asset_roots": {"repository": root.resolve().as_posix()},
        },
        manifest,
    )
    frozen = frozen_source_config(configuration)
    source = Source(
        alias=alias,
        label=label,
        adapter="managed_repository",
        root_path=configuration["root"],
        manifest_path=configuration["manifest"],
        read_only=True,
        enabled=True,
        config=frozen,
    )
    session.add(source)
    session.flush()
    _audit(
        session,
        "source",
        source.id,
        {
            "alias": alias,
            "read_only": True,
            "managed": True,
            "manifest_sha256": frozen["_frozen_manifest_sha256"],
        },
        request_id,
    )
    return source


def link_code(
    session: Session,
    *,
    paper_id: UUID,
    url: str | None,
    existing_asset_id: UUID | None,
    local_directory: str | None,
    kind: str,
    download: bool,
    revision: str | None,
    data_dir: Path,
    request_id: str | None = None,
) -> dict[str, str]:
    paper = _asset(session, paper_id, "paper")
    normalized = canonical_url(url) if url else None
    directory: Path | None = None
    if local_directory:
        directory = Path(local_directory).expanduser()
        if not directory.is_absolute() or not directory.is_dir() or directory.is_symlink():
            raise SnapshotError(
                "Choose an existing absolute local code directory; symlink roots are not supported."
            )
        directory = directory.resolve(strict=True)
    identity = normalized or (directory.as_posix() if directory else str(existing_asset_id))
    _lock(session, f"code-link:{identity}")
    repository: Asset | None
    if existing_asset_id:
        repository = _asset(session, existing_asset_id, "repository")
        profile = session.get(RepositoryProfile, repository.id)
        if normalized and profile and profile.canonical_url and profile.canonical_url != normalized:
            raise SnapshotError("The supplied URL does not match the existing repository asset.")
    else:
        profile = (
            session.scalar(
                select(RepositoryProfile).where(RepositoryProfile.canonical_url == normalized)
            )
            if normalized
            else None
        )
        repository = _asset(session, profile.asset_id, "repository") if profile else None
        if repository is None and directory is not None:
            path_hash = hashlib.sha256(directory.as_posix().encode()).hexdigest()
            local_source = session.scalar(
                select(Source).where(Source.alias == f"local-code-{path_hash[:40]}")
            )
            if local_source:
                representation = session.scalar(
                    select(Representation).where(
                        Representation.source_id == local_source.id,
                        Representation.deleted_at.is_(None),
                    )
                )
                if representation:
                    repository = _asset(session, representation.asset_id, "repository")
                    profile = session.get(RepositoryProfile, repository.id)
        if repository is None:
            repository = Asset(
                asset_type="repository",
                title=(normalized.rsplit("/", 2)[-2] + "/" + normalized.rsplit("/", 1)[-1])
                if normalized
                else Path(identity).name,
                source_urls=[normalized] if normalized else [],
                domain_slugs=list(paper.domain_slugs),
                visibility=paper.visibility,
            )
            session.add(repository)
            session.flush()
            _audit(
                session,
                "asset",
                repository.id,
                {"id": str(repository.id), "asset_type": "repository", "title": repository.title},
                request_id,
            )
    if profile is None:
        profile = RepositoryProfile(
            asset_id=repository.id,
            canonical_url=normalized,
            code_status="online" if normalized else "local",
        )
        session.add(profile)
    elif normalized and profile.canonical_url is None:
        profile.canonical_url = normalized
    if normalized and not session.scalar(
        select(Representation.id).where(
            Representation.asset_id == repository.id,
            Representation.external_url == normalized,
            Representation.materialization_kind == "url",
            Representation.deleted_at.is_(None),
        )
    ):
        representation = Representation(
            asset_id=repository.id,
            representation_type="repository",
            role="original",
            materialization_kind="url",
            availability="unknown",
            external_url=normalized,
            network_required=True,
            attributes={"source_kind": "user_declared", "link_kind": kind},
        )
        session.add(representation)
        session.flush()
        _audit(
            session,
            "representation",
            representation.id,
            {"asset_id": str(repository.id), "external_url": normalized},
            request_id,
        )
    if directory:
        path_hash = hashlib.sha256(directory.as_posix().encode()).hexdigest()
        source = _managed_source(
            session,
            data_dir,
            alias=f"local-code-{path_hash[:40]}",
            label=f"Local code · {directory.name}",
            domain=paper.domain_slugs[0] if paper.domain_slugs else "unclassified",
            root=directory,
            manifest={
                "schema_version": 1,
                "source_kind": "user_selected_directory",
                "source_record_id": path_hash,
                "directory_locator_sha256": path_hash,
            },
            request_id=request_id,
        )
        if not session.scalar(
            select(Representation.id).where(
                Representation.asset_id == repository.id,
                Representation.source_id == source.id,
                Representation.deleted_at.is_(None),
            )
        ):
            representation = Representation(
                asset_id=repository.id,
                source_id=source.id,
                representation_type="repository",
                role="original",
                materialization_kind="directory",
                availability="available",
                root_key="repository",
                path_ref=".",
                external_url=normalized,
                tree_digest=_directory_tree_digest(directory),
                network_required=False,
                verified_at=datetime.now(UTC),
                attributes={
                    "source_kind": "user_selected_directory",
                    "read_only": True,
                    "link_kind": kind,
                    "_source_mtime_ns": directory.stat().st_mtime_ns,
                },
            )
            session.add(representation)
            session.flush()
            _audit(
                session,
                "representation",
                representation.id,
                {
                    "asset_id": str(repository.id),
                    "source_id": str(source.id),
                    "path_ref": ".",
                    "read_only": True,
                },
                request_id,
            )
    relation_type = "related_to" if kind == "project" else "implemented_by"
    _lock(session, f"code-relation:{paper.id}:{repository.id}:{relation_type}")
    relation = session.scalar(
        select(Relation).where(
            Relation.subject_asset_id == paper.id,
            Relation.object_asset_id == repository.id,
            Relation.relation_type == relation_type,
        )
    )
    if relation and relation.deleted_at is not None:
        raise SnapshotError(
            "This code relation was deleted; restore it explicitly before linking again."
        )
    if relation is None:
        relation = Relation(
            subject_asset_id=paper.id,
            object_asset_id=repository.id,
            relation_type=relation_type,
            source_key="user-declared:code-link",
            confirmation_status="confirmed",
            evidence={"source_kind": "user_declared", "link_kind": kind},
            attributes={
                "source_kind": "user_declared",
                "link_kind": kind,
                "externally_verified": False,
            },
        )
        session.add(relation)
        session.flush()
        _audit(
            session,
            "relation",
            relation.id,
            {
                "subject_asset_id": str(paper.id),
                "object_asset_id": str(repository.id),
                "relation_type": relation_type,
                "link_kind": kind,
                "source_kind": "user_declared",
            },
            request_id,
        )
    evidence = {"source_kind": "user_declared", "link_kind": kind, "externally_verified": False}
    evidence_hash = stable_hash(evidence)
    if not session.scalar(
        select(RelationEvidenceModel.id).where(
            RelationEvidenceModel.relation_id == relation.id,
            RelationEvidenceModel.evidence_hash == evidence_hash,
        )
    ):
        item = RelationEvidenceModel(
            relation_id=relation.id,
            provider="user",
            source_key="user-declared:code-link",
            evidence=evidence,
            evidence_hash=evidence_hash,
        )
        session.add(item)
        session.flush()
        _audit(
            session,
            "relation_evidence",
            item.id,
            {"relation_id": str(relation.id), "evidence": evidence},
            request_id,
        )
    result = {"repository_asset_id": str(repository.id), "relation_id": str(relation.id)}
    if download:
        result["job_id"] = str(queue_snapshot(session, repository.id, revision, request_id).id)
    return result


def run_repository_snapshot(
    database: Any, settings: Any, registry: Any, parameters: dict[str, object]
) -> dict[str, object]:
    repository_id = UUID(str(parameters["repository_asset_id"]))
    data_dir = Path(settings.data_dir).expanduser().resolve()
    job_id = UUID(str(parameters["_job_id"])) if parameters.get("_job_id") else None
    requested_revision = str(parameters["revision"]) if parameters.get("revision") else None

    def check_cancel() -> None:
        if job_id:
            with database.session() as session:
                job = session.get(Job, job_id)
                if job is not None and job.cancel_requested:
                    raise SnapshotCancelled("Repository snapshot cancelled before publication.")

    with database.session() as session:
        repository_asset = _asset(session, repository_id, "repository")
        profile = session.get(RepositoryProfile, repository_id)
        if not profile or not profile.canonical_url:
            raise SnapshotError("Repository has no public archive URL.")
        public = public_repository(profile.canonical_url)
        domain = (
            repository_asset.domain_slugs[0] if repository_asset.domain_slugs else "unclassified"
        )
        if job_id:
            job = session.get(Job, job_id)
            if job and job.parameters.get("resolved_revision"):
                requested_revision = str(job.parameters["resolved_revision"])
    check_cancel()
    with httpx.Client(
        timeout=httpx.Timeout(30.0),
        headers={"User-Agent": "ResearchAssetHub/1.0", "Accept": "application/json"},
        trust_env=False,
    ) as client:
        commit = resolve_commit(client, public, requested_revision)
        if job_id:
            with database.session() as session:
                job = session.get(Job, job_id, with_for_update=True)
                if job and not job.parameters.get("resolved_revision"):
                    before = dict(job.parameters)
                    job.parameters = {**before, "resolved_revision": commit}
                    record_mutation(
                        session,
                        entity_type="job",
                        entity_id=job.id,
                        action=AuditAction.UPDATE,
                        before=before,
                        after=job.parameters,
                        request_id=None,
                        event_type="repository-snapshot.resolved",
                    )
        replaced_id: UUID | None = None
        with database.session() as session:
            existing = session.scalar(
                select(Representation)
                .where(
                    Representation.asset_id == repository_id,
                    Representation.role == "snapshot",
                    Representation.revision == commit,
                    Representation.deleted_at.is_(None),
                )
                .order_by(
                    (Representation.availability == "available").desc(),
                    Representation.created_at.desc(),
                )
            )
            if existing:
                try:
                    return _reuse_snapshot(session, existing, data_dir, registry)
                except SnapshotUnavailable:
                    replaced_id = existing.id
        base_parts = ("library", "repositories", str(repository_id))
        base = managed_path(data_dir, *base_parts)
        path_ref = _remember_recovery_path(database, job_id, commit) if replaced_id else commit
        final = managed_path(data_dir, *base_parts, path_ref)
        with _snapshot_staging(data_dir, base_parts) as stage:

            def check_stage() -> None:
                check_cancel()
                managed_path(data_dir, *base_parts, stage.name, "archive")
                managed_path(data_dir, *base_parts, stage.name, "tree")

            archive = managed_path(data_dir, *base_parts, stage.name, "archive")
            archive_digest, archive_size = download_archive(
                client, public, commit, archive, check_stage
            )
            metadata = extract_archive(
                archive, managed_path(data_dir, *base_parts, stage.name, "tree"), check_stage
            )
            metadata.update(
                {
                    "canonical_url": public.canonical_url,
                    "revision": commit,
                    "archive_sha256": archive_digest,
                    "archive_size_bytes": archive_size,
                }
            )
            check_cancel()
            with database.session() as session:
                _lock(session, f"snapshot-publish:{repository_id}:{commit}")
                _asset(session, repository_id, "repository")
                existing = session.scalar(
                    select(Representation)
                    .where(
                        Representation.asset_id == repository_id,
                        Representation.role == "snapshot",
                        Representation.revision == commit,
                        Representation.deleted_at.is_(None),
                    )
                    .order_by(
                        (Representation.availability == "available").desc(),
                        Representation.created_at.desc(),
                    )
                )
                if existing:
                    try:
                        return _reuse_snapshot(session, existing, data_dir, registry)
                    except SnapshotUnavailable:
                        replaced_id = existing.id
                        if path_ref == commit:
                            raise SnapshotError(
                                "Snapshot availability changed while downloading; "
                                "retry to plan an audited restoration."
                            ) from None
                final = managed_path(data_dir, *base_parts, path_ref)
                if final.exists():
                    evidence_path = managed_path(
                        data_dir, *base_parts, path_ref, ".hub-snapshot.json"
                    )
                    stored = json.loads(evidence_path.read_text(encoding="utf-8"))
                    if stored != metadata:
                        raise SnapshotError(
                            "An unpublished immutable snapshot already occupies this commit path "
                            "with different evidence."
                        )
                    _verify_published_tree(
                        data_dir, (*base_parts, path_ref), str(metadata["content_tree_digest"])
                    )
                else:
                    managed_path(
                        data_dir, *base_parts, stage.name, "tree", ".hub-snapshot.json"
                    ).write_text(
                        json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8"
                    )
                    os.rename(
                        managed_path(data_dir, *base_parts, stage.name, "tree"),
                        managed_path(data_dir, *base_parts, path_ref),
                    )
                tree_digest = _directory_tree_digest(final)
                source_key = hashlib.sha256((str(repository_id) + path_ref).encode()).hexdigest()[
                    :40
                ]
                source = _managed_source(
                    session,
                    Path(settings.data_dir),
                    alias=f"snapshot-{source_key}",
                    label=f"Code snapshot · {public.project} · {commit[:12]}",
                    domain=domain,
                    root=base,
                    manifest={
                        "schema_version": 1,
                        "source_kind": "public_commit_archive",
                        "source_record_id": f"{repository_id}:{path_ref}",
                        "repository_asset_id": str(repository_id),
                        "canonical_url": public.canonical_url,
                        "revision": commit,
                        "path_ref": path_ref,
                        "archive_sha256": archive_digest,
                        "tree_digest": tree_digest,
                        "scope": SNAPSHOT_SCOPE,
                    },
                )
                representation = Representation(
                    asset_id=repository_id,
                    source_id=source.id,
                    representation_type="repository",
                    role="snapshot",
                    materialization_kind="directory",
                    availability="available",
                    root_key="repository",
                    path_ref=path_ref,
                    external_url=public.canonical_url,
                    revision=commit,
                    sha256=archive_digest,
                    tree_digest=tree_digest,
                    size_bytes=metadata["size_bytes"],
                    network_required=False,
                    attributes={
                        **metadata,
                        "_source_mtime_ns": final.stat().st_mtime_ns,
                        "replaces_missing_representation_id": str(replaced_id)
                        if replaced_id
                        else None,
                    },
                    verified_at=datetime.now(UTC),
                )
                session.add(representation)
                session.flush()
                profile = session.get(RepositoryProfile, repository_id)
                profile.pinned_revision = commit
                profile.code_status = "local"
                _audit(
                    session,
                    "representation",
                    representation.id,
                    {
                        "asset_id": str(repository_id),
                        "source_id": str(source.id),
                        "revision": commit,
                        "sha256": archive_digest,
                        "scope": SNAPSHOT_SCOPE,
                        "replaces_missing_representation_id": str(replaced_id)
                        if replaced_id
                        else None,
                    },
                )
                session.add(
                    Job(
                        job_type="extract_representation",
                        parameters={"representation_id": str(representation.id)},
                        status="pending",
                        dedupe_key=f"repository-snapshot:{representation.id}:extract",
                    )
                )
                result = {
                    "status": "downloaded",
                    "representation_id": str(representation.id),
                    "repository_asset_id": str(repository_id),
                    "revision": commit,
                    **metadata,
                }
    registry.refresh_if_changed()
    return result
