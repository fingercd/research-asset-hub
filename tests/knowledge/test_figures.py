from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from packages.knowledge.figures import (
    SourceHashMismatch,
    apply_cached_figure_corrections,
    apply_caption_review_warnings,
    extract_pdf_figures,
    normalize_bbox,
)


def _pdf(path: Path, *, rotation: int = 0) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=400, height=300)
    page.draw_rect(pymupdf.Rect(40, 50, 120, 100), color=(0.8, 0.1, 0.1), width=2)
    page.draw_line((45, 95), (115, 55), color=(0.1, 0.1, 0.8), width=1.5)
    page.insert_text((40, 140), "Figure 1: A small vector figure", fontsize=10)
    if rotation:
        page.set_rotation(rotation)
    document.save(path)
    document.close()
    return path


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalize_bbox_converts_units_and_rotation() -> None:
    assert normalize_bbox(
        [100, 200, 300, 600],
        page_width=400,
        page_height=300,
        coordinate_system="normalized_1000_top_left",
        page_rotation=90,
    ) == pytest.approx((80, 210, 240, 270))
    assert normalize_bbox(
        [10, 20, 110, 70],
        page_width=400,
        page_height=300,
        coordinate_system="pixels_top_left",
        dpi=144,
    ) == pytest.approx((5, 10, 55, 35))
    assert normalize_bbox(
        [10, 20, 110, 70],
        page_width=400,
        page_height=300,
        coordinate_system="pdf_points_bottom_left",
    ) == pytest.approx((10, 230, 110, 280))


def test_pymupdf_candidate_has_caption_stable_id_and_relative_crops(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "paper.pdf")
    asset_id = "00000000-0000-0000-0000-000000000001"
    manifest = extract_pdf_figures(
        pdf_path,
        tmp_path / "derived" / asset_id,
        asset_id=asset_id,
        representation_id="00000000-0000-0000-0000-000000000002",
        source_hash=_sha256(pdf_path),
    )

    assert len(manifest["figures"]) == 1
    figure = manifest["figures"][0]
    assert figure["figure_number"] == "1"
    assert figure["caption"].startswith("Figure 1: A small vector figure")
    assert figure["page"] == 1
    assert figure["page_index"] == 0
    assert figure["coordinate_system"] == "pdf_points_top_left"
    assert figure["quality_status"] == "candidate"
    assert re.fullmatch(r"[A-Za-z0-9_-]+", figure["figure_id"])
    assert (tmp_path / "derived" / asset_id / figure["image_path"]).is_file()
    assert (tmp_path / "derived" / asset_id / figure["preview_path"]).is_file()
    assert figure["preview_bytes"] <= 5 * 1024 * 1024
    assert figure["overlay_path"].endswith("page-0001.png")
    assert manifest["source_hash"] == _sha256(pdf_path)


def test_rotated_page_crop_uses_unrotated_bbox_and_rotated_render_clip(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "rotated.pdf", rotation=90)
    asset_id = "00000000-0000-0000-0000-000000000003"
    manifest = extract_pdf_figures(
        pdf_path,
        tmp_path / "derived" / asset_id,
        asset_id=asset_id,
        representation_id="00000000-0000-0000-0000-000000000004",
        source_hash=_sha256(pdf_path),
        dpi=72,
        preview_dpi=72,
    )
    figure = manifest["figures"][0]

    assert figure["rotation"] == 90
    bbox = pymupdf.Rect(*figure["bbox"])
    rotated = bbox * pymupdf.Matrix(0, 1, -1, 0, 300, 0)
    with Image.open(tmp_path / "derived" / asset_id / figure["image_path"]) as image:
        assert image.size == (round(rotated.width), round(rotated.height))


