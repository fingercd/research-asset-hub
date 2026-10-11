"""Audited, ID-based Obsidian synchronization."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from packages.domain.enums import (
    AuditAction,
    IntegrationStatus,
    ReadingStatus,
    SyncConflictStatus,
    SyncStatus,
)
from packages.domain.models import (
    Annotation,
    Asset,
    AuditEvent,
    ExternalIdentifierModel,
    Integration,
    IntegrationObject,
    OutboxEvent,
    ReadingState,
    Relation,
    SyncConflict,
    SyncRun,
    SyncSnapshot,
)

from .models import three_way_merge
from .obsidian import ObsidianAssetDocument, ObsidianContentChanged, ObsidianVaultAdapter

_EDITABLE_FIELDS = {
    "title",
    "authors",
    "year",
    "venue",
    "abstract",
    "summary",
    "domains",
    "tags",
    "reading_status",
    "visibility",
}

_FILE_WRITES = "obsidian_transaction_writes"
_LOGGER = logging.getLogger(__name__)


def _remember_write(
    session: Session,
    adapter: ObsidianVaultAdapter,
    path: Path,
    original: str | None,
    written_digest: str,
) -> None:
    """Keep file compensation active until the enclosing DB transaction commits."""
    if not session.info.get("obsidian_transaction_listeners"):
        event.listen(session, "after_commit", _commit_writes)
        event.listen(session, "after_rollback", _rollback_writes)
        session.info["obsidian_transaction_listeners"] = True
    session.info.setdefault(_FILE_WRITES, []).append((adapter, path, original, written_digest))


def _commit_writes(session: Session) -> None:
    session.info.pop(_FILE_WRITES, None)


def _rollback_writes(session: Session) -> None:
    for adapter, path, original, written_digest in reversed(session.info.pop(_FILE_WRITES, [])):
        try:
            if original is None:
                current = path.read_text(encoding="utf-8-sig")
                if hashlib.sha256(current.encode("utf-8")).hexdigest() == written_digest:
                    path.unlink()
            else:
                adapter._write_content(path, original, written_digest)
        except (ObsidianContentChanged, FileNotFoundError):
            # A later user edit or deletion takes precedence over compensation.
            continue
        except OSError:
            _LOGGER.exception("Could not restore an Obsidian note after transaction rollback.")


def obsidian_sync_etag(
    session: Session,
    vault_path: Path,
    *,
    hub_directory: str = "00_Home/Research Asset Hub",
    documents: list[ObsidianAssetDocument] | None = None,
) -> str:
    """Return a digest covering both Hub row versions and external notes."""

    adapter = ObsidianVaultAdapter(vault_path, hub_directory)
    hub_versions = [
        (str(asset_id), row_version)
        for asset_id, row_version in session.execute(
            select(Asset.id, Asset.row_version).where(Asset.deleted_at.is_(None)).order_by(Asset.id)
        )
    ]
    external_versions = sorted(
        (document.asset_id, str(document.path), document.content_digest)
        for document in (adapter.scan() if documents is None else documents)
    )
    return _digest(
        {
            "hub": hub_versions,
            "external": external_versions,
            "reading_states": [
                (str(state.asset_id), state.status, state.row_version)
                for state in session.scalars(
                    select(ReadingState)
                    .where(ReadingState.user_id == "local")
                    .order_by(ReadingState.asset_id)
                )
            ],
            "snapshots": [
                (str(snapshot.id), snapshot.base_document)
                for snapshot in session.scalars(select(SyncSnapshot).order_by(SyncSnapshot.id))
            ],
            "identifiers": [
                (str(item.id), str(item.asset_id), item.scheme, item.canonical, item.is_identity)
                for item in session.scalars(
                    select(ExternalIdentifierModel).order_by(ExternalIdentifierModel.id)
                )
            ],
            "relations": [
                (str(item.id), item.row_version)
                for item in session.scalars(select(Relation).order_by(Relation.id))
            ],
            "annotations": [
                (str(item.id), item.row_version)
                for item in session.scalars(
                    select(Annotation).where(Annotation.user_id == "local").order_by(Annotation.id)
                )
            ],
            "vault_path": str(adapter.vault_path),
            "hub_directory": hub_directory,
        }
    )


def preview_obsidian_assets(
    session: Session,
    vault_path: Path,
    *,
    hub_directory: str = "00_Home/Research Asset Hub",
) -> dict[str, Any]:
    """Persist a non-mutating preview that can be validated before apply."""

    adapter = ObsidianVaultAdapter(vault_path, hub_directory)
    integration = _integration(session, adapter)
    started_at = datetime.now(UTC)
    run = SyncRun(
        integration_id=integration.id,
        status=SyncStatus.RUNNING.value,
        mode="full",
        preview_only=True,
        started_at=started_at,
    )
    session.add(run)
    session.flush()

    external_documents = {document.asset_id: document for document in adapter.scan()}
    assets = list(
        session.scalars(
            select(Asset)
            .where(Asset.deleted_at.is_(None))
            .order_by(Asset.asset_type, Asset.title, Asset.id)
        )
    )
    identifiers: dict[Any, list[ExternalIdentifierModel]] = defaultdict(list)
    for identifier in session.scalars(select(ExternalIdentifierModel)):
        identifiers[identifier.asset_id].append(identifier)
    states = {
        state.asset_id: state
        for state in session.scalars(select(ReadingState).where(ReadingState.user_id == "local"))
    }
    relation_ids: dict[Any, list[str]] = defaultdict(list)
    for relation in session.scalars(select(Relation).where(Relation.deleted_at.is_(None))):
        relation_ids[relation.subject_asset_id].append(str(relation.object_asset_id))
        relation_ids[relation.object_asset_id].append(str(relation.subject_asset_id))

    counters: dict[str, Any] = {
        "assets_seen": len(assets),
        "create": 0,
        "update_external": 0,
        "update_hub": 0,
        "unchanged": 0,
        "conflicts": 0,
    }
    for asset in assets:
        hub_fields = _asset_fields(
            asset,
            identifiers[asset.id],
            states.get(asset.id),
            sorted(set(relation_ids[asset.id])),
        )
        external = external_documents.get(str(asset.id))
        mapping = session.scalar(
            select(IntegrationObject).where(
                IntegrationObject.integration_id == integration.id,
                IntegrationObject.asset_id == asset.id,
                IntegrationObject.external_type == "markdown",
            )
        )
        latest_snapshot = None
        if mapping is not None:
            latest_snapshot = session.scalar(
                select(SyncSnapshot)
                .where(SyncSnapshot.external_object_id == mapping.id)
                .order_by(SyncSnapshot.created_at.desc(), SyncSnapshot.id.desc())
                .limit(1)
            )
        base = latest_snapshot.base_document if latest_snapshot else hub_fields
        external_fields = external.fields if external else hub_fields
        merge = three_way_merge(base, hub_fields, external_fields)
        if merge.conflicts:
            counters["conflicts"] += len(merge.conflicts)
        elif external is None:
            counters["create"] += 1
        elif external_fields != merge.merged:
            counters["update_external"] += 1
        elif hub_fields != merge.merged:
            counters["update_hub"] += 1
        else:
            counters["unchanged"] += 1

    etag = obsidian_sync_etag(
        session,
        vault_path,
        hub_directory=hub_directory,
        documents=list(external_documents.values()),
    )
    counters["etag"] = etag
    run.status = (
        SyncStatus.NEEDS_REVIEW.value if counters["conflicts"] else SyncStatus.SUCCEEDED.value
    )
    run.counters = counters
    run.finished_at = datetime.now(UTC)
    session.flush()
    return {
        "status": run.status,
        "sync_run_id": str(run.id),
        "preview_only": True,
        "etag": etag,
        "root": str(adapter.root),
        "counters": counters,
    }


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _asset_fields(
    asset: Asset,
    identifiers: list[ExternalIdentifierModel],
    state: ReadingState | None,
    relation_ids: list[str],
) -> dict[str, Any]:
    by_scheme = {item.scheme: item.canonical for item in identifiers if item.is_identity}
    return {
        "title": asset.title,
        "aliases": [asset.title_zh] if asset.title_zh else [],
        "authors": list(asset.authors or []),
        "year": asset.year,
        "venue": asset.venue,
        "doi": by_scheme.get("doi"),
        "arxiv": by_scheme.get("arxiv"),
        "domains": list(asset.domain_slugs or []),
        "tags": list(asset.tags or []),
        "reading_status": state.status if state else ReadingStatus.UNREAD.value,
        "visibility": asset.visibility,
        "relations": relation_ids,
        "abstract": asset.abstract,
        "summary": asset.summary,
        "asset_type": asset.asset_type,
        "identifiers": {
            item.scheme: item.canonical
            for item in identifiers
            if item.canonical and item.is_identity
        },
    }


def _initial_note_body(annotations: list[Annotation]) -> str:
    if not annotations:
        return ""
    lines = ["## Hub 批注", ""]
    for annotation in annotations:
        lines.extend(
            [
                f"### {annotation.title or annotation.kind}",
                "",
                annotation.body_markdown or "无",
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _apply_external_fields(
    session: Session,
    asset: Asset,
    merged: dict[str, Any],
) -> bool:
    changed = False
    field_mapping = {
        "title": "title",
        "authors": "authors",
        "year": "year",
        "venue": "venue",
        "abstract": "abstract",
        "summary": "summary",
        "domains": "domain_slugs",
        "tags": "tags",
        "visibility": "visibility",
    }
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for external_name, model_name in field_mapping.items():
        if external_name not in merged:
            continue
        value = merged[external_name]
        current = getattr(asset, model_name)
        if current != value:
            before[model_name] = current
            after[model_name] = value
            setattr(asset, model_name, value)
            changed = True

    reading_status = merged.get("reading_status")
    if reading_status in {item.value for item in ReadingStatus}:
        state = session.scalar(
            select(ReadingState).where(
                ReadingState.asset_id == asset.id,
                ReadingState.user_id == "local",
            )
        )
        current_status = state.status if state else ReadingStatus.UNREAD.value
        if current_status != reading_status:
            if state is None:
                state = ReadingState(
                    asset_id=asset.id,
                    user_id="local",
                    status=str(reading_status),
                )
                session.add(state)
            else:
                state.status = str(reading_status)
            changed = True
            before["reading_status"] = current_status
            after["reading_status"] = reading_status

    if changed:
        session.add(
            AuditEvent(
                entity_type="asset",
                entity_id=asset.id,
                action=AuditAction.UPDATE.value,
                actor="obsidian-sync",
                before=before,
                after=after,
                details={"provider": "obsidian"},
            )
        )
    return changed


def _integration(session: Session, adapter: ObsidianVaultAdapter) -> Integration:
    model = session.scalar(
        select(Integration).where(
            Integration.provider == "obsidian",
            Integration.name == "Research Asset Hub",
        )
    )
    config = {
        "vault_path": str(adapter.vault_path),
        "hub_directory": str(adapter.root.relative_to(adapter.vault_path)),
        "editable_fields": sorted(_EDITABLE_FIELDS),
    }
    if model is None:
        model = Integration(
            provider="obsidian",
            name="Research Asset Hub",
            config=config,
            enabled=True,
            status=IntegrationStatus.READY.value,
        )
        session.add(model)
        session.flush()
    else:
        model.config = config
        model.enabled = True
        model.status = IntegrationStatus.READY.value
        model.error = None
    return model


def _write_indexes(
    session: Session,
    adapter: ObsidianVaultAdapter,
    written: list[tuple[Asset, Path]],
) -> None:
    by_domain: dict[str, list[tuple[Asset, Path]]] = defaultdict(list)
    for asset, path in written:
        for domain in asset.domain_slugs or ["unclassified"]:
            by_domain[domain].append((asset, path))

    lines = [
        "---",
        "tags:",
        "  - research-asset-hub",
        "  - index",
        "---",
        "",
        "# Research Asset Hub",
        "",
        f"共 {len(written)} 项数字资产。此目录由稳定 `asset_id` 追踪，文件名可重命名。",
        "",
        "## 领域",
        "",
    ]
    for domain, values in sorted(by_domain.items()):
        lines.append(f"- [[Domains/{domain}|{domain}]]（{len(values)}）")
    lines.extend(
        [
            "",
            "## 使用说明",
            "",
            "- Frontmatter 中题名、作者、年份、领域、标签、阅读状态等字段可编辑。",
            "- `rah_asset_id`、`rah_revision`、`rah_export_digest` 与来源校验字段只读。",
            "- 两端同时修改同一字段时，平台会建立冲突记录而不是覆盖。",
            "",
        ]
    )
    adapter.root.mkdir(parents=True, exist_ok=True)
    adapter._write_content(
        adapter.root / "README.md",
        "\n".join(lines),
        None,
        partial(_remember_write, session, adapter),
    )

    for domain, values in sorted(by_domain.items()):
        domain_lines = [
            "---",
            f'domain: "{domain}"',
            "tags:",
            "  - research-asset-hub/domain",
            "---",
            "",
            f"# {domain}",
            "",
        ]
        for asset, path in sorted(values, key=lambda item: item[0].title.casefold()):
            relative = path.relative_to(adapter.root).with_suffix("").as_posix()
            domain_lines.append(f"- [[{relative}|{asset.title_zh or asset.title}]]")
        domain_lines.append("")
        domain_path = adapter.root / "Domains" / f"{domain}.md"
        domain_path.parent.mkdir(parents=True, exist_ok=True)
        adapter._write_content(
            domain_path,
            "\n".join(domain_lines),
            None,
            partial(_remember_write, session, adapter),
        )


def sync_obsidian_assets(
    session: Session,
    vault_path: Path,
    *,
    hub_directory: str = "00_Home/Research Asset Hub",
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Perform a three-way-safe sync and persist snapshots/conflicts."""

    adapter = ObsidianVaultAdapter(vault_path, hub_directory)
    integration = _integration(session, adapter)
    started_at = datetime.now(UTC)
    run = SyncRun(
        integration_id=integration.id,
        status=SyncStatus.RUNNING.value,
        mode="full",
        preview_only=False,
        started_at=started_at,
    )
    session.add(run)
    session.flush()

    external_documents: dict[str, ObsidianAssetDocument] = {}
    for document in adapter.scan():
        external_documents[document.asset_id] = document
    if (
        expected_etag is not None
        and obsidian_sync_etag(
            session,
            vault_path,
            hub_directory=hub_directory,
            documents=list(external_documents.values()),
        )
        != expected_etag
    ):
        raise ObsidianContentChanged("Hub or Obsidian content changed after preview.")

    assets = list(
        session.scalars(
            select(Asset)
            .where(Asset.deleted_at.is_(None))
            .order_by(Asset.asset_type, Asset.title, Asset.id)
        )
    )
    identifiers: dict[Any, list[ExternalIdentifierModel]] = defaultdict(list)
    for identifier in session.scalars(select(ExternalIdentifierModel)):
        identifiers[identifier.asset_id].append(identifier)
    states = {
        state.asset_id: state
        for state in session.scalars(select(ReadingState).where(ReadingState.user_id == "local"))
    }
    relation_ids: dict[Any, list[str]] = defaultdict(list)
    for relation in session.scalars(select(Relation).where(Relation.deleted_at.is_(None))):
        relation_ids[relation.subject_asset_id].append(str(relation.object_asset_id))
        relation_ids[relation.object_asset_id].append(str(relation.subject_asset_id))
    annotations: dict[Any, list[Annotation]] = defaultdict(list)
    for annotation in session.scalars(
        select(Annotation).where(
            Annotation.user_id == "local",
            Annotation.deleted_at.is_(None),
        )
    ):
        annotations[annotation.asset_id].append(annotation)

    counters = {
        "assets_seen": len(assets),
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "hub_updates": 0,
        "conflicts": 0,
    }
    written: list[tuple[Asset, Path]] = []
    for asset in assets:
        hub_fields = _asset_fields(
            asset,
            identifiers[asset.id],
            states.get(asset.id),
            sorted(set(relation_ids[asset.id])),
        )
        external = external_documents.get(str(asset.id))
        mapping = session.scalar(
            select(IntegrationObject).where(
                IntegrationObject.integration_id == integration.id,
                IntegrationObject.asset_id == asset.id,
                IntegrationObject.external_type == "markdown",
            )
        )
        latest_snapshot = None
        if mapping is not None:
            latest_snapshot = session.scalar(
                select(SyncSnapshot)
                .where(SyncSnapshot.external_object_id == mapping.id)
                .order_by(SyncSnapshot.created_at.desc(), SyncSnapshot.id.desc())
                .limit(1)
            )

        base = latest_snapshot.base_document if latest_snapshot else hub_fields
        external_fields = external.fields if external else hub_fields
        preview = three_way_merge(base, hub_fields, external_fields)
        if preview.conflicts:
            if mapping is None:
                mapping = IntegrationObject(
                    integration_id=integration.id,
                    library_scope=hub_directory,
                    external_type="markdown",
                    external_id=str(asset.id),
                    asset_id=asset.id,
                    external_path=str(external.path) if external and external.path else None,
                )
                session.add(mapping)
                session.flush()
            for conflict in preview.conflicts:
                session.add(
                    SyncConflict(
                        sync_run_id=run.id,
                        external_object_id=mapping.id,
                        entity_type="asset",
                        entity_id=asset.id,
                        field_path=f"/{conflict.field}",
                        base_value=conflict.base_value,
                        hub_value=conflict.hub_value,
                        external_value=conflict.external_value,
                        status=SyncConflictStatus.PENDING.value,
                    )
                )
                counters["conflicts"] += 1
            continue

        if external is not None and _apply_external_fields(session, asset, preview.merged):
            counters["hub_updates"] += 1
            session.flush()
            state = session.scalar(
                select(ReadingState).where(
                    ReadingState.asset_id == asset.id,
                    ReadingState.user_id == "local",
                )
            )
            if state is not None:
                states[asset.id] = state
            hub_fields = _asset_fields(
                asset,
                identifiers[asset.id],
                states.get(asset.id),
                sorted(set(relation_ids[asset.id])),
            )
        note_body = (
            external.note_body
            if external is not None
            else _initial_note_body(annotations[asset.id])
        )
        path = adapter.write(
            asset_id=str(asset.id),
            hub_revision=asset.row_version,
            fields=hub_fields,
            note_body=note_body,
            existing_path=external.path if external else None,
            expected_content_digest=external.content_digest if external else None,
            on_write=partial(_remember_write, session, adapter),
        )
        parsed = adapter.parse(path)
        if mapping is None:
            mapping = IntegrationObject(
                integration_id=integration.id,
                library_scope=hub_directory,
                external_type="markdown",
                external_id=str(asset.id),
                asset_id=asset.id,
            )
            session.add(mapping)
            session.flush()
            counters["created"] += 1
        elif external is None or external.export_digest != parsed.export_digest:
            counters["updated"] += 1
        else:
            counters["unchanged"] += 1
        mapping.external_version = parsed.export_digest
        mapping.external_hash = parsed.export_digest
        mapping.hub_version = asset.row_version
        mapping.external_path = str(path)
        mapping.remote_deleted = False
        mapping.last_synced_at = datetime.now(UTC)
        session.add(
            SyncSnapshot(
                external_object_id=mapping.id,
                sync_run_id=run.id,
                base_document=hub_fields,
                hub_revision=asset.row_version,
                external_revision=parsed.export_digest,
                hub_hash=_digest(hub_fields),
                external_hash=parsed.export_digest,
            )
        )
        written.append((asset, path))

    _write_indexes(session, adapter, written)
    finished_at = datetime.now(UTC)
    run.status = (
        SyncStatus.NEEDS_REVIEW.value if counters["conflicts"] else SyncStatus.SUCCEEDED.value
    )
    run.counters = counters
    run.finished_at = finished_at
    integration.last_sync_at = finished_at
    if counters["hub_updates"]:
        session.add(
            OutboxEvent(
                aggregate_type="integration",
                aggregate_id=integration.id,
                event_type="integration.obsidian.synced",
                payload=counters,
                dedupe_key=f"obsidian-sync:{run.id}",
            )
        )
    session.flush()
    return {
        "status": run.status,
        "sync_run_id": str(run.id),
        "root": str(adapter.root),
        "counters": counters,
    }


