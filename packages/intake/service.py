from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from pypdf import PdfReader
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.events import record_mutation, stable_hash
from packages.domain.enums import AuditAction
from packages.domain.identifiers import new_uuid7
from packages.domain.models import (
    Asset,
    ExternalIdentifierModel,
    FieldResolution,
    ImportRun,
    Job,
    PaperProfile,
    Representation,
    Source,
    SourceRecord,
    SourceRecordAsset,
    SourceRecordVersion,
    utc_now,
)
from packages.domain.product_models import IntakeDraft
from packages.domain.schemas import ExternalIdentifier
from packages.domain.serving_scope import active_representation_clause
from packages.domain_catalog.service import domain_slugs
from packages.managed_sources.service import (
    canonical_bytes,
    data_directory,
    frozen_source_config,
    managed_path,
    register_managed_source,
)


class IntakeError(ValueError):
    def __init__(self, detail: str, status: int = 422) -> None:
        super().__init__(detail)
        self.status = status


def draft_directory(settings: Any, draft_id: UUID) -> Path:
    return managed_path(data_directory(settings), "library", "staging", str(draft_id))


def draft_pdf(settings: Any, draft: IntakeDraft) -> Path:
    if draft.storage_mode == "reference":
        return checked_reference(settings, draft.reference_path or "")
    return managed_path(
        data_directory(settings), "library", "staging", str(draft.id), f"{draft.pdf_sha256}.pdf"
    )


def checked_reference(settings: Any, value: str) -> Path:
    raw = Path(value).expanduser()
    if not raw.is_absolute():
        raise IntakeError("原位引用必须选择已批准永久目录中的绝对 PDF 路径")
    path = raw.resolve()
    roots = [
        Path(item).expanduser().resolve()
        for item in getattr(settings, "intake_reference_roots", [])
    ]
    if not any(path.is_relative_to(root) for root in roots):
        raise IntakeError("该路径不在已批准的永久资料目录内；请使用托管上传或配置引用目录")
    if not path.is_file() or path.suffix.casefold() != ".pdf":
        raise IntakeError("原位引用必须是存在的 PDF 文件")
    return path


def validate_pdf(path: Path, max_bytes: int) -> tuple[str, int]:
    size = path.stat().st_size
    if size > max_bytes:
        raise IntakeError("PDF 超过上传大小限制", 413)
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise IntakeError("文件内容不是 PDF")
    try:
        with path.open("rb") as handle:
            reader = PdfReader(handle, strict=True)
            if reader.is_encrypted or not len(reader.pages):
                raise IntakeError("PDF 已加密或没有可读取页面")
    except IntakeError:
        raise
    except Exception as error:
        raise IntakeError("PDF 结构损坏，无法读取") from error
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), size


def serialize_draft(draft: IntakeDraft) -> dict[str, Any]:
    return {
        "id": str(draft.id),
        "domain": draft.domain,
        "status": draft.status,
        "storage_mode": draft.storage_mode,
        **{key: value for key, value in draft.metadata_json.items() if not key.startswith("_")},
        "filename": draft.filename,
        "pdf_sha256": draft.pdf_sha256,
        "pdf_size_bytes": draft.pdf_size_bytes,
        "pdf_uploaded": bool(draft.pdf_sha256),
        "reference_path": draft.reference_path,
        "commit_plan": draft.commit_plan,
        "asset_id": str(draft.asset_id) if draft.asset_id else None,
        "error": draft.error,
        "created_at": draft.created_at.isoformat(),
        "updated_at": draft.updated_at.isoformat(),
    }


def editable(draft: IntakeDraft) -> None:
    if draft.status not in {"draft", "ready"}:
        raise IntakeError("该草稿已取消或已进入正式提交，不能再修改", 409)


def require_domain(session: Session, settings: Any, slug: str) -> None:
    if slug not in domain_slugs(session, settings.taxonomy_config_path):
        raise IntakeError("请选择已注册的领域")


def identity_values(metadata: dict[str, Any]) -> list[tuple[str, str, str]]:
    return [
        (scheme, value, str(ExternalIdentifier(scheme=scheme, value=value).canonical))
        for scheme, value in (("doi", metadata.get("doi")), ("arxiv", metadata.get("arxiv_id")))
        if value
    ]