def test_pdffigures2_import_excludes_tables_and_preserves_page_bbox(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "paper.pdf")
    asset_id = "00000000-0000-0000-0000-000000000005"
    rows = [
        {
            "figType": "Figure",
            "name": "A.1",
            "caption": "Figure A.1: PDFFigures2 caption",
            "captionBoundary": {"x1": 40, "x2": 180, "y1": 130, "y2": 145},
            "page": 0,
            "regionBoundary": {"x1": 35, "x2": 140, "y1": 40, "y2": 110},
        },
        {
            "figType": "Table",
            "name": "1",
            "caption": "Table 1: excluded",
            "page": 0,
            "regionBoundary": {"x1": 30, "x2": 150, "y1": 160, "y2": 220},
        },
    ]
    manifest = extract_pdf_figures(
        pdf_path,
        tmp_path / "derived" / asset_id,
        asset_id=asset_id,
        representation_id="00000000-0000-0000-0000-000000000006",
        source_hash=_sha256(pdf_path),
        pdffigures2_results=rows,
        run_pymupdf_heuristic=False,
    )

    assert len(manifest["figures"]) == 1
    figure = manifest["figures"][0]
    assert figure["figure_number"] == "A.1"
    assert figure["bbox"] == [35.0, 40.0, 140.0, 110.0]
    assert figure["caption_bbox"] == [40.0, 130.0, 180.0, 145.0]
    assert figure["quality_status"] == "candidate"
    assert figure["detection_source"] == "pdffigures2"
    assert manifest["pdffigures2_tables_excluded"] == 1
    assert any(item["code"] == "pdffigures2_tables_excluded" for item in manifest["warnings"])


def test_human_bbox_correction_requires_explicit_review_flag(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "paper.pdf")
    asset_id = "00000000-0000-0000-0000-000000000007"
    root = tmp_path / "derived" / asset_id
    arguments = {
        "asset_id": asset_id,
        "representation_id": "00000000-0000-0000-0000-000000000008",
        "source_hash": _sha256(pdf_path),
    }
    initial = extract_pdf_figures(pdf_path, root, **arguments)
    figure_id = initial["figures"][0]["figure_id"]
    corrections = {
        figure_id: {
            "page_index": 0,
            "bbox": [35, 45, 125, 105],
            "coordinate_system": "pdf_points_top_left",
            "review_notes": "Included the axis labels.",
        }
    }

    corrected = extract_pdf_figures(pdf_path, root, **arguments, corrections=corrections)
    assert corrected["figures"][0]["bbox"] == [35, 45, 125, 105]
    assert corrected["figures"][0]["quality_status"] == "candidate"

    reviewed = extract_pdf_figures(
        pdf_path,
        root,
        **arguments,
        corrections=corrections,
        mark_corrected_reviewed=True,
    )
    assert reviewed["figures"][0]["quality_status"] == "reviewed"


def test_pdf_hash_mismatch_fails_before_writing_candidates(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "paper.pdf")
    asset_id = "00000000-0000-0000-0000-000000000009"
    output = tmp_path / "derived" / asset_id

    with pytest.raises(SourceHashMismatch):
        extract_pdf_figures(
            pdf_path,
            output,
            asset_id=asset_id,
            representation_id="00000000-0000-0000-0000-000000000010",
            source_hash="0" * 64,
        )
    assert not (output / "figures.json").exists()