def apply_obsidian_conflict(session: Session, item: SyncConflict, resolved_value: Any) -> None:
    """Apply one reviewed field without accepting unrelated external edits."""
    mapping = session.get(IntegrationObject, item.external_object_id)
    integration = session.get(Integration, mapping.integration_id) if mapping else None
    asset = session.get(Asset, item.entity_id)
    if (
        mapping is None
        or integration is None
        or integration.provider != "obsidian"
        or asset is None
        or asset.deleted_at is not None
    ):
        raise ObsidianContentChanged("The Obsidian conflict target is no longer available.")
    adapter = ObsidianVaultAdapter(
        Path(integration.config["vault_path"]), integration.config["hub_directory"]
    )
    documents = [doc for doc in adapter.scan() if doc.asset_id == str(asset.id)]
    if len(documents) != 1:
        raise ObsidianContentChanged("The conflicted Obsidian note is missing or ambiguous.")
    external = documents[0]
    snapshot = session.scalar(
        select(SyncSnapshot)
        .where(SyncSnapshot.external_object_id == mapping.id)
        .order_by(SyncSnapshot.created_at.desc(), SyncSnapshot.id.desc())
        .limit(1)
    )
    identifiers = list(
        session.scalars(
            select(ExternalIdentifierModel).where(ExternalIdentifierModel.asset_id == asset.id)
        )
    )
    state = session.scalar(
        select(ReadingState).where(
            ReadingState.asset_id == asset.id, ReadingState.user_id == "local"
        )
    )
    relations = list(
        session.scalars(
            select(Relation).where(
                Relation.deleted_at.is_(None),
                (Relation.subject_asset_id == asset.id) | (Relation.object_asset_id == asset.id),
            )
        )
    )
    relation_ids = sorted(
        {
            str(rel.object_asset_id if rel.subject_asset_id == asset.id else rel.subject_asset_id)
            for rel in relations
        }
    )
    hub_fields = _asset_fields(asset, identifiers, state, relation_ids)
    base = dict(snapshot.base_document if snapshot else hub_fields)
    field = item.field_path.removeprefix("/")
    if (
        base.get(field) != item.base_value
        or hub_fields.get(field) != item.hub_value
        or external.fields.get(field) != item.external_value
    ):
        raise ObsidianContentChanged(
            "The conflicted field changed after review; sync again before choosing a value."
        )
    if field not in _EDITABLE_FIELDS and resolved_value != hub_fields.get(field):
        raise ValueError("This system field can only keep its current Hub value.")
    if field == "reading_status" and resolved_value not in {
        status.value for status in ReadingStatus
    }:
        raise ValueError("Invalid reading status.")
    if _apply_external_fields(session, asset, {field: resolved_value}):
        session.add(
            OutboxEvent(
                aggregate_type="integration",
                aggregate_id=integration.id,
                event_type="integration.obsidian.synced",
                payload={"asset_id": str(asset.id)},
                dedupe_key=f"obsidian-conflict:{item.id}",
            )
        )
    session.flush()
    fields = {**external.fields, field: resolved_value}
    path = adapter.update_fields(
        external,
        fields=fields,
        hub_revision=asset.row_version,
        on_write=partial(_remember_write, session, adapter),
    )
    parsed = adapter.parse(path)
    base[field] = resolved_value
    mapping.external_version = parsed.export_digest
    mapping.external_hash = parsed.export_digest
    mapping.hub_version = asset.row_version
    mapping.external_path = str(path)
    mapping.last_synced_at = datetime.now(UTC)
    session.add(
        SyncSnapshot(
            external_object_id=mapping.id,
            base_document=base,
            hub_revision=asset.row_version,
            external_revision=parsed.export_digest,
            hub_hash=_digest(base),
            external_hash=parsed.export_digest,
        )
    )
