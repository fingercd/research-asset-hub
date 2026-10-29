"""Publication boundary and buildless catalog regression checks (no live services)."""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "public_catalog_builder", ROOT / "scripts/build_public_catalog.py"
)
assert SPEC and SPEC.loader
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://arxiv.org/pdf/2501.12345v2.pdf", "https://arxiv.org/abs/2501.12345"),
        ("http://export.arxiv.org/abs/1706.03762", "https://arxiv.org/abs/1706.03762"),
        ("https://doi.org/10.1000/example", "https://doi.org/10.1000/example"),
        ("https://openreview.net/pdf?id=Paper_123", "https://openreview.net/forum?id=Paper_123"),
        (
            "https://proceedings.mlr.press/v202/example23.html",
            "https://proceedings.mlr.press/v202/example23.html",
        ),
        (
            "https://aclanthology.org/2025.acl-long.123/",
            "https://aclanthology.org/2025.acl-long.123/",
        ),
        (
            "https://raw.githubusercontent.com/mlresearch/v202/main/assets/x/x.pdf",
            "https://raw.githubusercontent.com/mlresearch/v202/main/assets/x/x.pdf",
        ),
    ],
)
def test_public_paper_urls(value: str, expected: str) -> None:
    assert builder.public_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "data:text/html,test",
        "file:///C:/private.pdf",
        "https://localhost/paper.pdf",
        "https://127.0.0.1/paper.pdf",
        "http://[::1]/paper.pdf",
        "https://10.0.0.1/paper.pdf",
        "https://arxiv.org.evil.example/abs/2501.12345",
        "https://arxiv.org@evil.example/abs/2501.12345",
        "https://person:password@arxiv.org/abs/2501.12345",
        "https://arxiv.org:8766/abs/2501.12345",
        "https://arxiv.org/abs/2501.12345?token=private",
        "https://openreview.net/forum?id=Paper_123&access_token=private",
        "https://openreview.net/forum?id=Paper_123&id=Other",
        "https://openreview.net/forum?id=",
        "https://arxiv.org/abs/2501.12345#secret",
        "https://arxiv.org/abs/%2e%2e/2501.12345",
        "https://arxiv.org\\@localhost/abs/2501.12345",
        "https://doi.org/10.1000/%252e%252e/secret",
        "https://doi.org/10.1000/%5cprivate",
        "https://doi.org/10.1000/%0asecret",
        "https://doi.org/10.1000/%2fprivate",
        "https://raw.githubusercontent.com/private-user/private-repo/main/notes.pdf",
        "https://proceedings.mlr.press/v202/",
        "https://openreview.net/group?id=ICLR.cc",
        "https://example.com/paper.pdf",
        "https://arxiv.org/abs/2501.12345\n",
    ],
)
def test_reject_private_unknown_and_ambiguous_urls(value: str) -> None:
    assert builder.public_url(value) is None


def test_repository_identity_paths() -> None:
    assert (
        builder.public_url("https://github.com/org/repo.git", code=True)
        == "https://github.com/org/repo"
    )
    assert builder.public_url("https://huggingface.co/spaces/org/project", code=True)
    for value in [
        "https://github.com/org/repo/blob/main/private.txt",
        "https://github.com/login/foo",
        "https://github.com/org/repo?token=secret",
        "https://github.example.com/org/repo",
        "https://github.com/org/../settings",
        "https://github.com/org/repo#private",
    ]:
        assert builder.public_url(value, code=True) is None
    assert builder.public_url("https://github.com/org/repo") is None


def record(kind: str, ident: str, **fields: Any) -> dict[str, Any]:
    return {
        "asset": {
            "asset_id": ident,
            "asset_type": kind,
            "title": "A research paper",
            "title_zh": "研究论文",
            "year": 2026,
            "venue": "ICML 2026",
            "domains": ["agent"],
            "source_urls": [],
            "visibility": "private",
            "summary": "PRIVATE_SUMMARY",
            "abstract": "PRIVATE_ABSTRACT",
            "attributes": {"root": "C:/PRIVATE_PATH"},
            **fields,
        },
        "representations": [],
        "relations": [],
        "reading_state": {"user_id": "PRIVATE_USER"},
        "annotations": [{"text": "PRIVATE_NOTE"}],
        "provenance": {"secret": "PRIVATE_SOURCE"},
    }