def test_multiple_caption_labels_require_split_before_review(tmp_path: Path) -> None:
    pdf_path = _pdf(tmp_path / "paper.pdf")
    asset_id = "00000000-0000-0000-0000-000000000011"
    root = tmp_path / "derived" / asset_id
    rows = [
        {
            "figType": "Figure",
            "name": "1",
            "page": 0,
            "caption": "Figure 1: Left samples Figure 2: Right samples",
            "captionBoundary": {"x1": 40, "x2": 180, "y1": 130, "y2": 145},
            "regionBoundary": {"x1": 35, "x2": 140, "y1": 40, "y2": 110},
        }
    ]
    arguments = {
        "asset_id": asset_id,
        "representation_id": "00000000-0000-0000-0000-000000000012",
        "source_hash": _sha256(pdf_path),
        "pdffigures2_results": rows,
        "run_pymupdf_heuristic": False,
    }
    initial = extract_pdf_figures(pdf_path, root, **arguments)
    original = initial["figures"][0]
    assert original["requires_review"] is True
    assert any(item["code"] == "multiple_caption_labels" for item in initial["warnings"])
    correction = {original["figure_id"]: {"page_index": 0, "bbox": [35, 40, 140, 110]}}
    reviewed = extract_pdf_figures(
        pdf_path, root, **arguments, corrections=correction, mark_corrected_reviewed=True
    )
    assert reviewed["figures"][0]["quality_status"] == "candidate"
    splits = {
        original["figure_id"]: {
            "split_figures": [
                {
                    "figure_number": "1",
                    "caption": "Figure 1: Left samples",
                    "page_index": 0,
                    "bbox": [35, 40, 85, 110],
                },
                {
                    "figure_number": "2",
                    "caption": "Figure 2: Right samples",
                    "page_index": 0,
                    "bbox": [85, 40, 140, 110],
                },
            ]
        }
    }
    split = extract_pdf_figures(pdf_path, root, **arguments, corrections=splits)
    assert [figure["figure_number"] for figure in split["figures"]] == ["1", "2"]
    assert all(figure["quality_status"] == "candidate" for figure in split["figures"])
    assert all(figure["requires_review"] is False for figure in split["figures"])
    assert all(figure["detection_source"] == "manual_split" for figure in split["figures"])
    assert (root / "figures.json").is_file()


def test_caption_cross_reference_does_not_look_like_second_caption() -> None:
    manifest = {
        "warnings": [],
        "figures": [
            {
                "figure_id": "sample-fig-10",
                "page_index": 0,
                "caption": (
                    "Figure 10. Object removal as shown in Figure 10a. "
                    "For more results, see Fig. 21. Compare to Figure 2."
                ),
                "quality_status": "candidate",
            }
        ],
    }
    apply_caption_review_warnings(manifest)
    assert manifest["figures"][0]["requires_review"] is False
    assert manifest["warnings"] == []


@pytest.mark.parametrize(
    "caption_box",
    [
        {"x1": 40, "x2": 180, "y1": 305, "y2": 315},
        {"x1": 40, "x2": 180, "y1": 130, "y2": 130},
    ],
)
def test_valid_figure_survives_invalid_caption_box(
    tmp_path: Path, caption_box: dict[str, int]
) -> None:
    path = _pdf(tmp_path / "paper.pdf")
    manifest = extract_pdf_figures(
        path,
        tmp_path / "derived",
        asset_id="paper",
        representation_id="pdf",
        source_hash=_sha256(path),
        run_pymupdf_heuristic=False,
        pdffigures2_results=[
            {
                "figType": "Figure",
                "name": "1",
                "page": 0,
                "caption": "Figure 1: Kept caption text",
                "regionBoundary": {"x1": 35, "x2": 140, "y1": 40, "y2": 110},
                "captionBoundary": caption_box,
            }
        ],
    )
    figure = manifest["figures"][0]
    assert figure["bbox"] == [35, 40, 140, 110]
    assert figure["caption"] == "Figure 1: Kept caption text"
    assert figure["caption_bbox"] is None
    assert figure["requires_review"] is True
    assert any(w["code"] == "pdffigures2_invalid_caption_bbox" for w in manifest["warnings"])
    assert not any(w["code"] == "pdffigures2_invalid_item" for w in manifest["warnings"])


def test_cached_additions_are_idempotent_and_preserve_existing_crops(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "paper.pdf", rotation=90)
    root = tmp_path / "derived"
    original = extract_pdf_figures(
        path, root, asset_id="paper", representation_id="pdf", source_hash=_sha256(path)
    )
    before = (root / original["figures"][0]["image_path"]).read_bytes()
    addition = {
        "figure_number": "2",
        "caption": "Figure 2: Added region",
        "page_index": 0,
        "bbox": [180, 50, 280, 100],
        "reviewer": "agent-visual-review",
        "human_reviewed": False,
    }
    first = apply_cached_figure_corrections(
        path, root, source_hash=_sha256(path), add_figures=[addition], mark_corrected_reviewed=True
    )
    assert len(first["figures"]) == 2
    assert first["figures"][1]["quality_status"] == "agent_visual_review"
    assert first["figures"][1]["correction_provenance"]["human_reviewed"] is False
    with Image.open(root / first["figures"][1]["image_path"]) as crop:
        assert crop.height > crop.width  # Unrotated 100x50 region rendered at 90 degrees.
    second = apply_cached_figure_corrections(
        path, root, source_hash=_sha256(path), add_figures=[addition]
    )
    assert [r["figure_id"] for r in first["figures"]] == [r["figure_id"] for r in second["figures"]]
    assert second["last_correction_run"]["changed_figures"] == 0
    assert (root / original["figures"][0]["image_path"]).read_bytes() == before


