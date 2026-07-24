from __future__ import annotations

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from apps.api.rag_runtime import _hydrate_remote_hits
from packages.domain.models import Asset, Base, DocumentChunk, Representation
from packages.indexing import SearchDocument, SearchHit


def test_remote_vector_hit_is_hydrated_from_postgres() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    full_text = "完整证据文本" * 20_000
    digest = "b" * 64
    with Session(engine) as session:
        asset = Asset(asset_type="paper", title="Hydrated Evidence")
        session.add(asset)
        session.flush()
        representation = Representation(
            asset_id=asset.id,
            representation_type="pdf",
            role="original",
            availability="available",
            root_key="source",
            path_ref="paper.pdf",
            sha256=digest,
        )
        session.add(representation)
        session.flush()
        chunk = DocumentChunk(
            asset_id=asset.id,
            representation_id=representation.id,
            chunk_index=0,
            chunk_kind="text",
            text_content=full_text,
            content_hash=hashlib.sha256(full_text.encode()).hexdigest(),
            parser_name="pypdf",
            parser_version="1",
            anchor_kind="pdf_page",
            anchor={"page": 1},
        )
        session.add(chunk)
        session.flush()
        remote = SearchHit(
            document=SearchDocument(
                document_id=f"chunk:{chunk.id}",
                asset_id=str(asset.id),
                kind="text",
                title=asset.title,
                text=full_text[:32_768],
                metadata={
                    "chunk_id": str(chunk.id),
                    "representation_id": str(representation.id),
                    "representation_digest": digest,
                    "chunk_hash": chunk.content_hash,
                    "anchor": {"page": 1},
                },
            ),
            score=0.8,
            rank=1,
            source="qdrant",
        )
        digest_map: dict[str, str] = {}

        hydrated = _hydrate_remote_hits(session, [remote], digest_map)

    engine.dispose()
    assert hydrated[0].document.text == full_text
    assert hydrated[0].document.metadata["chunk_hash"] == hashlib.sha256(
        full_text.encode()
    ).hexdigest()
    assert digest_map[str(representation.id)] == digest
