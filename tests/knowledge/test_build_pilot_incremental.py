import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.asset_gateway.registry import SourceRegistry
from packages.domain.models import Asset, Base, Representation, Source, SourceRecord
from packages.domain.platform_models import Activity, DocumentChunk
from packages.knowledge.documents import text_hash
from scripts import build_knowledge_pilot as builder


@pytest.fixture
def library(tmp_path):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        source = Source(
            alias="math-test", label="Math", adapter="generic_manifest", root_path=str(tmp_path)
        )
        asset = Asset(asset_type="paper", title="Formal Math", analysis={"summary": "已有中文分析"})
        db.add_all([source, asset])
        db.flush()
        (tmp_path / "paper.pdf").write_bytes(b"PDF fixture; extraction must be reused")
        rep = Representation(
            asset_id=asset.id,
            source_id=source.id,
            representation_type="pdf",
            role="original",
            path_ref="paper.pdf",
            availability="available",
            sha256=builder.file_hash(tmp_path / "paper.pdf"),
        )
        db.add_all(
            [rep, SourceRecord(source_id=source.id, source_record_id="math-1", asset_id=asset.id)]
        )
        db.flush()
        db.add(
            DocumentChunk(
                asset_id=asset.id,
                representation_id=rep.id,
                chunk_index=0,
                chunk_kind="pdf",
                text_content="Exact original page.",
                content_hash=text_hash("Exact original page."),
                parser_name="pdf",
                parser_version="1",
                anchor={"kind": "pdf", "page": 1},
            )
        )
        db.commit()
        yield db, asset, rep, SourceRegistry({("math-test", "source"): tmp_path}), tmp_path
    engine.dispose()


def test_incremental_build_reuses_chunks_and_preserves_existing_files(library, monkeypatch):
    db, asset, rep, registry, root = library
    monkeypatch.setattr(builder.pdfplumber, "open", lambda *_: pytest.fail("must reuse extraction"))
    item = {"asset_id": str(asset.id), "group": "math-test"}
    first = builder.build_paper(db, registry, item, root / "derivatives")
    directory = root / "derivatives/v1" / str(asset.id)
    document = json.loads((directory / "document.json").read_text(encoding="utf-8"))
    assert first["build_action"] == "built"
    assert document["extraction_reused"] is True
    assert document["pages"][0]["chunk_ids"]
    assert "<!-- page: 1 -->" in (directory / "source.md").read_text(encoding="utf-8")
    assert "不是论文原文" in (directory / "analysis.zh.md").read_text(encoding="utf-8")
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    asset.analysis = {"summary": "should not replace inherited derivative"}
    second = builder.build_paper(db, registry, item, root / "derivatives")
    assert second["build_action"] == "reused"
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    (root / "paper.pdf").write_bytes(b"changed")
    with pytest.raises(ValueError, match="fingerprint"):
        builder.build_paper(db, registry, item, root / "derivatives")


def test_repeated_source_and_asset_scopes_deduplicate(library):
    db, asset, *_ = library
    items = builder.scoped_selection(db, ["math-test", "math-test"], [str(asset.id)] * 2)
    assert len(items) == 1
    assert items[0]["source_record_id"] == "math-1"
    with pytest.raises(ValueError, match="Unknown or disabled"):
        builder.scoped_selection(db, ["unknown-source"], [])


def test_native_pdf_fallback_uses_pdfplumber_and_preserves_original(library):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    db, asset, rep, registry, root = library
    path = root / "paper.pdf"
    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    font_reference = writer._add_object(font)
    texts = ["First extraction page.", "Second extraction page."]
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_reference})}
        )
        content = DecodedStreamObject()
        content.set_data(f"BT /F1 12 Tf 40 750 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(content)
    writer.write(path)
    db.query(DocumentChunk).delete()
    rep.sha256 = builder.file_hash(path)
    db.commit()
    before = path.read_bytes(), path.stat().st_mtime_ns

    result = builder.build_paper(
        db, registry, {"asset_id": str(asset.id), "group": "math-test"}, root / "native-derivatives"
    )
    document_path = root / "native-derivatives/v1" / str(asset.id) / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    assert result["page_count"] == 2
    assert document["parser"].startswith("pdfplumber ")
    assert document["extraction_reused"] is False
    assert [page["text"] for page in document["pages"]] == texts
    assert [page["anchor"]["page"] for page in document["pages"]] == [1, 2]
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_invalid_chunk_hash_cannot_be_rebound_to_live_pdf(library):
    db, asset, rep, _, _ = library
    db.query(DocumentChunk).first().content_hash = "0" * 64
    with pytest.raises(ValueError, match="chunk content fingerprint"):
        builder.extracted_document(db, asset, rep, rep.sha256)


def test_stale_activity_cannot_be_reused(library):
    db, asset, rep, _, _ = library
    activity = Activity(
        asset_id=asset.id,
        representation_id=rep.id,
        activity_type="extract_pdf",
        tool_name="fixture-pdf-parser",
        tool_version="1",
        status="succeeded",
        output_digest="0" * 64,
    )
    db.add(activity)
    db.flush()
    chunk = db.query(DocumentChunk).first()
    chunk.activity_id = activity.id
    chunk.anchor["source_path"] = "paper.pdf"
    with pytest.raises(ValueError, match="live PDF fingerprint"):
        builder.extracted_document(db, asset, rep, rep.sha256)