@pytest.mark.parametrize(
    "bad_row",
    [
        {"page_index": 1, "bbox": [180, 50, 280, 100]},
        {"page_index": 0, "bbox": [180, 50, 410, 100]},
        {"page_index": 0, "bbox": [180, 50, 180, 100]},
    ],
)
def test_cached_additions_reject_bad_page_and_bbox_without_manifest_write(
    tmp_path: Path, bad_row: dict[str, object]
) -> None:
    path = _pdf(tmp_path / "paper.pdf")
    root = tmp_path / "derived"
    extract_pdf_figures(
        path, root, asset_id="paper", representation_id="pdf", source_hash=_sha256(path)
    )
    before = (root / "figures.json").read_bytes()
    with pytest.raises(ValueError):
        apply_cached_figure_corrections(
            path,
            root,
            source_hash=_sha256(path),
            add_figures=[{"figure_number": "2", "caption": "Figure 2: Added", **bad_row}],
        )
    assert (root / "figures.json").read_bytes() == before
    with pytest.raises(SourceHashMismatch):
        apply_cached_figure_corrections(path, root, source_hash="0" * 64, add_figures=[])


def test_cached_additions_reject_duplicate_logical_figures(tmp_path: Path) -> None:
    path = _pdf(tmp_path / "paper.pdf")
    root = tmp_path / "derived"
    extract_pdf_figures(
        path, root, asset_id="paper", representation_id="pdf", source_hash=_sha256(path)
    )
    row = {
        "figure_number": "2",
        "caption": "Figure 2: Added",
        "page_index": 0,
        "bbox": [180, 50, 280, 100],
    }
    with pytest.raises(ValueError, match="Duplicate logical"):
        apply_cached_figure_corrections(
            path, root, source_hash=_sha256(path), add_figures=[row, row]
        )


@pytest.mark.parametrize("source_clipped", [False, True])
def test_explicit_addition_preserves_agent_review_status_in_fresh_extraction(
    tmp_path: Path,
    source_clipped: bool,
) -> None:
    path = _pdf(tmp_path / "paper.pdf")
    manifest = extract_pdf_figures(
        path,
        tmp_path / "derived",
        asset_id="paper",
        representation_id="pdf",
        source_hash=_sha256(path),
        pdffigures2_results=[],
        run_pymupdf_heuristic=False,
        mark_corrected_reviewed=True,
        add_figures=[
            {
                "page_index": 0,
                "bbox": [40, 50, 120, 100],
                "figure_number": "1",
                "caption": "Figure 1: Explicitly recovered",
                "reviewer": "agent-visual-review",
                "human_reviewed": False,
                "source_clipped": source_clipped,
            }
        ],
    )
    assert manifest["figures"][0]["quality_status"] == (
        "source_clipped_candidate" if source_clipped else "agent_visual_review"
    )
    assert manifest["figures"][0]["requires_review"] is source_clipped
    assert manifest["figures"][0]["correction_provenance"]["human_reviewed"] is False


def test_addition_config_requires_original_pdf_hash() -> None:
    from scripts.extract_pilot_figures import _additions_for_entry

    entry = {"asset_id": "paper", "source_hash": "a" * 64}
    payload = {"papers": {"paper": {"add_figures": [{"figure_number": "1"}]}}}
    with pytest.raises(ValueError, match="source_hash"):
        _additions_for_entry(payload, entry)
    payload["papers"]["paper"]["source_hash"] = "a" * 64
    assert _additions_for_entry(payload, entry)[0]["human_reviewed"] is False
