from __future__ import annotations

import json
from pathlib import Path

from pypdf import PdfWriter

from tools.paper_census.census import DEFAULT_EXCLUDED_DIR_NAMES, merge_reports, scan_roots


def _write_pdf(path: Path, pages: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": "Census fixture", "/Author": "Fixture Author"})
    with path.open("wb") as handle:
        writer.write(handle)


def _make_fixture(root: Path) -> None:
    _write_pdf(root / "papers" / "paper.pdf", pages=2)
    (root / "papers" / "paper.html").write_text("<html>fixture</html>", encoding="utf-8")
    (root / "manifest.json").write_text('{"items": []}', encoding="utf-8")
    (root / "paper-catalog.yaml").write_text("papers: []\n", encoding="utf-8")
    (root / "auth.json").write_text('{"token": "must-not-be-read"}', encoding="utf-8")
    for excluded in DEFAULT_EXCLUDED_DIR_NAMES:
        _write_pdf(root / excluded / "excluded.pdf")


def test_scan_exclusions_pdf_health_and_checkpoint_resume(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _make_fixture(root)
    prefix = tmp_path / "reports" / "census"

    first = scan_roots([root], output_prefix=prefix, core_root=root, checkpoint_every=1)
    assert first["stats"]["by_kind"] == {"catalog": 1, "html": 1, "manifest": 1, "pdf": 1}
    assert first["stats"]["pdf"]["magic_ok"] == 1
    assert first["stats"]["pdf"]["with_page_count"] == 1
    assert first["stats"]["pdf"]["page_total"] == 2
    assert first["discovery"]["directories_excluded"] == len(DEFAULT_EXCLUDED_DIR_NAMES)
    assert first["discovery"]["sensitive_files_skipped"] == 1
    assert all(record["root_in_core"] for record in first["records"])
    assert not any("auth.json" in json.dumps(record) for record in first["records"])
    assert prefix.with_suffix(".json").exists()
    assert prefix.with_suffix(".csv").exists()
    assert prefix.with_suffix(".md").exists()

    second = scan_roots([root], output_prefix=prefix, core_root=root, checkpoint_every=1)
    assert second["hashes"] == {"computed": 0, "reused_from_checkpoint": 4}
    assert all(record["hash_source"] == "checkpoint" for record in second["records"])


def test_two_shards_merge_to_full_record_set(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    _make_fixture(root)
    shard_paths: list[Path] = []
    for shard_index in range(2):
        prefix = tmp_path / f"shard-{shard_index}"
        scan_roots(
            [root],
            output_prefix=prefix,
            shard_count=2,
            shard_index=shard_index,
        )
        shard_paths.append(prefix.with_suffix(".json"))

    merged = merge_reports(shard_paths, output_prefix=tmp_path / "merged")
    assert merged["scope"]["complete"] is True
    assert merged["stats"]["record_count"] == 4
    assert len({record["record_id"] for record in merged["records"]}) == 4