def duplicate_candidates(session: Session, draft: IntakeDraft) -> list[dict[str, Any]]:
    matches: dict[UUID, set[str]] = {}
    for scheme, _, canonical in identity_values(draft.metadata_json):
        for identifier in session.scalars(
            select(ExternalIdentifierModel).where(
                ExternalIdentifierModel.scheme == scheme,
                ExternalIdentifierModel.canonical == canonical,
                ExternalIdentifierModel.is_identity.is_(True),
            )
        ):
            matches.setdefault(identifier.asset_id, set()).add(scheme)
    if draft.pdf_sha256:
        for representation in session.scalars(
            select(Representation).where(
                Representation.sha256 == draft.pdf_sha256,
                active_representation_clause(),
            )
        ):
            matches.setdefault(representation.asset_id, set()).add("sha256")
    result = []
    for asset_id, reasons in matches.items():
        asset = session.get(Asset, asset_id)
        if asset is not None and asset.deleted_at is None:
            result.append(
                {"asset_id": str(asset.id), "title": asset.title, "reasons": sorted(reasons)}
            )
    return result


def preview(session: Session, draft: IntakeDraft) -> dict[str, Any]:
    warnings = []
    if not draft.metadata_json.get("title"):
        warnings.append({"code": "title_required", "message": "正式提交前需要填写标题"})
    if not draft.pdf_sha256:
        warnings.append({"code": "pdf_required", "message": "正式提交前需要上传或选择 PDF"})
    duplicates = duplicate_candidates(session, draft)
    if duplicates:
        warnings.append(
            {
                "code": "duplicate_candidates",
                "message": "发现标识或内容相同的资料，请明确选择创建或附加",
            }
        )
    return {"draft": serialize_draft(draft), "duplicates": duplicates, "warnings": warnings}


def prepare_commit(
    session: Session, settings: Any, draft: IntakeDraft, plan: dict[str, Any]
) -> None:
    if draft.status in {"committed", "committing"}:
        if draft.commit_plan != plan:
            raise IntakeError("该草稿已使用其他提交决定，不能更换资产或操作", 409)
        return
    editable(draft)
    require_domain(session, settings, draft.domain)
    if not draft.metadata_json.get("title") or not draft.pdf_sha256:
        raise IntakeError("正式入库需要标题、领域和 PDF")
    digest, size = validate_pdf(draft_pdf(settings, draft), settings.intake_max_pdf_bytes)
    if digest != draft.pdf_sha256 or size != draft.pdf_size_bytes:
        raise IntakeError("草稿 PDF 内容已发生变化，请重新上传或选择", 409)
    identifiers = [
        item for item in duplicate_candidates(session, draft) if set(item["reasons"]) - {"sha256"}
    ]
    if plan["action"] == "create" and identifiers:
        raise IntakeError("强标识已属于现有资料，请选择附加或更正标识", 409)
    if plan["action"] == "attach":
        target = session.get(Asset, UUID(plan["asset_id"])) if plan.get("asset_id") else None
        if target is None or target.deleted_at is not None or target.asset_type != "paper":
            raise IntakeError("附加需要选择存在的论文资产", 409)
        if any(item["asset_id"] != str(target.id) for item in identifiers):
            raise IntakeError("强标识指向其他资产，请核对附加对象", 409)
        session.refresh(target, with_for_update=True)
        draft.metadata_json = {
            **draft.metadata_json,
            "_attach_metadata": {
                field: getattr(target, field)
                for field in (
                    "title",
                    "title_zh",
                    "abstract",
                    "summary",
                    "authors",
                    "year",
                    "venue",
                    "tags",
                )
            },
        }
    draft.status = "committing"
    draft.commit_plan = plan
    draft.error = None
    session.flush()


