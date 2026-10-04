"""Read-only attachment export and reviewed, immutable package import."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from apps.api.domain_views import annotate_presentation_domains, presentation_domains
from apps.api.events import stable_hash
from apps.api.serialization import serialize_asset
from packages.adapters.base import stable_uuid7
from packages.domain.identifiers import new_uuid7
from packages.domain.models import (
    Annotation,
    Asset,
    ExternalIdentifierModel,
    FieldClaim,
    FieldResolution,
    ImportRun,
    Job,
    PaperProfile,
    ReadingState,
    Relation,
    RepositoryProfile,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRecordVersion,
    utc_now,
)
from packages.domain.product_models import DomainDefinition
from packages.domain.serving_scope import active_representation_clause
from packages.domain.workflow_models import PackageExport, PackageImport, ReadingPosition
from packages.domain_catalog.service import domain_slugs, list_domain_definitions
from packages.ingestion.reconciliation import canonical_identifier, normalize_title
from packages.ingestion.safe_tree_policy import (
    content_tree_digest,
    existing_destination_has_reparse,
    path_has_reparse,
    safety_reason,
    scan_directory,
    sha256_file,
)
from packages.ingestion.source_integrity import _directory_tree_digest
from packages.managed_sources.service import (
    canonical_bytes,
    data_directory,
    file_lock,
    frozen_source_config,
    immutable_write,
    register_managed_source,
)
from packages.workflows.contracts import PackageExportRequest
from packages.workflows.package_contracts import (
    PackageCommitRequest,
    PackageManifest,
    PackageRepresentation,
)
from packages.workflows.service import audit

BUFFER_SIZE = 1024 * 1024
MAX_ARCHIVE_BYTES = 8 * 1024**3
MAX_EXPANDED_BYTES = 16 * 1024**3
MAX_ENTRY_BYTES = 512 * 1024**2
MAX_MANIFEST_BYTES = 32 * 1024**2
MAX_ENTRIES = 100_000
MAX_COMPRESSION_RATIO = 200
_METADATA_FIELDS = (
    "asset_type",
    "title",
    "title_zh",
    "subtitle",
    "authors",
    "year",
    "venue",
    "language",
    "abstract",
    "summary",
    "domain_slugs",
    "tags",
    "source_urls",
)
_PRIVATE_PATH = re.compile(r"(?:^[A-Za-z]:[\\/]|^/|^\\\\)")
_SECRET_KEYS = re.compile(
    r"(?:token|password|credential|secret|api.?key|authorization|cookie)", re.I
)


class PackageError(ValueError):
    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.status = status


def package_directory(settings: Any) -> Path:
    return data_directory(settings) / "library" / "packages"


def safe_archive_path(value: str) -> str:
    parts = value.split("/")
    if not value or len(value) > 2048 or value.startswith("/") or "\\" in value or "\x00" in value:
        raise PackageError("归档包含无效或越界路径")
    if any(
        part in {"", ".", ".."}
        or ":" in part
        or part.rstrip(" .") != part
        or re.fullmatch(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part, re.I)
        for part in parts
    ):
        raise PackageError("归档包含无效或越界路径")
    return PurePosixPath(value).as_posix()


def registered_package_path(settings: Any, relative: str) -> Path:
    relative = safe_archive_path(relative)
    root = package_directory(settings).resolve()
    candidate = root / relative
    unsafe, _ = existing_destination_has_reparse(root, candidate)
    if unsafe or not candidate.resolve().is_relative_to(root):
        raise PackageError("归档产物路径无效", 409)
    return candidate


def _public_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        # Credentials and signed query parameters never enter a portable archive.
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return urlunsplit((parsed.scheme, host + port, parsed.path, "", ""))
    except ValueError:
        return None


def _safe_anchor(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _safe_anchor(item)
            for key, item in value.items()
            if not _SECRET_KEYS.search(key)
            and key not in {"path", "path_ref", "root_path", "source_path"}
        }
    if isinstance(value, list):
        return [_safe_anchor(item) for item in value]
    if isinstance(value, str) and _PRIVATE_PATH.match(value):
        return None
    return value


def _metadata(session: Session, asset: Asset, include_user_state: bool) -> dict[str, Any]:
    result = {name: getattr(asset, name) for name in _METADATA_FIELDS}
    if not include_user_state:
        resolutions = session.scalars(
            select(FieldResolution).where(
                FieldResolution.entity_type == "asset",
                FieldResolution.entity_id == asset.id,
                FieldResolution.resolution_kind == "override",
            )
        )
        for resolution in resolutions:
            field = resolution.field_path.removeprefix("/")
            if field not in result:
                continue
            base = session.scalar(
                select(FieldClaim).where(
                    FieldClaim.entity_type == "asset",
                    FieldClaim.entity_id == asset.id,
                    FieldClaim.field_path == resolution.field_path,
                    FieldClaim.source_key == f"override-base:{resolution.id}",
                )
            )
            if base is not None:
                result[field] = base.value_json
    result["id"] = str(asset.id)
    result["source_urls"] = [url for value in result["source_urls"] if (url := _public_url(value))]
    result["identifiers"] = [
        {
            "scheme": item.scheme,
            "scope": item.scope,
            "value": item.value,
            "canonical": item.canonical,
            "is_identity": item.is_identity,
        }
        for item in session.scalars(
            select(ExternalIdentifierModel)
            .where(ExternalIdentifierModel.asset_id == asset.id)
            .order_by(ExternalIdentifierModel.scheme, ExternalIdentifierModel.canonical)
        )
        if not _PRIVATE_PATH.match(item.value) and not _PRIVATE_PATH.match(item.scope)
    ]
    return result


def _zip_write_file(
    archive: zipfile.ZipFile, path: Path, target: str, expected_hash: str, expected_size: int
) -> None:
    target = safe_archive_path(target)
    before = path.stat()
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as source, archive.open(target, "w", force_zip64=True) as output:
        for block in iter(lambda: source.read(BUFFER_SIZE), b""):
            digest.update(block)
            size += len(block)
            output.write(block)
    after = path.stat()
    if (
        size != expected_size
        or digest.hexdigest() != expected_hash
        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    ):
        raise PackageError("导出期间来源附件发生变化；请重试", 409)


def export_package(
    database: Any, settings: Any, registry: Any, parameters: dict[str, Any]
) -> dict[str, Any]:
    """Worker entry point. Produces and registers an immutable ZIP artifact."""
    request = PackageExportRequest.model_validate(
        {key: value for key, value in parameters.items() if not key.startswith("_")}
    )
    if not parameters.get("_job_id"):
        raise PackageError("导出必须绑定已登记的后台任务", 409)
    job_id = UUID(str(parameters["_job_id"]))
    destination = registered_package_path(settings, f"exports/{job_id}.zip")
    with database.session() as session:
        existing = session.scalar(select(PackageExport).where(PackageExport.job_id == job_id))
        if existing:
            digest, size = sha256_file(registered_package_path(settings, existing.relative_path))
            if (digest, size) != (existing.sha256, existing.size_bytes):
                raise PackageError("已登记导出产物内容变化", 409)
            return {
                "status": "succeeded",
                "job_id": str(job_id),
                "download_url": f"/api/v1/exports/{job_id}/download",
                "sha256": digest,
            }
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="package-", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with database.session() as session:
            if session.get(Job, job_id) is None:
                raise PackageError("导出任务不存在", 404)
            statement = select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.id)
            if request.asset_ids:
                statement = statement.where(Asset.id.in_(request.asset_ids))
            assets = list(session.scalars(statement))
            annotate_presentation_domains(session, assets)
            assets = [
                asset
                for asset in assets
                if (not request.domain or request.domain in presentation_domains(asset))
                and (request.include_user_state or asset.asset_type != "collection")
            ]
            if not assets:
                raise PackageError("选定范围没有可导出的资料", 404)
            if request.asset_ids and set(request.asset_ids) - {item.id for item in assets}:
                raise PackageError("选定资产失效或不在选定领域内", 409)
            ids = {item.id for item in assets}
            # Include registered repository assets related to selected papers.
            related_ids = (
                set(
                    session.scalars(
                        select(Relation.object_asset_id).where(
                            Relation.subject_asset_id.in_(ids),
                            Relation.relation_type == "implemented_by",
                            Relation.confirmation_status == "confirmed",
                            Relation.deleted_at.is_(None),
                        )
                    )
                )
                - ids
            )
            assets.extend(
                session.scalars(
                    select(Asset)
                    .where(Asset.id.in_(related_ids), Asset.deleted_at.is_(None))
                    .order_by(Asset.id)
                )
            )
            ids = {item.id for item in assets}
            if request.include_user_state:
                collection_ids = (
                    set(
                        session.scalars(
                            select(Relation.object_asset_id).where(
                                Relation.subject_asset_id.in_(ids),
                                Relation.relation_type == "belongs_to_collection",
                                Relation.deleted_at.is_(None),
                            )
                        )
                    )
                    - ids
                )
                assets.extend(
                    session.scalars(
                        select(Asset)
                        .where(
                            Asset.id.in_(collection_ids),
                            Asset.asset_type == "collection",
                            Asset.deleted_at.is_(None),
                        )
                        .order_by(Asset.id)
                    )
                )
                ids = {item.id for item in assets}
            metadata = [_metadata(session, item, request.include_user_state) for item in assets]
            aliases = {
                source.id: source.alias
                for source in session.scalars(select(Source).where(Source.enabled.is_(True)))
            }
            representations: list[dict[str, Any]] = []
            skipped: list[dict[str, str]] = []
            total_bytes, total_entries = 0, 0
            with zipfile.ZipFile(
                temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True
            ) as archive:
                for representation in session.scalars(
                    select(Representation)
                    .where(
                        Representation.asset_id.in_(ids),
                        Representation.deleted_at.is_(None),
                        active_representation_clause(),
                    )
                    .order_by(Representation.id)
                ):
                    row = {
                        "id": str(representation.id),
                        "asset_id": str(representation.asset_id),
                        "representation_type": representation.representation_type,
                        "role": representation.role,
                        "materialization_kind": representation.materialization_kind,
                        "external_url": _public_url(representation.external_url),
                        "entrypoint": representation.entrypoint,
                        "revision": representation.revision,
                        "sha256": representation.sha256,
                        "tree_digest": representation.tree_digest,
                        "mime_type": representation.mime_type,
                        "files": [],
                    }
                    if row["entrypoint"]:
                        row["entrypoint"] = safe_archive_path(row["entrypoint"])
                    copy_started = False
                    if not request.include_external_files:
                        row["omission_reason"] = "files_not_requested"
                    elif representation.path_ref and representation.source_id in aliases:
                        try:
                            source_path = registry.resolve(
                                aliases[representation.source_id],
                                representation.root_key,
                                representation.path_ref,
                            )
                            unsafe, _ = path_has_reparse(source_path.parent, source_path)
                            if unsafe:
                                raise PackageError("附件使用链接或特殊路径")
                            if source_path.is_dir():
                                local_skipped: list[dict[str, str]] = []
                                errors: list[dict[str, str]] = []
                                ledger = scan_directory(
                                    source_path, source_path, local_skipped, errors
                                )
                                skipped.extend(
                                    {
                                        "representation_id": str(representation.id),
                                        "relative_path": item["relative_path"],
                                        "reason": item["reason"],
                                    }
                                    for item in local_skipped
                                )
                                if errors:
                                    raise PackageError("附件目录存在不可读取文件")
                                row["tree_digest"] = content_tree_digest(ledger)
                            elif source_path.is_file():
                                reason = safety_reason(source_path, is_directory=False)
                                if reason:
                                    raise PackageError("附件文件属于排除范围")
                                digest, size = sha256_file(source_path)
                                if representation.sha256 and digest != representation.sha256:
                                    raise PackageError("附件摘要与登记证据不一致")
                                ledger = [
                                    {
                                        "relative_path": source_path.name,
                                        "source_path": str(source_path),
                                        "sha256": digest,
                                        "size_bytes": size,
                                    }
                                ]
                                row["sha256"] = digest
                            else:
                                raise PackageError("附件不可读取")
                            for file in ledger:
                                total_entries += 1
                                total_bytes += file["size_bytes"]
                                if (
                                    total_entries > MAX_ENTRIES
                                    or total_bytes > MAX_EXPANDED_BYTES
                                    or file["size_bytes"] > MAX_ENTRY_BYTES
                                ):
                                    raise PackageError("选定附件超过领域包大小限制", 413)
                                target = f"attachments/{representation.id}/{file['relative_path']}"
                                copy_started = True
                                _zip_write_file(
                                    archive,
                                    Path(file["source_path"]),
                                    target,
                                    file["sha256"],
                                    file["size_bytes"],
                                )
                                row["files"].append(
                                    {
                                        "path": target,
                                        "sha256": file["sha256"],
                                        "size_bytes": file["size_bytes"],
                                    }
                                )
                        except (OSError, ValueError) as error:
                            if copy_started:
                                raise PackageError(
                                    "附件复制失败或来源内容在导出期间变化；请重试", 409
                                ) from error
                            if isinstance(error, PackageError) and error.status == 413:
                                raise
                            # Missing or stale declared evidence remains visible in the manifest.
                            row["omission_reason"] = "attachment_unavailable"
                    elif representation.path_ref:
                        row["omission_reason"] = "source_unavailable"
                    representations.append(row)
                relations = [
                    {
                        "id": str(item.id),
                        "subject_asset_id": str(item.subject_asset_id),
                        "object_asset_id": str(item.object_asset_id),
                        "relation_type": item.relation_type,
                        "confirmation_status": item.confirmation_status,
                        "confidence": item.confidence,
                    }
                    for item in session.scalars(
                        select(Relation)
                        .where(
                            Relation.subject_asset_id.in_(ids),
                            Relation.object_asset_id.in_(ids),
                            Relation.deleted_at.is_(None),
                        )
                        .order_by(Relation.id)
                    )
                ]
                slugs = {slug for item in metadata for slug in item["domain_slugs"]}
                domains = [
                    {
                        key: item.get(key)
                        for key in ("slug", "label", "description", "aliases", "tags")
                    }
                    for item in list_domain_definitions(session, settings, include_hidden=True)
                    if item["slug"] in slugs
                ]
                state = None
                if request.include_user_state:
                    representation_ids = {UUID(item["id"]) for item in representations}
                    state = {
                        "reading_states": [
                            {"asset_id": str(item.asset_id), "status": item.status}
                            for item in session.scalars(
                                select(ReadingState).where(
                                    ReadingState.asset_id.in_(ids), ReadingState.user_id == "local"
                                )
                            )
                        ],
                        "reading_positions": [
                            {
                                "asset_id": str(item.asset_id),
                                "representation_id": str(item.representation_id),
                                "page": item.page,
                                "source_hash": item.source_hash,
                            }
                            for item in session.scalars(
                                select(ReadingPosition).where(
                                    ReadingPosition.asset_id.in_(ids),
                                    ReadingPosition.user_id == "local",
                                )
                            )
                            if item.representation_id in representation_ids
                        ],
                        "annotations": [
                            {
                                "id": str(item.id),
                                "asset_id": str(item.asset_id),
                                "representation_id": str(item.representation_id)
                                if item.representation_id in representation_ids
                                else None,
                                "kind": item.kind,
                                "title": item.title,
                                "summary": item.summary,
                                "body_markdown": item.body_markdown,
                                "anchor": _safe_anchor(item.anchor),
                                "anchor_kind": item.anchor_kind,
                                "anchor_status": item.anchor_status,
                                "pinned": item.pinned,
                            }
                            for item in session.scalars(
                                select(Annotation)
                                .where(
                                    Annotation.asset_id.in_(ids), Annotation.deleted_at.is_(None)
                                )
                                .order_by(Annotation.id)
                            )
                        ],
                    }
                manifest = PackageManifest.model_validate(
                    {
                        "assets": metadata,
                        "representations": representations,
                        "relations": relations,
                        "domains": domains,
                        "user_state": state,
                        "skipped_files": skipped,
                    }
                )
                document = manifest.model_dump(mode="json", exclude_none=True)
                encoded = canonical_bytes(document)
                if len(encoded) > MAX_MANIFEST_BYTES:
                    raise PackageError("归档元数据超过清单大小限制", 413)
                archive.writestr("manifest.json", encoded)
            digest, size = sha256_file(temporary)
            if size > MAX_ARCHIVE_BYTES:
                raise PackageError("生成归档超过上传大小限制", 413)
            with file_lock(package_directory(settings) / "exports.lock"):
                if destination.exists():
                    if sha256_file(destination) != (digest, size):
                        raise PackageError("同一导出任务已有不同产物", 409)
                else:
                    os.link(temporary, destination)
                artifact = PackageExport(
                    job_id=job_id,
                    relative_path=f"exports/{job_id}.zip",
                    sha256=digest,
                    size_bytes=size,
                    manifest=document,
                )
                session.add(artifact)
                session.flush()
                audit(
                    session,
                    artifact,
                    "package_export",
                    None,
                    {"job_id": str(job_id), "sha256": digest, "size_bytes": size},
                    None,
                )
            return {
                "status": "succeeded",
                "job_id": str(job_id),
                "download_url": f"/api/v1/exports/{job_id}/download",
                "sha256": digest,
                "asset_count": len(assets),
                "omitted_representations": sum(
                    bool(item.get("omission_reason")) for item in representations
                ),
            }
    finally:
        temporary.unlink(missing_ok=True)


def inspect_package(path: Path) -> PackageManifest:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise PackageError("领域包超过上传大小限制", 413)
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ENTRIES + 1:
                raise PackageError("归档文件数超过限制", 413)
            paths, folded, total = {}, set(), 0
            for info in infos:
                name = safe_archive_path(info.filename)
                if name in paths or name.casefold() in folded:
                    raise PackageError("归档包含重复或大小写冲突路径")
                mode = (info.external_attr >> 16) & 0xFFFF
                if (
                    info.is_dir()
                    or stat.S_ISLNK(mode)
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                    or info.flag_bits & 1
                ):
                    raise PackageError("归档只允许未加密的普通文件")
                if name != "manifest.json" and not name.startswith("attachments/"):
                    raise PackageError("归档包含未声明文件")
                if info.file_size > (
                    MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_ENTRY_BYTES
                ):
                    raise PackageError("归档单文件超过大小限制", 413)
                total += info.file_size
                if total > MAX_EXPANDED_BYTES or (
                    info.file_size > 1024**2
                    and info.file_size / max(info.compress_size, 1) > MAX_COMPRESSION_RATIO
                ):
                    raise PackageError("归档膨胀比例或总大小超过限制", 413)
                paths[name] = info
                folded.add(name.casefold())
            if "manifest.json" not in paths:
                raise PackageError("归档缺少 manifest.json")
            manifest = PackageManifest.model_validate(json.loads(archive.read("manifest.json")))
            declared = {
                file.path: file
                for representation in manifest.representations
                for file in representation.files
            }
            if set(paths) != {"manifest.json", *declared}:
                raise PackageError("归档文件与 manifest 清单不一致")
            for name, declaration in declared.items():
                info = paths[name]
                if info.file_size != declaration.size_bytes:
                    raise PackageError("归档附件大小校验失败")
                digest, size = hashlib.sha256(), 0
                with archive.open(info) as handle:
                    for block in iter(lambda: handle.read(BUFFER_SIZE), b""):
                        size += len(block)
                        if size > declaration.size_bytes:
                            raise PackageError("归档附件膨胀超过声明大小", 413)
                        digest.update(block)
                if digest.hexdigest() != declaration.sha256:
                    raise PackageError("归档附件 SHA-256 校验失败")
                if any(
                    safety_reason(Path(part), is_directory=index < len(name.split("/")) - 1)
                    for index, part in enumerate(name.split("/"))
                ):
                    raise PackageError("归档附件包含敏感文件或排除目录")
            for asset in manifest.assets:
                for identifier in asset.identifiers:
                    if (
                        canonical_identifier(identifier.scheme, identifier.value).split(":", 1)[-1]
                        != identifier.canonical
                    ):
                        raise PackageError("归档标识的原值和规范值不一致")
                if any(_public_url(url) != url for url in asset.source_urls):
                    raise PackageError("来源 URL 包含无效地址、凭据或查询参数")
            for representation in manifest.representations:
                if representation.files and representation.materialization_kind in {
                    "directory",
                    "bundle",
                }:
                    ledger = [
                        {
                            "relative_path": PurePosixPath(file.path)
                            .relative_to(f"attachments/{representation.id}")
                            .as_posix(),
                            "size_bytes": file.size_bytes,
                            "sha256": file.sha256,
                        }
                        for file in representation.files
                    ]
                    if representation.tree_digest != content_tree_digest(ledger):
                        raise PackageError("归档目录摘要与逐文件证据不一致")
                if (
                    representation.external_url
                    and _public_url(representation.external_url) != representation.external_url
                ):
                    raise PackageError("附件 URL 包含无效地址、凭据或查询参数")
                if representation.entrypoint:
                    safe_archive_path(representation.entrypoint)
            return manifest
    except PackageError:
        raise
    except (
        OSError,
        zipfile.BadZipFile,
        RuntimeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
    ) as error:
        raise PackageError("领域包不是有效、可校验的 v1 归档") from error


def preview_package(session: Session, manifest: PackageManifest) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    assets = list(session.scalars(select(Asset).where(Asset.deleted_at.is_(None))))
    by_id = {item.id: item for item in assets}
    for item in manifest.assets:
        strong: set[UUID] = set()
        for identifier in item.identifiers:
            if identifier.is_identity:
                strong.update(
                    session.scalars(
                        select(ExternalIdentifierModel.asset_id).where(
                            ExternalIdentifierModel.scheme == identifier.scheme,
                            ExternalIdentifierModel.scope == identifier.scope,
                            ExternalIdentifierModel.canonical == identifier.canonical,
                            ExternalIdentifierModel.is_identity.is_(True),
                        )
                    )
                )
        same_id = by_id.get(item.id)
        entry: dict[str, Any] = {
            "id": str(item.id),
            "title": item.title,
            "action": "create",
            "target_asset_id": None,
            "candidates": [],
        }
        if len(strong) > 1 or (same_id and strong and same_id.id not in strong):
            entry["action"] = "conflict"
        elif same_id and (
            same_id.asset_type != item.asset_type
            or normalize_title(same_id.title) != normalize_title(item.title)
        ):
            entry["action"] = "conflict"
        elif same_id or strong:
            target = same_id.id if same_id else next(iter(strong))
            if target not in by_id or by_id[target].asset_type != item.asset_type:
                entry["action"] = "conflict"
            else:
                entry.update(action="reuse", target_asset_id=str(target))
        else:
            weak = [
                asset
                for asset in assets
                if asset.asset_type == item.asset_type
                and asset.year == item.year
                and normalize_title(asset.title) == normalize_title(item.title)
            ]
            if weak:
                entry.update(
                    action="needs_review",
                    candidates=[
                        {"id": str(asset.id), "title": asset.title, "year": asset.year}
                        for asset in weak
                    ],
                )
        entries.append(entry)
    return {
        "schema_version": 1,
        "assets": entries,
        "counts": dict(Counter(item["action"] for item in entries)),
        "attachment_count": sum(len(item.files) for item in manifest.representations),
        "attachment_bytes": sum(
            file.size_bytes for item in manifest.representations for file in item.files
        ),
        "omitted_representations": sum(
            bool(item.omission_reason) for item in manifest.representations
        ),
        "has_user_state": manifest.user_state is not None,
    }


def import_payload(item: PackageImport) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "import_id": str(item.id),
        "preview_id": str(item.id),
        "status": item.status,
        "preview": item.preview,
        "result": item.result,
        "row_version": item.row_version,
    }


def stage_package(
    session: Session, settings: Any, path: Path, filename: str, digest: str, request_id: str | None
) -> PackageImport:
    old = session.scalar(select(PackageImport).where(PackageImport.sha256 == digest))
    if old is not None:
        return old
    manifest = inspect_package(path)
    semantic_digest = stable_hash(manifest.model_dump(mode="json", exclude_none=True))
    for candidate in session.scalars(select(PackageImport)):
        if stable_hash(candidate.manifest) == semantic_digest:
            return candidate
    relative = f"staging/{digest}.zip"
    destination = registered_package_path(settings, relative)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(package_directory(settings) / "imports.lock"):
        if not destination.exists():
            os.link(path, destination)
        elif sha256_file(destination)[0] != digest:
            raise PackageError("已暂存归档内容发生变化", 409)
    item = PackageImport(
        sha256=digest,
        filename=Path(filename).name[:255],
        relative_path=relative,
        manifest=manifest.model_dump(mode="json", exclude_none=True),
        preview=preview_package(session, manifest),
        status="preview",
    )
    session.add(item)
    session.flush()
    audit(session, item, "package_import", None, import_payload(item), request_id)
    return item


def _extract_snapshot(settings: Any, package: PackageImport, manifest: PackageManifest) -> Path:
    source = registered_package_path(settings, package.relative_path)
    if sha256_file(source)[0] != package.sha256:
        raise PackageError("暂存归档内容发生变化", 409)
    # Revalidate immediately before publication, including hashes and ZIP bounds.
    if inspect_package(source).model_dump(mode="json", exclude_none=True) != package.manifest:
        raise PackageError("暂存归档清单发生变化", 409)
    snapshot = registered_package_path(settings, f"snapshots/{package.sha256}")
    snapshot.mkdir(parents=True, exist_ok=True)
    with (
        file_lock(package_directory(settings) / "imports.lock"),
        zipfile.ZipFile(source) as archive,
    ):
        for representation in manifest.representations:
            for file in representation.files:
                target = snapshot / safe_archive_path(file.path)
                unsafe, _ = existing_destination_has_reparse(snapshot, target)
                if unsafe:
                    raise PackageError("托管快照包含链接或越界路径", 409)
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if sha256_file(target) != (file.sha256, file.size_bytes):
                        raise PackageError("已登记快照不可覆盖", 409)
                    continue
                temporary = target.with_name(".package-" + uuid4().hex + ".tmp")
                try:
                    digest, size = hashlib.sha256(), 0
                    with (
                        archive.open(file.path) as source_file,
                        temporary.open("xb") as destination,
                    ):
                        for block in iter(lambda: source_file.read(BUFFER_SIZE), b""):
                            digest.update(block)
                            size += len(block)
                            if size > file.size_bytes:
                                raise PackageError("附件大小在快照提交前发生变化", 409)
                            destination.write(block)
                        destination.flush()
                        os.fsync(destination.fileno())
                    if (digest.hexdigest(), size) != (file.sha256, file.size_bytes):
                        raise PackageError("附件摘要在快照提交前发生变化", 409)
                    os.link(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
        immutable_write(snapshot / "manifest.json", canonical_bytes(package.manifest))
    return snapshot


def _existing_representation(
    session: Session, target: UUID, item: PackageRepresentation
) -> Representation | None:
    if not item.files and not item.external_url:
        return None
    if item.files and item.materialization_kind == "file":
        identity = Representation.sha256 == item.files[0].sha256
    elif item.tree_digest:
        identity = or_(
            Representation.tree_digest == item.tree_digest,
            Representation.attributes["package_content_tree_digest"].as_string()
            == item.tree_digest,
        )
    elif not item.files:
        identity = Representation.external_url == item.external_url
    else:
        return None
    return session.scalar(
        select(Representation).where(
            Representation.asset_id == target,
            Representation.representation_type == item.representation_type.value,
            Representation.role == item.role.value,
            Representation.deleted_at.is_(None),
            identity,
            active_representation_clause(),
        )
    )


def commit_package(
    session: Session,
    settings: Any,
    package: PackageImport,
    request: PackageCommitRequest,
    request_id: str | None,
) -> dict[str, Any]:
    if package.status == "committed":
        return package.result or {}
    manifest = PackageManifest.model_validate(package.manifest)
    fresh_preview = preview_package(session, manifest)
    package_assets = {item.id: item for item in manifest.assets}
    if set(request.identity_mapping) - set(package_assets):
        raise PackageError("身份映射包含归档范围外的资产")
    mappings, created = {}, set()
    for entry in fresh_preview["assets"]:
        original_id = UUID(entry["id"])
        item = package_assets[original_id]
        if entry["action"] == "conflict":
            raise PackageError("归档强标识或资产身份冲突，需先更正资料", 409)
        if original_id in request.identity_mapping:
            target = request.identity_mapping[original_id]
            if entry["action"] == "reuse" and target != UUID(entry["target_asset_id"]):
                raise PackageError("强标识匹配不可改为其他资产或新建", 409)
            if target is not None:
                asset = session.get(Asset, target, with_for_update=True)
                if (
                    asset is None
                    or asset.deleted_at is not None
                    or asset.asset_type != item.asset_type
                ):
                    raise PackageError("选定身份映射目标不可用", 409)
                candidates = {UUID(candidate["id"]) for candidate in entry["candidates"]}
                if entry["action"] == "needs_review" and target not in candidates:
                    raise PackageError("请从预览中的同名候选选择身份映射", 409)
                if entry["action"] == "create":
                    raise PackageError("没有匹配证据的新资料不可映射到任意已有资产", 409)
        elif entry["action"] == "needs_review":
            raise PackageError("同名同年份候选需明确选择已有资料或创建新资料", 409)
        else:
            target = UUID(entry["target_asset_id"]) if entry["target_asset_id"] else None
        if target is None:
            # UUID7 remains the native operational identity; an archive ID is an interchange key.
            target = stable_uuid7("package-" + package.sha256[:48], str(original_id))
            created.add(target)
        mappings[original_id] = target
    if len(set(mappings.values())) != len(mappings):
        raise PackageError("多个归档对象不能映射到同一资产", 409)
    snapshot = _extract_snapshot(settings, package, manifest)
    known_domains = domain_slugs(session, settings.taxonomy_config_path)
    definitions = {item.slug: item for item in manifest.domains}
    required_domains = {slug for item in manifest.assets for slug in item.domain_slugs}
    if required_domains - known_domains - set(definitions):
        raise PackageError("归档缺少所需领域定义")
    if not required_domains:
        from packages.workflows.package_contracts import PackageDomain

        required_domains.add("package-assets")
        definitions["package-assets"] = PackageDomain(slug="package-assets", label="归档资料")
    for slug in sorted(required_domains - known_domains):
        domain = DomainDefinition(**definitions[slug].model_dump())
        session.add(domain)
        session.flush()
        # Domain keys are strings; use the package UUID for the audit aggregate.
        audit(
            session,
            package,
            "domain_definition",
            None,
            definitions[slug].model_dump(mode="json"),
            request_id,
        )
    records: list[dict[str, Any]] = []
    for item in manifest.assets:
        rows: list[dict[str, Any]] = []
        for representation in manifest.representations:
            if representation.asset_id != item.id:
                continue
            value = representation.model_dump(mode="json", exclude_none=True)
            duplicate = _existing_representation(session, mappings[item.id], representation)
            if duplicate is not None:
                value["reuse_representation_id"] = str(duplicate.id)
            if representation.files:
                value["path_ref"] = (
                    f"attachments/{representation.id}"
                    if representation.materialization_kind in {"directory", "bundle"}
                    else representation.files[0].path
                )
                if representation.materialization_kind in {"directory", "bundle"}:
                    value["package_content_tree_digest"] = representation.tree_digest
                    value["tree_digest"] = _directory_tree_digest(snapshot / value["path_ref"])
            rows.append(value)
        records.append(
            {
                "id": str(item.id),
                "target_asset_id": str(mappings[item.id]),
                "asset": item.model_dump(mode="json", exclude_none=True),
                "representations": rows,
                "relations": [],
            }
        )
        for relation in manifest.relations:
            if relation.subject_asset_id != item.id:
                continue
            value = relation.model_dump(mode="json")
            reused_relation = session.scalar(
                select(Relation).where(
                    Relation.subject_asset_id == mappings[relation.subject_asset_id],
                    Relation.object_asset_id == mappings[relation.object_asset_id],
                    Relation.relation_type == relation.relation_type.value,
                    Relation.deleted_at.is_(None),
                )
            )
            if reused_relation is not None:
                value["reuse_relation_id"] = str(reused_relation.id)
            records[-1]["relations"].append(value)
    source_manifest = {"schema_version": 1, "items": records}
    source_id = "package-" + package.sha256[:48]
    declaration = register_managed_source(
        data_directory(settings),
        {
            "id": source_id,
            "label": "领域包 · " + package.filename[:200],
            "domain": sorted(required_domains)[0],
            "adapter": "domain_package",
            "asset_roots": {"attachments": str(snapshot)},
            "options": {"record_selector": "items"},
        },
        source_manifest,
    )
    source = session.scalar(select(Source).where(Source.alias == source_id))
    if source is not None:
        raise PackageError("归档来源已登记但预览未提交；请恢复该导入记录", 409)
    source = Source(
        alias=source_id,
        label=declaration["label"],
        adapter="domain_package",
        root_path=declaration["root"],
        manifest_path="manifest.json",
        read_only=True,
        config=frozen_source_config(declaration),
    )
    session.add(source)
    session.flush()
    now = utc_now()
    run = ImportRun(
        source_id=source.id,
        status="succeeded",
        mode="domain_package",
        dry_run=False,
        adapter_name="domain_package",
        adapter_version="1",
        started_at=now,
        finished_at=now,
        config_digest=source.config["_frozen_source_config_sha256"],
        counters={
            "assets_created": len(created),
            "assets_reused": len(mappings) - len(created),
            "source_records_created": len(records),
        },
    )
    session.add(run)
    session.flush()
    record_models: dict[UUID, SourceRecord] = {}
    for item, raw in zip(manifest.assets, records, strict=True):
        target = mappings[item.id]
        if target in created:
            values = item.model_dump(exclude={"id", "identifiers"})
            asset = Asset(id=target, **values)
            session.add(asset)
            session.flush()
            if asset.asset_type == "paper":
                session.add(PaperProfile(asset_id=asset.id))
            audit(session, asset, "asset", None, serialize_asset(asset), request_id)
        else:
            asset = session.get_one(Asset, target, with_for_update=True)
        if asset.asset_type == "repository" and session.get(RepositoryProfile, target) is None:
            from packages.ingestion.formal import canonical_repository_url

            urls = [
                representation.external_url
                for representation in manifest.representations
                if representation.asset_id == item.id and representation.external_url
            ]
            urls.extend(
                identifier.value
                for identifier in item.identifiers
                if identifier.scheme in {"github", "url"} and _public_url(identifier.value)
            )
            canonical_url = canonical_repository_url(urls[0]) if urls else None
            parsed = urlsplit(canonical_url) if canonical_url else None
            session.add(
                RepositoryProfile(
                    asset_id=target,
                    canonical_url=canonical_url,
                    host=parsed.hostname if parsed else None,
                    code_status="available",
                )
            )
        for identifier in item.identifiers:
            identity_owner = (
                session.scalar(
                    select(ExternalIdentifierModel).where(
                        ExternalIdentifierModel.scheme == identifier.scheme,
                        ExternalIdentifierModel.scope == identifier.scope,
                        ExternalIdentifierModel.canonical == identifier.canonical,
                        ExternalIdentifierModel.is_identity.is_(True),
                    )
                )
                if identifier.is_identity
                else None
            )
            if identity_owner is not None and identity_owner.asset_id != target:
                raise PackageError("强标识在提交时已被其他资料占用", 409)
            own = session.scalar(
                select(ExternalIdentifierModel).where(
                    ExternalIdentifierModel.asset_id == target,
                    ExternalIdentifierModel.scheme == identifier.scheme,
                    ExternalIdentifierModel.scope == identifier.scope,
                    ExternalIdentifierModel.canonical == identifier.canonical,
                )
            )
            if own is None:
                session.add(ExternalIdentifierModel(asset_id=target, **identifier.model_dump()))
        record = SourceRecord(
            source_id=source.id,
            source_record_id=str(item.id),
            asset_id=target,
            import_run_id=run.id,
            raw_data=raw,
            source_fields=raw["asset"],
            normalized_digest=stable_hash(raw),
        )
        session.add(record)
        session.flush()
        version = SourceRecordVersion(
            source_record_id=record.id,
            import_run_id=run.id,
            adapter_version="1",
            raw_data=raw,
            source_fields=raw["asset"],
            raw_digest=stable_hash(raw),
        )
        session.add(version)
        session.flush()
        session.add(
            SourceRecordAsset(
                source_record_id=record.id,
                asset_id=target,
                source_record_version_id=version.id,
                role="primary",
                evidence={"package_sha256": package.sha256, "archive_asset_id": str(item.id)},
            )
        )
        record_models[item.id] = record
        for field in _METADATA_FIELDS:
            if field in raw["asset"]:
                value = raw["asset"][field]
                session.add(
                    FieldClaim(
                        entity_type="asset",
                        entity_id=target,
                        field_path=f"/{field}",
                        value_json=value,
                        value_digest=stable_hash(value),
                        source_record_version_id=version.id,
                        provider="domain-package",
                        source_key=f"{source_id}:{item.id}",
                        source_pointer=f"/items/{len(record_models) - 1}/asset/{field}",
                        authority=100,
                        status="active",
                    )
                )
        session.add(
            Job(
                job_type="index_asset",
                parameters={"asset_id": str(target)},
                dedupe_key=f"package:{package.id}:index:{target}",
            )
        )
    representation_map: dict[UUID, UUID] = {}
    representation_count = 0
    for archived_representation in manifest.representations:
        target = mappings[archived_representation.asset_id]
        if not archived_representation.files and not archived_representation.external_url:
            continue
        path_ref = None
        if archived_representation.files:
            if archived_representation.materialization_kind in {"directory", "bundle"}:
                path_ref = f"attachments/{archived_representation.id}"
            else:
                if len(archived_representation.files) != 1:
                    raise PackageError("文件表示必须对应一个附件")
                path_ref = archived_representation.files[0].path
        # Same digest/type/role on a reused asset refers to the existing representation.
        duplicate = _existing_representation(session, target, archived_representation)
        if duplicate is not None:
            representation_map[archived_representation.id] = duplicate.id
            continue
        attributes: dict[str, Any] = {
            "package_import_id": str(package.id),
            "archive_representation_id": str(archived_representation.id),
            "sandbox_policy": "static",
        }
        tree_digest = None
        if path_ref is not None:
            materialized_path = snapshot / path_ref
            materialized_stat = materialized_path.stat()
            attributes["_source_size"] = materialized_stat.st_size
            attributes["_source_mtime_ns"] = materialized_stat.st_mtime_ns
            if archived_representation.materialization_kind in {"directory", "bundle"}:
                tree_digest = _directory_tree_digest(materialized_path)
                attributes["package_content_tree_digest"] = archived_representation.tree_digest
        native_representation = Representation(
            asset_id=target,
            source_id=source.id,
            source_record_id=record_models[archived_representation.asset_id].id,
            representation_type=archived_representation.representation_type.value,
            role=archived_representation.role.value,
            materialization_kind=archived_representation.materialization_kind
            if archived_representation.files
            else "url",
            availability="available",
            root_key="attachments",
            path_ref=path_ref,
            external_url=archived_representation.external_url,
            entrypoint=archived_representation.entrypoint,
            revision=archived_representation.revision,
            sha256=archived_representation.files[0].sha256
            if archived_representation.files
            and archived_representation.materialization_kind == "file"
            else archived_representation.sha256,
            tree_digest=tree_digest,
            mime_type=archived_representation.mime_type,
            size_bytes=sum(file.size_bytes for file in archived_representation.files)
            if archived_representation.files
            else None,
            network_required=not bool(archived_representation.files),
            verified_at=now,
            attributes=attributes,
        )
        session.add(native_representation)
        session.flush()
        representation_map[archived_representation.id] = native_representation.id
        representation_count += 1
        audit(
            session,
            native_representation,
            "representation",
            None,
            {
                "asset_id": str(target),
                "sha256": native_representation.sha256,
                "package_id": str(package.id),
            },
            request_id,
        )
        if archived_representation.files:
            session.add(
                Job(
                    job_type="extract_representation",
                    parameters={"representation_id": str(native_representation.id)},
                    dedupe_key=f"package:{package.id}:extract:{native_representation.id}",
                )
            )
    relations_created = 0
    for archived_relation in manifest.relations:
        subject, object_id = (
            mappings[archived_relation.subject_asset_id],
            mappings[archived_relation.object_asset_id],
        )
        existing_relation = session.scalar(
            select(Relation).where(
                Relation.subject_asset_id == subject,
                Relation.object_asset_id == object_id,
                Relation.relation_type == archived_relation.relation_type.value,
                Relation.deleted_at.is_(None),
            )
        )
        if existing_relation:
            continue
        native_relation = Relation(
            subject_asset_id=subject,
            object_asset_id=object_id,
            relation_type=archived_relation.relation_type.value,
            source_key="canonical",
            confirmation_status=archived_relation.confirmation_status,
            confidence=archived_relation.confidence,
            evidence={
                "package_sha256": package.sha256,
                "archive_relation_id": str(archived_relation.id),
            },
        )
        session.add(native_relation)
        session.flush()
        audit(
            session,
            native_relation,
            "relation",
            None,
            {
                "subject_asset_id": str(subject),
                "object_asset_id": str(object_id),
                "relation_type": native_relation.relation_type,
            },
            request_id,
        )
        relations_created += 1
    user_state_created = 0
    if request.include_user_state and manifest.user_state:
        for archived_state in manifest.user_state.reading_states:
            target = mappings[archived_state.asset_id]
            if (
                session.scalar(
                    select(ReadingState).where(
                        ReadingState.asset_id == target, ReadingState.user_id == "local"
                    )
                )
                is None
            ):
                state = ReadingState(asset_id=target, user_id="local", status=archived_state.status)
                session.add(state)
                session.flush()
                audit(
                    session,
                    state,
                    "reading_state",
                    None,
                    {"asset_id": str(target), "status": archived_state.status},
                    request_id,
                )
                user_state_created += 1
        for archived_position in manifest.user_state.reading_positions:
            target = mappings[archived_position.asset_id]
            representation_id = representation_map.get(archived_position.representation_id)
            if (
                representation_id is not None
                and session.scalar(
                    select(ReadingPosition).where(
                        ReadingPosition.asset_id == target, ReadingPosition.user_id == "local"
                    )
                )
                is None
            ):
                position = ReadingPosition(
                    asset_id=target,
                    representation_id=representation_id,
                    page=archived_position.page,
                    source_hash=archived_position.source_hash,
                )
                session.add(position)
                session.flush()
                audit(
                    session,
                    position,
                    "reading_position",
                    None,
                    {
                        "asset_id": str(target),
                        "page": archived_position.page,
                        "source_hash": archived_position.source_hash,
                    },
                    request_id,
                )
                user_state_created += 1
        for archived_annotation in manifest.user_state.annotations:
            target = mappings[archived_annotation.asset_id]
            old = session.scalar(
                select(Annotation).where(
                    Annotation.asset_id == target, Annotation.id == archived_annotation.id
                )
            )
            if old is not None:
                continue
            values = archived_annotation.model_dump(exclude={"id", "asset_id", "representation_id"})
            annotation = Annotation(
                id=archived_annotation.id
                if session.get(Annotation, archived_annotation.id) is None
                else new_uuid7(),
                asset_id=target,
                representation_id=representation_map.get(archived_annotation.representation_id)
                if archived_annotation.representation_id is not None
                else None,
                **values,
            )
            session.add(annotation)
            session.flush()
            audit(
                session,
                annotation,
                "annotation",
                None,
                {"asset_id": str(target), "package_id": str(package.id)},
                request_id,
            )
            user_state_created += 1
    result = {
        "import_id": str(package.id),
        "status": "committed",
        "assets_created": len(created),
        "assets_reused": len(mappings) - len(created),
        "representations_created": representation_count,
        "relations_created": relations_created,
        "user_state_created": user_state_created,
        "identity_mapping": {str(key): str(value) for key, value in mappings.items()},
    }
    before = import_payload(package)
    package.status, package.result, package.preview = "committed", result, fresh_preview
    session.flush()
    audit(session, package, "package_import", before, import_payload(package), request_id)
    return result