def test_publication_is_explicit_whitelist_and_actual_confirmed_relation(tmp_path: Path) -> None:
    source = tmp_path / "catalog.jsonl"
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text(
        "domains:\n  - id: agent\n    label: Agent 学习\n"
        "  - id: hidden\n    label: 隐藏领域\n    hidden_from_catalog: true\n",
        encoding="utf-8",
    )
    paper = record(
        "paper",
        "PRIVATE_PAPER_ID",
        title='<img src=x onerror="alert(1)"> Paper',
        source_urls=["https://arxiv.org/abs/2501.12345", "https://localhost/private"],
    )
    paper["relations"] = [
        {
            "relation_type": "implemented_by",
            "confirmation_status": "confirmed",
            "subject_asset_id": "PRIVATE_PAPER_ID",
            "object_asset_id": "PRIVATE_REPO_ID",
        },
        {
            "relation_type": "implemented_by",
            "confirmation_status": "candidate",
            "subject_asset_id": "PRIVATE_PAPER_ID",
            "object_asset_id": "CANDIDATE_REPO",
        },
        {
            "relation_type": "implemented_by",
            "confirmation_status": "confirmed",
            "subject_asset_id": "WRONG_PAPER",
            "object_asset_id": "CANDIDATE_REPO",
        },
    ]
    rows = [
        paper,
        record("repository", "PRIVATE_REPO_ID", source_urls=["https://github.com/org/public-code"]),
        record(
            "repository", "CANDIDATE_REPO", source_urls=["https://github.com/org/not-confirmed"]
        ),
        record("note", "NOTE_ID", source_urls=["https://arxiv.org/abs/2502.12345"]),
        record("paper", "NO_PUBLIC", source_urls=["https://localhost/private"]),
        record(
            "paper",
            "HIDDEN_PAPER",
            domains=["hidden"],
            source_urls=["https://arxiv.org/abs/2503.12345"],
        ),
        record(
            "paper",
            "DELETED_PAPER",
            deleted_at="2026-01-01",
            source_urls=["https://arxiv.org/abs/2504.12345"],
        ),
        record("paper", "DUPLICATE", source_urls=["https://arxiv.org/pdf/2501.12345v2.pdf"]),
    ]
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    catalog = builder.build_catalog(source, taxonomy, "2026-10-04")
    assert len(catalog["papers"]) == 1
    public = catalog["papers"][0]
    assert set(public) == builder.RECORD_FIELDS
    assert public["code_urls"] == ["https://github.com/org/public-code"]
    assert public["domains"] == ["agent"]
    serialized = json.dumps(catalog)
    for secret in (
        "PRIVATE_",
        "reading_state",
        "annotations",
        "provenance",
        "visibility",
        "not-confirmed",
    ):
        assert secret not in serialized
    assert catalog["stats"]["excluded_no_public_paper_url"] == 1
    assert catalog["stats"]["excluded_no_visible_domain"] == 1
    assert catalog["stats"]["excluded_deleted"] == 1
    assert catalog["stats"]["merged_same_public_url"] == 1


def test_rich_text_and_paths_cannot_be_coerced_into_metadata() -> None:
    assert builder.clean_text({"secret": "value"}) == ""
    assert builder.clean_text("C:\\Users\\private\\notes") == ""
    assert builder.clean_text("D:/private/notes") == ""
    assert builder.clean_text("Normal  title\n here") == "Normal title here"


def test_public_identity_deduplication_is_transitive(tmp_path: Path) -> None:
    taxonomy = tmp_path / "taxonomy.yaml"
    taxonomy.write_text("domains:\n  - id: agent\n    label: Agent\n", encoding="utf-8")
    first = "https://arxiv.org/abs/2501.12345"
    second = "https://doi.org/10.1234/example"
    rows = [
        record("paper", "A", source_urls=[first]),
        record("paper", "B", source_urls=[second]),
        record("paper", "BRIDGE", source_urls=[first, second]),
    ]
    source = tmp_path / "catalog.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    catalog = builder.build_catalog(source, taxonomy, "2026-10-04")
    assert len(catalog["papers"]) == 1
    assert set(catalog["papers"][0]["paper_urls"]) == {first, second}
    assert catalog["stats"]["merged_same_public_url"] == 2


def test_generated_catalog_boundary() -> None:
    catalog = json.loads((ROOT / "site/dist/assets/catalog.json").read_text(encoding="utf-8"))
    assert catalog["snapshot_date"] == "2026-10-04"
    ids = [paper["id"] for paper in catalog["papers"]]
    assert len(ids) == len(set(ids))
    assert catalog["stats"]["published_papers"] == len(ids)
    domains = {domain["id"] for domain in catalog["domains"]}
    assert "generation" not in domains and "navigation-compression" not in domains
    for paper in catalog["papers"]:
        assert set(paper) == builder.RECORD_FIELDS
        assert re.fullmatch(r"[a-f0-9]{20}", paper["id"])
        assert paper["paper_urls"]
        assert set(paper["domains"]) <= domains
        for url in paper["paper_urls"]:
            assert builder.public_url(url) == url
        for url in paper["code_urls"]:
            assert builder.public_url(url, code=True) == url