def _permanent_pdf(settings: Any, draft: IntakeDraft) -> Path:
    source_path = draft_pdf(settings, draft)
    digest, _ = validate_pdf(source_path, settings.intake_max_pdf_bytes)
    if digest != draft.pdf_sha256:
        raise IntakeError("提交中的 PDF 指纹已变化", 409)
    if draft.storage_mode == "reference":
        return source_path
    destination = managed_path(
        data_directory(settings), "library", "documents", str(draft.id), "paper.pdf"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if validate_pdf(destination, settings.intake_max_pdf_bytes)[0] != digest:
            raise IntakeError("正式资料路径已存在其他内容，拒绝覆盖", 409)
        return destination
    temporary = destination.with_name("paper-" + uuid4().hex + ".tmp")
    try:
        shutil.copyfile(source_path, temporary)
        if validate_pdf(temporary, settings.intake_max_pdf_bytes)[0] != draft.pdf_sha256:
            raise IntakeError("复制期间 PDF 内容发生变化，输入已保留，请重试", 409)
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        # Hard linking publishes a complete file without replacing any existing original.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if validate_pdf(destination, settings.intake_max_pdf_bytes)[0] != digest:
                raise IntakeError("正式资料路径不可覆盖", 409) from None
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def complete_commit(
    session: Session, settings: Any, draft: IntakeDraft, request_id: str | None
) -> dict[str, Any]:
    if draft.status == "committed":
        return {"asset_id": str(draft.asset_id), "draft_id": str(draft.id), "status": "committed"}
    plan = draft.commit_plan
    if draft.status != "committing" or plan is None:
        raise IntakeError("草稿尚未准备提交", 409)
    pdf = _permanent_pdf(settings, draft)
    metadata = {key: value for key, value in draft.metadata_json.items() if not key.startswith("_")}
    manifest_metadata = metadata
    if plan["action"] == "attach":
        target = session.get(Asset, UUID(plan["asset_id"]))
        if target is None or target.deleted_at is not None:
            raise IntakeError("附加对象已不可用", 409)
        session.refresh(target, with_for_update=True)
        manifest_metadata = {**metadata, **draft.metadata_json["_attach_metadata"]}
    item = {
        **manifest_metadata,
        "id": str(draft.id),
        "representations": {"pdf": {"path": pdf.name, "sha256": draft.pdf_sha256}},
    }
    if plan["action"] == "attach":
        item["intake_metadata"] = metadata
    if metadata.get("html_url"):
        item["representations"]["html"] = {"url": metadata["html_url"]}
    declaration = register_managed_source(
        data_directory(settings),
        {
            "id": "intake-" + draft.id.hex,
            "label": "人工入库 · " + metadata["title"][:200],
            "domain": draft.domain,
            "adapter": "generic_manifest",
            "asset_roots": {"pdf": str(pdf.parent)},
        },
        {"schema_version": 1, "items": [item]},
    )
    source = Source(
        id=new_uuid7(),
        alias=declaration["id"],
        label=declaration["label"],
        adapter="generic_manifest",
        root_path=declaration["root"],
        manifest_path="manifest.json",
        read_only=True,
        config=frozen_source_config(declaration),
    )
    session.add(source)
    if plan["action"] == "attach":
        asset = session.get(Asset, UUID(plan["asset_id"]))
        if asset is None or asset.deleted_at is not None:
            raise IntakeError("附加对象已不可用", 409)
        before = {"domain_slugs": list(asset.domain_slugs)}
        updated_domains = list(dict.fromkeys([*asset.domain_slugs, draft.domain]))
        if updated_domains != asset.domain_slugs:
            _domain_override(session, asset, updated_domains, request_id)
    else:
        asset = Asset(
            id=new_uuid7(),
            asset_type="paper",
            title=metadata["title"],
            domain_slugs=[draft.domain],
            **{
                key: metadata[key]
                for key in ("title_zh", "abstract", "summary", "authors", "year", "venue", "tags")
                if key in metadata
            },
        )
        session.add(asset)
        session.add(PaperProfile(asset_id=asset.id))
        before = None
    session.flush()
    now = utc_now()
    run = ImportRun(
        id=new_uuid7(),
        source_id=source.id,
        status="succeeded",
        mode="manual_intake",
        dry_run=False,
        adapter_name="generic_manifest",
        adapter_version="manual-intake-v1",
        config_digest=source.config["_frozen_source_config_sha256"],
        started_at=now,
        finished_at=now,
        counters={
            "source_records_created": 1,
            "representations_created": 1,
            "paper_assets_created": int(plan["action"] == "create"),
        },
    )
    session.add(run)
    session.flush()
    record = SourceRecord(
        id=new_uuid7(),
        source_id=source.id,
        source_record_id=str(draft.id),
        asset_id=asset.id,
        import_run_id=run.id,
        outcome="ready",
        raw_data=item,
        source_fields=metadata,
        normalized_digest=stable_hash(item),
    )
    session.add(record)
    session.flush()
    version = SourceRecordVersion(
        id=new_uuid7(),
        source_record_id=record.id,
        import_run_id=run.id,
        adapter_version="manual-intake-v1",
        raw_data=item,
        source_fields=metadata,
        raw_digest=hashlib.sha256(canonical_bytes(item)).hexdigest(),
    )
    session.add(version)
    session.flush()
    session.add(
        SourceRecordAsset(
            source_record_id=record.id,
            asset_id=asset.id,
            source_record_version_id=version.id,
            role="primary",
            evidence={"action": plan["action"]},
        )
    )
    for scheme, value, canonical in identity_values(metadata):
        existing = session.scalar(
            select(ExternalIdentifierModel).where(
                ExternalIdentifierModel.scheme == scheme,
                ExternalIdentifierModel.canonical == canonical,
                ExternalIdentifierModel.is_identity.is_(True),
            )
        )
        if existing is not None and existing.asset_id != asset.id:
            raise IntakeError("强标识已被其他资料占用，请核对后重试", 409)
        if existing is None:
            session.add(
                ExternalIdentifierModel(
                    asset_id=asset.id,
                    scheme=scheme,
                    value=value,
                    canonical=canonical,
                    scope="",
                    is_identity=True,
                )
            )
    published_stat = pdf.stat()
    representation = Representation(
        id=new_uuid7(),
        asset_id=asset.id,
        source_id=source.id,
        source_record_id=record.id,
        representation_type="pdf",
        role="original",
        materialization_kind="file",
        availability="available",
        root_key="pdf",
        path_ref=pdf.name,
        sha256=draft.pdf_sha256,
        size_bytes=draft.pdf_size_bytes,
        mime_type="application/pdf",
        verified_at=now,
        attributes={
            "intake_draft_id": str(draft.id),
            "storage_mode": draft.storage_mode,
            "_source_size": published_stat.st_size,
            "_source_mtime_ns": published_stat.st_mtime_ns,
        },
    )
    session.add(representation)
    if metadata.get("html_url"):
        session.add(
            Representation(
                asset_id=asset.id,
                source_id=source.id,
                source_record_id=record.id,
                representation_type="html",
                role="original",
                materialization_kind="url",
                availability="available",
                external_url=metadata["html_url"],
                network_required=True,
            )
        )
    session.add(
        Job(
            job_type="extract_representation",
            parameters={"representation_id": str(representation.id)},
            dedupe_key=f"intake:{draft.id}:extract",
            status="pending",
        )
    )
    response = {"asset_id": str(asset.id), "draft_id": str(draft.id), "status": "committed"}
    record_mutation(
        session,
        entity_type="asset",
        entity_id=asset.id,
        action=AuditAction.IMPORT,
        before=before,
        after={
            "asset_id": str(asset.id),
            "draft_id": str(draft.id),
            "source_id": str(source.id),
            "domain_slugs": asset.domain_slugs,
        },
        request_id=request_id,
        event_type="asset.imported",
    )
    draft.asset_id, draft.source_id, draft.status, draft.error = (
        asset.id,
        source.id,
        "committed",
        None,
    )
    session.flush()
    return response


def _domain_override(
    session: Session, asset: Asset, domains: list[str], request_id: str | None
) -> None:
    from apps.api.routes_v1 import _ensure_override_base_claim, _serialize_override

    resolution = session.scalar(
        select(FieldResolution)
        .where(
            FieldResolution.entity_type == "asset",
            FieldResolution.entity_id == asset.id,
            FieldResolution.field_path == "/domain_slugs",
        )
        .with_for_update()
    )
    before = _serialize_override(resolution) if resolution else None
    if resolution is None:
        resolution = FieldResolution(
            entity_type="asset",
            entity_id=asset.id,
            field_path="/domain_slugs",
            resolution_kind="override",
            override_value=domains,
            resolved_by="local",
        )
        session.add(resolution)
        session.flush()
    _ensure_override_base_claim(session, resolution, asset)
    resolution.resolution_kind, resolution.selected_claim_id = "override", None
    resolution.override_value, resolution.reason = domains, "人工附加 PDF 时选择领域"
    asset.domain_slugs = domains
    session.flush()
    record_mutation(
        session,
        entity_type="field_override",
        entity_id=resolution.id,
        action=AuditAction.CREATE if before is None else AuditAction.UPDATE,
        before=before,
        after=_serialize_override(resolution),
        request_id=request_id,
        event_type="field_override.updated",
    )
