from uuid import UUID

import pytest
from sqlalchemy import event

from packages.domain.models import Asset, ExternalIdentifierModel
from packages.knowledge.service import KnowledgeError
from tests.knowledge.test_service import library as library


@pytest.mark.parametrize("field", ["summary", "content", "tags", "authors", "source_urls"])
def test_non_search_metadata_never_recalls(library, field):
    service, asset, *_ = library
    setattr(
        asset,
        field,
        ["bluewidget"] if field in {"tags", "authors", "source_urls"} else "bluewidget",
    )
    service.session.commit()
    assert service.search_assets("bluewidget")["total"] == 0


@pytest.mark.parametrize("field", ["title", "title_zh", "abstract"])
def test_titles_and_abstract_recall(library, field):
    service, asset, *_ = library
    setattr(asset, field, "bluewidget")
    service.session.commit()
    result = service.search_assets("bluewidget")
    assert result["total"] == 1
    assert result["items"][0]["asset_id"] == str(asset.id)
    assert result["search_fields"] == ["title", "title_zh", "abstract"]
    assert result["semantic_search"] is False
    assert result["query_plan"]["unmapped"] == ("bluewidget",)


@pytest.mark.parametrize(
    "scheme,value,query",
    [
        ("doi", "10.1234/bluewidget", "https://doi.org/10.1234/bluewidget"),
        ("arxiv", "2601.12345", "arXiv: 2601.12345v2"),
    ],
)
def test_identifiers_only_match_whole_normalized_values(library, scheme, value, query):
    service, asset, *_ = library
    service.session.add(
        ExternalIdentifierModel(asset_id=asset.id, scheme=scheme, value=value, canonical=value)
    )
    service.session.commit()
    result = service.search_assets(query)
    assert result["total"] == 1
    assert result["items"][0]["keyword_score"] >= 2000
    assert service.search_assets("bluewidget")["total"] == 0
    assert service.search_assets("2601")["total"] == 0


def test_all_aliases_nfkc_and_optional_conditions_recall(library):
    service, asset, *_ = library
    asset.title = "S2Q: Diffusion Sampling"
    asset.year = 2024
    asset.summary = "requires training"
    service.session.commit()
    result = service.search_assets("S²Q量化，2026年，无需重新训练")
    assert result["total"] == 1
    assert result["items"][0]["constraints_need_evidence_verification"]
    assert service.search_assets("扩散")["total"] == 1
    assert (
        service.search_assets("ＦＵＬＬＷＩＤＴＨ", keywords=["ＤＩＦＦＵＳＩＯＮ"])["total"] == 1
    )


def test_complete_pagination_over_multiple_database_batches(library):
    service, *_ = library
    expected = [str(UUID(int=index)) for index in range(1, 302)]
    service.session.add_all(
        [
            Asset(
                id=UUID(value),
                asset_type="repository" if index % 2 else "paper",
                title="bluewidget",
            )
            for index, value in enumerate(expected)
        ]
    )
    service.session.commit()
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT assets."):
            statements.append((statement, parameters))

    event.listen(service.session.bind, "before_cursor_execute", record)
    seen = []
    offset = 0
    try:
        while True:
            page = service.search_assets("bluewidget", offset=offset)
            assert page["total"] == 301 and page["limit"] == 20
            assert page["offset"] == offset
            assert not page["total_is_lower_bound"] and not page["degraded"]
            assert page["backend"] == "postgresql_keywords"
            assert [item["rank"] for item in page["items"]] == list(
                range(offset + 1, offset + len(page["items"]) + 1)
            )
            seen.extend(item["asset_id"] for item in page["items"])
            if not page["has_more"]:
                assert page["next_offset"] is None
                break
            offset = page["next_offset"]
    finally:
        event.remove(service.session.bind, "before_cursor_execute", record)
    assert seen == expected
    assert all("assets.summary" not in sql and "assets.content" not in sql for sql, _ in statements)
    assert len(statements) == 32  # two UUID database pages per response
    assert all(parameters[-2] == 256 for _, parameters in statements)
    assert service.search_assets("bluewidget", offset=999)["items"] == []
    with pytest.raises(KnowledgeError, match="nonnegative"):
        service.search_assets("bluewidget", offset=-1)
    assert service.search_assets("bluewidget", asset_type="paper")["total"] == 151


def test_source_type_filter_preserves_available_metadata(library):
    service, asset, rep, source, *_ = library
    assert service.search_assets("Evidence", source_type="original")["total"] == 1
    rep.availability = "missing"
    service.session.commit()
    # Metadata remains findable, even when its file is unavailable.
    assert service.search_assets("Evidence")["total"] == 1
    assert service.get_paper(asset.id)["representations"][0]["readable"] is False
    source.enabled = False
    service.session.commit()
    assert service.search_assets("Evidence", source_type="original")["total"] == 0
    assert service.search_assets("Evidence")["total"] == 1


@pytest.mark.parametrize("query", ["", "  "])
def test_domain_catalog_lists_all_27_papers_without_title_keyword(library, query):
    service, *_ = library
    expected = [str(UUID(int=index)) for index in range(1, 28)]
    service.session.add_all(
        [
            Asset(
                id=UUID(value),
                asset_type="paper",
                title=f"Unrelated title {index}",
                domain_slugs=["ai-for-math"],
            )
            for index, value in enumerate(expected)
        ]
    )
    service.session.add_all(
        [
            Asset(asset_type="repository", title="Code", domain_slugs=["ai-for-math"]),
            Asset(asset_type="paper", title="Other domain", domain_slugs=["agent"]),
            Asset(
                asset_type="paper",
                title="Hidden",
                domain_slugs=["ai-for-math"],
                attributes={"system_hidden": True},
            ),
        ]
    )
    service.session.commit()
    seen = []
    offset = 0
    while True:
        page = service.search_assets(
            query, domain="ai-for-math", asset_type="paper", limit=20, offset=offset
        )
        assert page["mode"] == "metadata" and page["total"] == 27
        assert page["total_is_lower_bound"] is False
        seen.extend(item["asset_id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_offset"] is None
            break
        offset = page["next_offset"]
    assert seen == expected
    assert len(set(seen)) == 27
    assert not service.session.dirty and not service.session.new