def test_evidenced_display_correction_clears_only_the_known_mismatched_translation() -> None:
    path = ROOT / "site/metadata-corrections.json"
    correction = json.loads(path.read_text(encoding="utf-8"))["corrections"][0]
    entry = {
        "title": correction["expected_title"],
        "title_zh": "Audible World Models：为 3D 世界生成具空间感的声音",
        "paper_urls": [correction["url"]],
    }
    unrelated = {"title": "Other paper", "title_zh": "其他论文", "paper_urls": []}
    assert builder.apply_metadata_corrections([entry, unrelated], path) == 1
    assert entry["title_zh"] == ""
    assert entry["title"] == correction["expected_title"]
    assert unrelated["title_zh"] == "其他论文"
    assert correction["evidence_url"] == correction["url"]
    assert correction["verified_on"] == "2026-10-29"
    entry["title"] = "Different identity at the same URL"
    with pytest.raises(ValueError, match="title no longer matches"):
        builder.apply_metadata_corrections([entry], path)


def test_generated_catalog_includes_the_known_metadata_correction() -> None:
    catalog = json.loads((ROOT / "site/dist/assets/catalog.json").read_text(encoding="utf-8"))
    matches = [
        paper
        for paper in catalog["papers"]
        if "https://arxiv.org/abs/2605.30349" in paper["paper_urls"]
    ]
    assert len(matches) == 1
    assert matches[0]["title"] == "AdaState: Self-Evolving Anchors for Streaming Video Generation"
    assert matches[0]["title_zh"] == ""
    assert catalog["stats"]["metadata_corrections_applied"] == 1


def test_static_safety_and_accessibility_contract() -> None:
    index = (ROOT / "site/dist/index.html").read_text(encoding="utf-8")
    script = (ROOT / "site/dist/assets/app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert "textContent" in script
    assert 'aria-live="polite"' in index and 'role="search"' in index
    for ident in [
        "search",
        "domain",
        "venue",
        "year",
        "sort",
        "clear",
        "previous",
        "next",
        "empty-state",
        "error-state",
        "retry",
    ]:
        assert f'id="{ident}"' in index
    for ident in ["search", "domain", "venue", "year", "sort"]:
        assert f'for="{ident}"' in index
    assert "Content-Security-Policy" in index
    assert "no hosted papers" not in index  # User-facing copy is Chinese.
    assert "不托管论文 PDF" in index and "静态快照" in index
    assert "https://research-asset-hub.aurora-coati-0241.chatgpt.site/" in index


def test_search_filter_empty_result_and_pagination() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for the pure JavaScript regression checks")
    core_source = (ROOT / "site/dist/assets/catalog-core.js").read_bytes()
    core = "data:text/javascript;base64," + base64.b64encode(core_source).decode("ascii")
    script = f"""
import assert from 'node:assert/strict';
import {{ preparePapers, filterPapers, paginate, pageNumbers, venueKey }} from {json.dumps(core)};
const papers = preparePapers(Array.from({{length: 45}}, (_, i) => ({{
  id: String(i), title: `World Model ${{i}}`, title_zh: '世界模型', year: 2024 + i % 3,
  venue: i % 2 ? 'ICML 2026' : 'ICLR 2025', domains: [i % 2 ? 'agent' : 'world-model'],
  paper_urls: ['https://arxiv.org/abs/2501.12345'], code_urls: []
}})), new Map([['agent', '智能体'], ['world-model', '世界模型']]));
const base = {{q:'', domain:'', venue:'', year:'', sort:'newest'}};
assert.equal(venueKey('ICML 2026'), 'ICML');
assert.equal(filterPapers(papers, {{...base, q:'WORLD model'}}).length,45);
assert.equal(filterPapers(papers, {{...base, q:'不存在的词'}}).length,0);
assert.equal(filterPapers(papers, {{...base, q:'智能体'}}).length,22);
assert.equal(filterPapers(papers, {{...base, domain:'agent',venue:'ICLR'}}).length,0);
assert(filterPapers(papers, {{...base,year:'2026'}}).every(p => p.year === 2026));
assert.equal(paginate(papers,1).items.length,20);
assert.equal(paginate(papers,3).items.length,5);
assert.equal(paginate(papers,999).page,3);
assert.equal(paginate(papers,-1).page,1);
assert.equal(paginate([],2).items.length,0);
assert.equal(paginate([],2).page,1);
assert.equal(paginate(papers,Infinity).page,3);
assert.deepEqual(pageNumbers(1,1),[1]);
assert.deepEqual(pageNumbers(4,9),[1,'…',3,4,5,'…',9]);
assert.equal(filterPapers(papers,base)[0].year,2026);
assert.equal(filterPapers(papers,{{...base,sort:'oldest'}})[0].year,2024);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
