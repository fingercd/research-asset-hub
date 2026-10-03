import httpx
import pytest

from packages.knowledge.abstracts import (
    extract_abstract,
    parse_arxiv_abstract,
    parse_proceedings_abstract,
    title_identity,
)
from scripts.enrich_paper_abstracts import apply_candidates, digest, explicit_abstracts

ABSTRACT = (
    "We present a method for efficient model training with carefully controlled "
    "experiments on several benchmark datasets. Our approach reduces memory "
    "requirements while preserving predictive accuracy across all evaluated tasks. "
    "The results show consistent improvements over previous methods and provide "
    "evidence that the proposed optimization scales to large models."
)


def test_labelled_abstract_preserves_author_text():
    value = f"A Study\nAuthor\nAbstract\n{ABSTRACT}\n1. Introduction\nBody."
    excerpt, reason = extract_abstract(value)
    assert reason == "labelled_author_abstract"
    assert excerpt.text == ABSTRACT
    assert excerpt.end_marker == "1. Introduction"


def test_inline_abstract_and_split_section_number():
    excerpt, _ = extract_abstract(f"Abstract—{ABSTRACT}\n1\nINTRODUCTION\nBody")
    assert excerpt is not None
    assert excerpt.text == ABSTRACT


def test_spaced_abstract_heading_and_unicode_keyword_separator():
    excerpt, reason = extract_abstract(
        f"A B S T R A C T\n{ABSTRACT}\nKeywords\u2002 Machine learning\n1 Introduction"
    )
    assert reason == "labelled_author_abstract"
    assert excerpt.text == ABSTRACT


def test_body_and_generated_summary_are_not_an_abstract():
    assert extract_abstract(f"Summary\n{ABSTRACT}\nIntroduction")[0] is None
    assert extract_abstract(f"Abstract\n{ABSTRACT}")[0] is None


def test_footer_contamination_is_rejected():
    excerpt, reason = extract_abstract(f"Abstract\n{ABSTRACT}\nCopyright 2025.\nIntroduction")
    assert excerpt is None
    assert reason == "abstract_layout_contamination"


def test_title_identity_accepts_typography_and_catalog_nickname():
    result = title_identity(
        "Elucidating the Design Space of Diffusion-Based Generative Models（EDM）",
        "Elucidating the Design Space of Diffusion–Based\n"
        "Generative Models\nAuthor\nAbstract\nBody",
    )
    assert result["status"] == "verified"


def test_title_identity_rejects_known_mismatches_and_body_mentions():
    for title, wrong in [
        (
            "TASO: Optimizing Deep Learning Computation with "
            "Automatic Generation of Graph Substitutions",
            "SEXTANT: Tracking Mobile Devices",
        ),
        (
            "Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations",
            "Bessel–like birth–death process",
        ),
    ]:
        assert (
            title_identity(title, f"{wrong}\nAuthor\nAbstract\n{title}")["status"] == "unverified"
        )


def test_arxiv_parser_only_reads_labelled_author_fields():
    page = (
        '<h1>Other heading</h1><h1 class="title mathjax"><span>Title:</span>A Study</h1>'
        '<blockquote class="abstract mathjax"><span>Abstract:</span>'
        + ABSTRACT
        + "</blockquote><div>Additional website prose</div>"
    )
    assert parse_arxiv_abstract(page) == ("A Study", ABSTRACT)
    assert parse_arxiv_abstract("<h1>Title: A Study</h1><p>Abstract: Other prose</p>") is None


def test_arxiv_link_placeholder_preserves_author_url():
    page = (
        '<h1 class="title">Title: A Study</h1>'
        '<blockquote class="abstract">Abstract: Code is at '
        '<a href="https://github.com/example/project">this https URL</a>.</blockquote>'
    )
    assert parse_arxiv_abstract(page) == ("A Study", "Code is at https://github.com/example/project.")


def test_source_translated_guide_is_not_original_abstract():
    assert explicit_abstracts({"abstract_zh": "中文方法概括。" * 50, "summary": ABSTRACT}) == []


def test_proceedings_abstract_uses_explicit_region_and_citation_title():
    page = (
        '<meta name="citation_title" content="A Study">'
        '<div id="abstract"><p>' + ABSTRACT + "</p></div><div>Body text.</div>"
    )
    assert parse_proceedings_abstract(page) == ("A Study", ABSTRACT)
    page = (
        '<meta name="citation_title" content="A Study">'
        "<h4>Abstract</h4><p>" + ABSTRACT + "</p><h4>Authors</h4><p>Other text.</p>"
    )
    assert parse_proceedings_abstract(page) == ("A Study", ABSTRACT)


def test_acm_abstract_ends_at_ccs_and_title_prefix_is_allowed():
    excerpt, _ = extract_abstract(f"Abstract\n{ABSTRACT}\nCCS CONCEPTS\nOther metadata")
    assert excerpt.text == ABSTRACT
    assert (
        title_identity(
            "LaGoVAD: Language-Guided Open-World Video Anomaly Detection under Weak Supervision",
            "Language-Guided Open-World Video Anomaly Detection under Weak Supervision\nAbstract",
        )["status"]
        == "verified"
    )


def test_jair_article_abstract_region_excludes_article_details():
    page = (
        '<meta name="citation_title" content="A Study">'
        '<h2>Abstract</h2><div class="article-abstract">'
        + ABSTRACT
        + '</div><h2>Article Details</h2><p>Other metadata.</p>'
    )
    assert parse_proceedings_abstract(page) == ("A Study", ABSTRACT)


def _candidate():
    return {
        "asset_id": "asset-1",
        "title": "A Study",
        "row_version": 1,
        "abstract": ABSTRACT,
        "abstract_sha256": digest(ABSTRACT),
        "language": "en",
        "provenance": {
            "kind": "official_arxiv_author_abstract",
            "identity": {"status": "verified"},
        },
    }


def _mock_client(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(handler))
    )


def test_apply_refuses_api_without_atomic_etag_support(monkeypatch):
    def handler(request):
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json={"paths": {"/api/v1/overrides": {"post": {}}}})

    _mock_client(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="If-Match"):
        apply_candidates({"candidates": [_candidate()]}, api="http://test")


def test_apply_skips_nonempty_and_concurrent_conflict(monkeypatch):
    posts = []
    current_abstract = ["", ABSTRACT]

    def handler(request):
        if request.url.path == "/openapi.json":
            return httpx.Response(
                200,
                json={
                    "paths": {"/api/v1/overrides": {"post": {"parameters": [{"name": "If-Match"}]}}}
                },
            )
        if request.method == "GET":
            return httpx.Response(
                200,
                headers={"ETag": '"asset-1:1"'},
                json={
                    "id": "asset-1",
                    "title": "A Study",
                    "row_version": 1,
                    "abstract": current_abstract.pop(0),
                },
            )
        posts.append(request)
        assert request.headers["If-Match"] == '"asset-1:1"'
        assert (
            request.headers["Idempotency-Key"] == f"abstract-enrichment:asset-1:{digest(ABSTRACT)}"
        )
        return httpx.Response(412)

    _mock_client(monkeypatch, handler)
    outcomes = apply_candidates({"candidates": [_candidate(), _candidate()]}, api="http://test")
    assert [item["status"] for item in outcomes] == ["concurrent_conflict_skip", "already_present"]
    assert len(posts) == 1
