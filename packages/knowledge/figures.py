"""Create reviewable, local figure candidates from scientific-paper PDFs."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

import pymupdf
from PIL import Image, ImageDraw, ImageFont

EXTRACTOR_NAME = "pymupdf-graphic-clusters"
EXTRACTOR_VERSION = "1"
COORDINATE_SYSTEM: Literal["pdf_points_top_left"] = "pdf_points_top_left"
PREVIEW_MAX_BYTES = 4_500_000
_FIGURE_CAPTION = re.compile(
    r"^\s*(?:figure|fig\.?\s*)\s*"
    r"(?P<number>[A-Z]{0,2}\s*\d+(?:[.-]\d+)*(?:[A-Z])?)\b",
    re.IGNORECASE,
)
_SAFE_ID_PART = re.compile(r"[^A-Za-z0-9_-]+")

BBox = tuple[float, float, float, float]
BBoxCoordinateSystem = Literal[
    "pdf_points_top_left",
    "pdffigures2_72dpi_top_left",
    "pdf_points_bottom_left",
    "visible_points_top_left",
    "visible_points_bottom_left",
    "normalized_top_left",
    "normalized_bottom_left",
    "normalized_1000_top_left",
    "pixels_top_left",
    "pixels_bottom_left",
]


class FigureExtractionError(RuntimeError):
    """Raised when the requested PDF cannot be safely used for figure extraction."""


class SourceHashMismatch(FigureExtractionError):
    """Raised when the local PDF bytes differ from the pilot manifest fingerprint."""


@dataclass(frozen=True, slots=True)
class _Caption:
    number: str
    text: str
    bbox: BBox


@dataclass(frozen=True, slots=True)
class _Candidate:
    page_index: int
    bbox: BBox
    caption: _Caption | None
    caption_bbox: BBox | None
    figure_number: str | None
    source_kind: str
    note: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_bbox(bbox: Sequence[float]) -> BBox:
    if len(bbox) != 4:
        raise ValueError("bbox must contain exactly four coordinates.")
    values = tuple(float(value) for value in bbox)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("bbox coordinates must be finite numbers.")
    x0, y0, x1, y1 = values
    if x1 <= x0 or y1 <= y0:
        raise ValueError("bbox must have positive width and height.")
    return values  # type: ignore[return-value]


def _unrotate_visible_bbox(bbox: BBox, width: float, height: float, rotation: int) -> BBox:
    x0, y0, x1, y1 = bbox
    if rotation == 0:
        return bbox
    if rotation == 90:
        return y0, height - x1, y1, height - x0
    if rotation == 180:
        return width - x1, height - y1, width - x0, height - y0
    if rotation == 270:
        return width - y1, x0, width - y0, x1
    raise ValueError("page_rotation must be one of 0, 90, 180, or 270 degrees.")


def normalize_bbox(
    bbox: Sequence[float],
    *,
    page_width: float,
    page_height: float,
    coordinate_system: BBoxCoordinateSystem = COORDINATE_SYSTEM,
    page_rotation: int = 0,
    dpi: float | None = None,
) -> BBox:
    """Convert an external bbox into unrotated, top-left PDF-point coordinates.

    ``page_width`` and ``page_height`` describe the unrotated CropBox in points.
    Normalized, pixel, and visible-point coordinates refer to the rendered page
    orientation, so they are derotated before they are used to clip the PDF.
    """
    rect = _validate_bbox(bbox)
    if page_width <= 0 or page_height <= 0:
        raise ValueError("page dimensions must be positive.")
    if page_rotation not in {0, 90, 180, 270}:
        raise ValueError("page_rotation must be one of 0, 90, 180, or 270 degrees.")

    if coordinate_system in {"pdf_points_top_left", "pdffigures2_72dpi_top_left"}:
        return rect
    if coordinate_system == "pdf_points_bottom_left":
        x0, y0, x1, y1 = rect
        return x0, page_height - y1, x1, page_height - y0

    visible_width = page_height if page_rotation in {90, 270} else page_width
    visible_height = page_width if page_rotation in {90, 270} else page_height
    x0, y0, x1, y1 = rect

    if coordinate_system in {"visible_points_top_left", "visible_points_bottom_left"}:
        pass
    elif coordinate_system in {"normalized_top_left", "normalized_bottom_left"}:
        x0, x1 = x0 * visible_width, x1 * visible_width
        y0, y1 = y0 * visible_height, y1 * visible_height
    elif coordinate_system == "normalized_1000_top_left":
        x0, x1 = x0 * visible_width / 1000, x1 * visible_width / 1000
        y0, y1 = y0 * visible_height / 1000, y1 * visible_height / 1000
    elif coordinate_system in {"pixels_top_left", "pixels_bottom_left"}:
        if dpi is None or dpi <= 0:
            raise ValueError("positive dpi is required for pixel bboxes.")
        pixels_to_points = 72 / dpi
        x0, x1 = x0 * pixels_to_points, x1 * pixels_to_points
        y0, y1 = y0 * pixels_to_points, y1 * pixels_to_points
    else:
        raise ValueError(f"Unsupported bbox coordinate system: {coordinate_system}.")

    if coordinate_system.endswith("bottom_left"):
        y0, y1 = visible_height - y1, visible_height - y0
    return _unrotate_visible_bbox((x0, y0, x1, y1), page_width, page_height, page_rotation)


def _box_union(boxes: Sequence[BBox]) -> BBox:
    return (
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    )


def _box_gap(first: BBox, second: BBox) -> tuple[float, float]:
    x_gap = max(0.0, first[0] - second[2], second[0] - first[2])
    y_gap = max(0.0, first[1] - second[3], second[1] - first[3])
    return x_gap, y_gap


def _merge_graphics(boxes: Sequence[BBox], *, gap: float = 18.0) -> list[BBox]:
    merged: list[BBox] = []
    for box in sorted(boxes, key=lambda item: (item[1], item[0], item[3], item[2])):
        pending = box
        changed = True
        while changed:
            changed = False
            remaining: list[BBox] = []
            for existing in merged:
                x_gap, y_gap = _box_gap(pending, existing)
                if x_gap <= gap and y_gap <= gap:
                    pending = _box_union((pending, existing))
                    changed = True
                else:
                    remaining.append(existing)
            merged = remaining
        merged.append(pending)
    return sorted(merged, key=lambda item: (item[1], item[0], item[3], item[2]))


def _text_lines(page: pymupdf.Page) -> list[tuple[str, BBox]]:
    lines: list[tuple[str, BBox]] = []
    for block in page.get_text("dict").get("blocks", []):  # type: ignore[no-untyped-call]
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            cleaned = re.sub(r"\s+", " ", text).strip()
            if cleaned:
                lines.append((cleaned, _validate_bbox(line["bbox"])))
    return sorted(lines, key=lambda item: (item[1][1], item[1][0]))


def _captions_from_page(page: pymupdf.Page, lines: Sequence[tuple[str, BBox]]) -> list[_Caption]:
    text_data = page.get_text("dict")  # type: ignore[no-untyped-call]
    text_blocks = [block for block in text_data.get("blocks", []) if block.get("type") == 0]
    captions: list[_Caption] = []
    seen: set[tuple[str, BBox]] = set()
    for block in text_blocks:
        block_lines: list[tuple[str, BBox]] = []
        for line in block.get("lines", []):
            text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
            cleaned = re.sub(r"\s+", " ", text).strip()
            if cleaned:
                block_lines.append((cleaned, _validate_bbox(line["bbox"])))
        for index, (text, bbox) in enumerate(block_lines):
            match = _FIGURE_CAPTION.match(text)
            if match is None:
                continue
            number = re.sub(r"\s+", "", match.group("number"))
            collected = [(text, bbox)]
            prior = bbox
            for next_text, next_bbox in block_lines[index + 1 :]:
                if _FIGURE_CAPTION.match(next_text):
                    break
                vertical_gap = max(0.0, next_bbox[1] - prior[3])
                if vertical_gap > 15 or abs(next_bbox[0] - bbox[0]) > 36:
                    break
                collected.append((next_text, next_bbox))
                prior = next_bbox
            caption_text = " ".join(part[0] for part in collected)
            caption_bbox = _box_union([part[1] for part in collected])
            key = number, caption_bbox
            if key not in seen:
                captions.append(_Caption(number, caption_text, caption_bbox))
                seen.add(key)
    return sorted(captions, key=lambda item: (item.bbox[1], item.bbox[0]))


def _graphic_boxes(page: pymupdf.Page) -> tuple[list[BBox], bool]:
    boxes: list[BBox] = []
    try:
        boxes.extend(
            _validate_bbox(tuple(rect))
            for rect in page.cluster_drawings(x_tolerance=8, y_tolerance=8)
            if rect.width > 0 and rect.height > 0
        )
    except (RuntimeError, ValueError):
        boxes.extend(
            _validate_bbox(tuple(drawing["rect"]))
            for drawing in page.get_drawings()
            if drawing["rect"].width > 0 and drawing["rect"].height > 0
        )

    image_boxes = [
        _validate_bbox(tuple(info["bbox"]))
        for info in page.get_image_info()
        if info.get("bbox") is not None
    ]
    boxes.extend(image_boxes)
    page_box = page.cropbox
    min_area = max(45.0, page_box.width * page_box.height * 0.00015)
    groups = [
        box for box in _merge_graphics(boxes) if (box[2] - box[0]) * (box[3] - box[1]) >= min_area
    ]
    full_page_image = any(
        image[2] - image[0] >= page_box.width * 0.96
        and image[3] - image[1] >= page_box.height * 0.96
        for image in image_boxes
    )
    return groups, full_page_image


def _caption_score(graphic: BBox, caption: _Caption) -> float:
    x_gap, y_gap = _box_gap(graphic, caption.bbox)
    graphic_center = (graphic[0] + graphic[2]) / 2
    caption_center = (caption.bbox[0] + caption.bbox[2]) / 2
    width = min(graphic[2] - graphic[0], caption.bbox[2] - caption.bbox[0])
    overlap = max(0.0, min(graphic[2], caption.bbox[2]) - max(graphic[0], caption.bbox[0]))
    overlap_ratio = overlap / width if width > 0 else 0.0
    center_penalty = max(0.0, abs(graphic_center - caption_center) - 24.0)
    return y_gap + 2.0 * x_gap + 0.15 * center_penalty + (0.0 if overlap_ratio >= 0.2 else 50.0)


def _candidate_rect_for_caption(
    graphic: BBox,
    graphics: Sequence[BBox],
    caption: _Caption,
    *,
    page_width: float,
    page_height: float,
) -> BBox:
    related = [graphic]
    base_score = _caption_score(graphic, caption)
    side = -1 if graphic[3] <= caption.bbox[1] else 1
    for other in graphics:
        if other == graphic:
            continue
        other_side = -1 if other[3] <= caption.bbox[1] else 1
        if other_side != side or _caption_score(other, caption) > base_score + 36:
            continue
        x_gap, y_gap = _box_gap(graphic, other)
        if x_gap <= max(36.0, (graphic[2] - graphic[0]) * 0.35) and y_gap <= 42:
            related.append(other)
    x0, y0, x1, y1 = _box_union(related)
    padding = 2.0
    return (
        max(0.0, x0 - padding),
        max(0.0, y0 - padding),
        min(page_width, x1 + padding),
        min(page_height, y1 + padding),
    )


def _page_candidates(
    page: pymupdf.Page,
    page_index: int,
    warnings: list[dict[str, Any]],
) -> list[_Candidate]:
    try:
        text_lines = _text_lines(page)
        captions = _captions_from_page(page, text_lines)
        graphics, full_page_image = _graphic_boxes(page)
    except Exception as error:
        warnings.append(
            {
                "code": "page_parse_failed",
                "page_index": page_index,
                "message": f"{type(error).__name__}: {error}",
            }
        )
        return []

    if full_page_image:
        warnings.append(
            {
                "code": "full_page_raster_detected",
                "page_index": page_index,
                "message": (
                    "The page contains a near full-page raster image; figure regions may need "
                    "manual splitting."
                ),
            }
        )
    if captions and not graphics:
        for caption in captions:
            warnings.append(
                {
                    "code": "caption_without_graphic_candidate",
                    "page_index": page_index,
                    "figure_number": caption.number,
                    "message": (
                        "A figure caption was detected but no vector/image region was proposed."
                    ),
                }
            )
        return []
    if not captions and graphics:
        warnings.append(
            {
                "code": "uncaptioned_graphics_detected",
                "page_index": page_index,
                "candidate_count": len(graphics),
                "message": (
                    "Graphic regions have no detected Figure/Fig. caption; they remain "
                    "review candidates."
                ),
            }
        )
    if captions and not graphics:
        return []

    assignments: dict[int, int] = {}
    pairs = sorted(
        (_caption_score(graphic, caption), graphic_index, caption_index)
        for graphic_index, graphic in enumerate(graphics)
        for caption_index, caption in enumerate(captions)
    )
    used_captions: set[int] = set()
    for score, graphic_index, caption_index in pairs:
        if graphic_index in assignments or caption_index in used_captions:
            continue
        if score > page.cropbox.height * 0.45:
            continue
        assignments[graphic_index] = caption_index
        used_captions.add(caption_index)

    candidates: list[_Candidate] = []
    matched_caption: _Caption | None
    for graphic_index, graphic in enumerate(graphics):
        assigned_caption_index = assignments.get(graphic_index)
        matched_caption = (
            captions[assigned_caption_index] if assigned_caption_index is not None else None
        )
        bbox = (
            _candidate_rect_for_caption(
                graphic,
                graphics,
                matched_caption,
                page_width=page.cropbox.width,
                page_height=page.cropbox.height,
            )
            if matched_caption is not None
            else (
                max(0.0, graphic[0] - 2),
                max(0.0, graphic[1] - 2),
                min(page.cropbox.width, graphic[2] + 2),
                min(page.cropbox.height, graphic[3] + 2),
            )
        )
        note = (
            "Heuristic vector/raster region associated with a nearby Figure/Fig. caption; "
            "automatic candidate, not human-reviewed."
            if matched_caption is not None
            else (
                "Heuristic vector/raster region without a matched figure caption; automatic "
                "candidate, not human-reviewed."
            )
        )
        candidates.append(
            _Candidate(
                page_index=page_index,
                bbox=_validate_bbox(bbox),
                caption=matched_caption,
                caption_bbox=matched_caption.bbox if matched_caption is not None else None,
                figure_number=matched_caption.number if matched_caption is not None else None,
                source_kind="raster_and_vector",
                note=note,
            )
        )

    unmatched_captions = [
        caption
        for caption_index, caption in enumerate(captions)
        if caption_index not in used_captions
    ]
    for caption in unmatched_captions:
        warnings.append(
            {
                "code": "caption_not_associated",
                "page_index": page_index,
                "figure_number": caption.number,
                "message": "The caption could not be associated with a nearby graphic region.",
            }
        )
    return candidates


def _safe_id_part(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_ID_PART.sub("-", value.strip()).strip("-_ ")
    return cleaned[:48] or fallback


def _figure_id(asset_id: str, candidate: _Candidate, ordinal: int) -> str:
    number = candidate.figure_number or ""
    label = _safe_id_part(number, fallback="")
    if not label:
        label = f"p{candidate.page_index + 1}"
    stable = json.dumps(
        [candidate.page_index, [round(value, 1) for value in candidate.bbox], number],
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(stable).hexdigest()[:8]
    safe_asset_id = _safe_id_part(asset_id, fallback="asset")
    del ordinal
    return f"{safe_asset_id}-fig-{label}-{digest}"


def _clamp_bbox(bbox: BBox, page_width: float, page_height: float) -> BBox:
    x0, y0, x1, y1 = bbox
    return _validate_bbox(
        (
            max(0.0, x0),
            max(0.0, y0),
            min(page_width, x1),
            min(page_height, y1),
        )
    )


def _pdffigures2_candidates(
    rows: Sequence[Mapping[str, Any]],
    *,
    page_sizes: Mapping[int, tuple[float, float]],
    warnings: list[dict[str, Any]],
) -> tuple[dict[int, list[_Candidate]], int]:
    by_page: dict[int, list[_Candidate]] = {page_index: [] for page_index in page_sizes}
    table_count = 0
    for row_index, row in enumerate(rows):
        figure_type = str(row.get("figType", row.get("figureType", ""))).casefold()
        if figure_type == "table":
            table_count += 1
            continue
        if figure_type != "figure":
            warnings.append(
                {
                    "code": "pdffigures2_unknown_item_type",
                    "page_index": row.get("page"),
                    "message": (
                        f"PDFFigures2 item {row_index} has unsupported type "
                        f"{figure_type!r}; skipped."
                    ),
                }
            )
            continue
        try:
            page_index = int(row["page"])
            width, height = page_sizes[page_index]
            region = row["regionBoundary"]
            bbox = _clamp_bbox(
                normalize_bbox(
                    [region["x1"], region["y1"], region["x2"], region["y2"]],
                    page_width=width,
                    page_height=height,
                    coordinate_system="pdffigures2_72dpi_top_left",
                ),
                width,
                height,
            )
            caption_text = str(row.get("caption", "")).strip()
            number = str(row.get("name", "")).strip()
            if not number:
                caption_match = _FIGURE_CAPTION.match(caption_text)
                number = (
                    re.sub(r"\s+", "", caption_match.group("number"))
                    if caption_match is not None
                    else ""
                )
            caption_boundary = row.get("captionBoundary")
            caption_bbox = None
            if isinstance(caption_boundary, Mapping):
                try:
                    normalized_caption = normalize_bbox(
                        [
                            caption_boundary["x1"],
                            caption_boundary["y1"],
                            caption_boundary["x2"],
                            caption_boundary["y2"],
                        ],
                        page_width=width,
                        page_height=height,
                        coordinate_system="pdffigures2_72dpi_top_left",
                    )
                    if normalized_caption != _clamp_bbox(normalized_caption, width, height):
                        raise ValueError("Caption bbox lies outside the visible page.")
                    caption_bbox = normalized_caption
                except (ValueError, KeyError, TypeError) as error:
                    warnings.append(
                        {
                            "code": "pdffigures2_invalid_caption_bbox",
                            "page_index": page_index,
                            "figure_number": number,
                            "raw_caption_boundary": dict(caption_boundary),
                            "message": f"Figure region retained; caption bbox omitted: {error}",
                            "requires_review": True,
                        }
                    )
            caption = _Caption(number, caption_text, caption_bbox or bbox) if caption_text else None
            by_page[page_index].append(
                _Candidate(
                    page_index=page_index,
                    bbox=bbox,
                    caption=caption,
                    caption_bbox=caption_bbox,
                    figure_number=number or None,
                    source_kind="pdffigures2",
                    note=(
                        "PDFFigures2 caption/layout candidate; automatic result, "
                        "not human-reviewed."
                    ),
                )
            )
        except (KeyError, TypeError, ValueError, IndexError) as error:
            warnings.append(
                {
                    "code": "pdffigures2_invalid_item",
                    "page_index": row.get("page"),
                    "message": (
                        f"PDFFigures2 item {row_index} was skipped: {type(error).__name__}: {error}"
                    ),
                }
            )
    return by_page, table_count


def _rotated_rect(page: pymupdf.Page, bbox: BBox) -> pymupdf.Rect:
    rect = pymupdf.Rect(*bbox)  # type: ignore[no-untyped-call]
    return cast(pymupdf.Rect, rect * page.rotation_matrix)


def _render_crop(page: pymupdf.Page, bbox: BBox, dpi: int) -> Image.Image:
    clip = _rotated_rect(page, bbox) & page.rect
    if clip.is_empty or clip.is_infinite:
        raise FigureExtractionError("The bbox does not intersect the visible page.")
    pixmap = page.get_pixmap(clip=clip, dpi=dpi, alpha=False)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def _save_png(image: Image.Image, path: Path, *, max_bytes: int | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    working = image.convert("RGB")
    while True:
        working.save(path, format="PNG", optimize=True)
        size = path.stat().st_size
        if max_bytes is None or size <= max_bytes:
            return size
        if max(working.size) <= 128:
            raise FigureExtractionError(f"Could not reduce preview below {max_bytes} bytes.")
        width = max(1, int(working.width * 0.8))
        height = max(1, int(working.height * 0.8))
        working = working.resize((width, height), Image.Resampling.LANCZOS)


def _render_overlay(
    page: pymupdf.Page,
    candidates: Sequence[tuple[str, BBox]],
    dpi: int,
) -> Image.Image:
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    draw = ImageDraw.Draw(image)
    scale_x = image.width / page.rect.width
    scale_y = image.height / page.rect.height
    font = ImageFont.load_default()
    for label, bbox in candidates:
        visible = _rotated_rect(page, bbox)
        box = (
            round(visible.x0 * scale_x),
            round(visible.y0 * scale_y),
            round(visible.x1 * scale_x),
            round(visible.y1 * scale_y),
        )
        draw.rectangle(box, outline=(235, 30, 70), width=max(2, round(dpi / 60)))
        draw.rectangle(
            (box[0], box[1], min(image.width, box[0] + 90), box[1] + 16), fill=(235, 30, 70)
        )
        draw.text((box[0] + 3, box[1] + 2), label[:18], fill=(255, 255, 255), font=font)
    return image


def _caption_labels(caption: str) -> list[str]:
    number = r"[A-Z]{0,2}\s*\d+(?:[.-]\d+)*(?:[A-Z])?"
    pattern = rf"\b(?:Figure|Fig\.?)\s+(?P<number>{number})\s*(?P<separator>[:.])"
    labels = []
    for match in re.finditer(pattern, caption, flags=re.IGNORECASE):
        # Periods often end inline references; colon labels are caption headers.
        if match.group("separator") == ".":
            prefix = caption[: match.start()].rstrip()
            if match.end() == len(caption) or re.search(
                r"\b(?:see|to|as|in|of|from|with|and|or|above|below)$",
                prefix,
                flags=re.IGNORECASE,
            ):
                continue
        # Figure 3a / 3b references identify panels of logical Figure 3.
        label = re.sub(r"(?<=\d)[A-Z]$", "", match.group("number"), flags=re.IGNORECASE)
        labels.append(label)
    return labels


def apply_caption_review_warnings(manifest: dict[str, Any]) -> None:
    """Flag captions containing several labels, including cached automatic results."""
    warnings = manifest.setdefault("warnings", [])
    warnings[:] = [w for w in warnings if w.get("code") != "multiple_caption_labels"]
    for figure in manifest["figures"]:
        labels = list(dict.fromkeys(_caption_labels(figure.get("caption") or "")))
        ambiguous = len(labels) > 1
        figure["requires_review"] = (
            ambiguous
            or bool(figure.get("source_clipped"))
            or bool(figure.get("caption_bbox_invalid"))
        )
        if not ambiguous:
            continue
        figure["quality_status"] = "candidate"
        warnings.append(
            {
                "code": "multiple_caption_labels",
                "page_index": figure["page_index"],
                "figure_id": figure["figure_id"],
                "figure_numbers": labels,
                "requires_review": True,
                "message": (
                    "Several figure labels share one crop; inspect and split logical figures."
                ),
            }
        )


def _split_corrected_candidates(
    candidates: Sequence[_Candidate],
    corrections: Mapping[str, Mapping[str, Any]] | None,
    *,
    asset_id: str,
    page_sizes: Mapping[int, tuple[float, float]],
    page_rotations: Mapping[int, int],
) -> tuple[list[_Candidate], dict[str, Mapping[str, Any]]]:
    expanded: list[_Candidate] = []
    remaining = dict(corrections or {})
    for ordinal, candidate in enumerate(candidates, start=1):
        original_id = _figure_id(asset_id, candidate, ordinal)
        correction = remaining.get(original_id, {})
        splits = correction.get("split_figures")
        if splits is None:
            expanded.append(candidate)
            continue
        if not isinstance(splits, list) or len(splits) < 2:
            raise ValueError("split_figures must contain at least two logical figures.")
        del remaining[original_id]
        for split in splits:
            page_index = int(split["page_index"])
            if page_index not in page_sizes:
                raise ValueError("Split figure page_index is outside the PDF.")
            width, height = page_sizes[page_index]
            bbox = _clamp_bbox(
                normalize_bbox(
                    split["bbox"],
                    page_width=width,
                    page_height=height,
                    coordinate_system=split.get("coordinate_system", COORDINATE_SYSTEM),
                    page_rotation=page_rotations[page_index],
                    dpi=split.get("dpi"),
                ),
                width,
                height,
            )
            caption_text = str(split["caption"]).strip()
            number = str(split["figure_number"]).strip()
            if not caption_text or not number or len(_caption_labels(caption_text)) > 1:
                raise ValueError("Every split needs one figure number and one unambiguous caption.")
            split_candidate = _Candidate(
                page_index=page_index,
                bbox=bbox,
                caption=_Caption(number, caption_text, bbox),
                caption_bbox=None,
                figure_number=number,
                source_kind="manual_split",
                note=f"Logical-figure split from {original_id}; review required.",
            )
            expanded.append(split_candidate)
            split_id = _figure_id(asset_id, split_candidate, len(expanded))
            remaining[split_id] = {
                "page_index": page_index,
                "bbox": bbox,
                "coordinate_system": COORDINATE_SYSTEM,
                "review_notes": split.get("review_notes", correction.get("review_notes", "")),
                "reviewer": split.get("reviewer", correction.get("reviewer")),
                "human_reviewed": split.get(
                    "human_reviewed", correction.get("human_reviewed", False)
                ),
            }
    return expanded, remaining


def _build_correction_map(
    corrections: Mapping[str, Mapping[str, Any]] | None,
    *,
    page_sizes: Mapping[int, tuple[float, float]],
    page_rotations: Mapping[int, int],
) -> dict[str, tuple[BBox, str | None]]:
    normalized: dict[str, tuple[BBox, str | None]] = {}
    for figure_id, correction in (corrections or {}).items():
        if not isinstance(correction, Mapping) or "bbox" not in correction:
            raise ValueError(f"Correction for {figure_id!r} must include bbox.")
        page_index = int(correction.get("page_index", -1))
        if page_index not in page_sizes:
            raise ValueError(f"Correction for {figure_id!r} has an invalid page_index.")
        width, height = page_sizes[page_index]
        bbox = normalize_bbox(
            correction["bbox"],
            page_width=width,
            page_height=height,
            coordinate_system=correction.get("coordinate_system", COORDINATE_SYSTEM),
            page_rotation=int(correction.get("page_rotation", page_rotations[page_index])),
            dpi=float(correction["dpi"]) if correction.get("dpi") is not None else None,
        )
        normalized[figure_id] = (
            _clamp_bbox(bbox, width, height),
            str(correction.get("review_notes", "")).strip() or None,
        )
    return normalized


def _added_candidates(
    additions: Sequence[Mapping[str, Any]],
    *,
    page_sizes: Mapping[int, tuple[float, float]],
    page_rotations: Mapping[int, int],
    existing: Sequence[tuple[int, str | None]],
) -> list[tuple[_Candidate, Mapping[str, Any]]]:
    added: list[tuple[_Candidate, Mapping[str, Any]]] = []
    seen: set[tuple[int, str]] = set()
    for row in additions:
        page_index = int(row["page_index"])
        number = str(row["figure_number"]).strip()
        caption = str(row["caption"]).strip()
        if page_index not in page_sizes:
            raise ValueError("Added figure page_index is outside the PDF.")
        if not number or not caption or len(_caption_labels(caption)) > 1:
            raise ValueError("Added figures require one figure number and an unambiguous caption.")
        key = (page_index, number)
        if key in seen:
            raise ValueError("Duplicate logical figure in add_figures.")
        seen.add(key)
        width, height = page_sizes[page_index]
        bbox = normalize_bbox(
            row["bbox"],
            page_width=width,
            page_height=height,
            coordinate_system=row.get("coordinate_system", COORDINATE_SYSTEM),
            page_rotation=page_rotations[page_index],
            dpi=row.get("dpi"),
        )
        if bbox != _clamp_bbox(bbox, width, height):
            raise ValueError("Added figure bbox must stay within the unrotated CropBox.")
        caption_bbox = None
        if row.get("caption_bbox") is not None:
            caption_bbox = _validate_bbox(row["caption_bbox"])
            if caption_bbox != _clamp_bbox(caption_bbox, width, height):
                raise ValueError("Added caption bbox must stay within the unrotated CropBox.")
        if key in existing:
            continue
        added.append(
            (
                _Candidate(
                    page_index=page_index,
                    bbox=bbox,
                    caption=_Caption(number, caption, caption_bbox or bbox),
                    caption_bbox=caption_bbox,
                    figure_number=number,
                    source_kind="manual_addition",
                    note=str(
                        row.get("review_notes") or "Explicit figure addition; review required."
                    ),
                ),
                row,
            )
        )
    return added


def apply_cached_figure_corrections(
    pdf_path: Path,
    asset_directory: Path,
    *,
    source_hash: str,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
    add_figures: Sequence[Mapping[str, Any]] = (),
    dpi: int = 220,
    preview_dpi: int = 90,
    mark_corrected_reviewed: bool = False,
) -> dict[str, Any]:
    """Apply explicit corrections to cached crops, rendering only changed pages."""
    manifest_path = asset_directory / "figures.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(pdf_path) != source_hash or manifest["source_hash"] != source_hash:
        raise SourceHashMismatch("Cached correction requires the original PDF hash.")
    if dpi < 72 or preview_dpi < 36:
        raise ValueError("dpi must be at least 72 and preview_dpi at least 36.")
    records = manifest["figures"]
    by_id = {record["figure_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Cached figure IDs are not unique.")
    with pymupdf.open(pdf_path) as document:  # type: ignore[no-untyped-call]
        sizes = {i: (p.cropbox.width, p.cropbox.height) for i, p in enumerate(document)}
        rotations = {i: p.rotation for i, p in enumerate(document)}
        changes: list[tuple[dict[str, Any], Mapping[str, Any]]] = []
        for figure_id, correction in (corrections or {}).items():
            if "split_figures" in correction:
                # An already applied split is safe to re-import without duplicating children.
                children = correction["split_figures"]
                if all(
                    any(
                        r["page_index"] == c["page_index"]
                        and r["figure_number"] == c["figure_number"]
                        for r in records
                    )
                    for c in children
                ):
                    continue
                raise ValueError("Unapplied splits require extraction from original candidates.")
            if figure_id not in by_id:
                raise ValueError(f"Unknown cached correction figure: {figure_id}")
            record = by_id[figure_id]
            if int(correction["page_index"]) != record["page_index"]:
                raise ValueError("A bbox correction must use its figure's page_index.")
            cmap = _build_correction_map(
                {figure_id: correction}, page_sizes=sizes, page_rotations=rotations
            )
            record["bbox"] = list(cmap[figure_id][0])
            changes.append((record, correction))
        for candidate, row in _added_candidates(
            add_figures,
            page_sizes=sizes,
            page_rotations=rotations,
            existing=[(r["page_index"], r["figure_number"]) for r in records],
        ):
            figure_id = _figure_id(manifest["asset_id"], candidate, 0)
            if figure_id in by_id:
                raise ValueError("Added figure ID collides with a cached figure.")
            width, height = sizes[candidate.page_index]
            record = dict(
                figure_id=figure_id,
                figure_number=candidate.figure_number,
                caption=candidate.caption.text if candidate.caption else None,
                page=candidate.page_index + 1,
                page_index=candidate.page_index,
                bbox=list(candidate.bbox),
                caption_bbox=candidate.caption_bbox,
                coordinate_system=COORDINATE_SYSTEM,
                page_width=width,
                page_height=height,
                rotation=rotations[candidate.page_index],
                image_path=f"figures/images/{figure_id}.png",
                preview_path=f"figures/previews/{figure_id}.png",
                overlay_path=f"figures/overlays/page-{candidate.page_index + 1:04d}.png",
                detection_source="manual_addition",
            )
            records.append(record)
            by_id[figure_id] = record
            changes.append((record, row))
        changed_pages = set()
        for record, row in changes:
            page = document[record["page_index"]]
            bbox = _validate_bbox(record["bbox"])
            _save_png(_render_crop(page, bbox, dpi), asset_directory / record["image_path"])
            record["preview_bytes"] = _save_png(
                _render_crop(page, bbox, preview_dpi),
                asset_directory / record["preview_path"],
                max_bytes=PREVIEW_MAX_BYTES,
            )
            record["review_notes"] = str(row.get("review_notes") or "Explicit bbox correction.")
            record["correction_provenance"] = {
                "reviewer": row.get("reviewer"),
                "human_reviewed": row.get("human_reviewed", False),
                "confidence": row.get("confidence"),
                "review_id": row.get("review_id"),
            }
            human = bool(row.get("human_reviewed", False))
            record["quality_status"] = (
                "reviewed"
                if human and mark_corrected_reviewed
                else "agent_visual_review"
                if row.get("reviewer") == "agent-visual-review"
                else "candidate"
            )
            record["source_clipped"] = bool(row.get("source_clipped", False))
            if record["source_clipped"]:
                record["quality_status"] = "source_clipped_candidate"
            changed_pages.add(record["page_index"])
        overlays = {r["page_index"]: r for r in manifest.get("review_overlays", [])}
        for pi in changed_pages:
            entries = [
                (str(r["figure_number"]), _validate_bbox(r["bbox"]))
                for r in records
                if r["page_index"] == pi
            ]
            relative = f"figures/overlays/page-{pi + 1:04d}.png"
            size = _save_png(
                _render_overlay(document[pi], entries, preview_dpi),
                asset_directory / relative,
                max_bytes=PREVIEW_MAX_BYTES,
            )
            overlays[pi] = dict(
                page=pi + 1, page_index=pi, path=relative, candidate_count=len(entries), bytes=size
            )
        manifest["review_overlays"] = list(overlays.values())
        manifest["last_correction_run"] = dict(
            changed_figures=len(changes), changed_pages=sorted(changed_pages)
        )
    apply_caption_review_warnings(manifest)
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(manifest_path)
    return cast(dict[str, Any], manifest)


def extract_pdf_figures(
    pdf_path: Path,
    asset_directory: Path,
    *,
    asset_id: str,
    representation_id: str,
    source_hash: str,
    title: str | None = None,
    dpi: int = 220,
    preview_dpi: int = 90,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
    add_figures: Sequence[Mapping[str, Any]] = (),
    mark_corrected_reviewed: bool = False,
    pdffigures2_results: Sequence[Mapping[str, Any]] | None = None,
    run_pymupdf_heuristic: bool = True,
    use_heuristics_for_uncovered_pages: bool = True,
) -> dict[str, Any]:
    """Extract figure crops from PDFFigures2 or a review-only PyMuPDF fallback."""
    pdf_path = Path(pdf_path)
    asset_directory = Path(asset_directory)
    if not pdf_path.is_file():
        raise FigureExtractionError(f"PDF file does not exist: {pdf_path}")
    if dpi < 72 or preview_dpi < 36:
        raise ValueError("dpi must be at least 72 and preview_dpi at least 36.")

    actual_hash = sha256_file(pdf_path)
    if source_hash.casefold() != actual_hash:
        raise SourceHashMismatch(
            "Pilot source_hash does not match the PDF bytes: "
            f"expected {source_hash}, actual {actual_hash}."
        )

    figures_dir = asset_directory / "figures"
    image_dir = figures_dir / "images"
    preview_dir = figures_dir / "previews"
    overlay_dir = figures_dir / "overlays"
    for directory in (image_dir, preview_dir, overlay_dir):
        directory.mkdir(parents=True, exist_ok=True)

    warnings: list[dict[str, Any]] = []
    page_candidates: dict[int, list[_Candidate]] = {}
    observed_captions: dict[int, list[_Caption]] = {}
    page_sizes: dict[int, tuple[float, float]] = {}
    page_rotations: dict[int, int] = {}
    with pymupdf.open(pdf_path) as document:  # type: ignore[no-untyped-call]
        for page_index, page in enumerate(document):
            page_sizes[page_index] = (page.cropbox.width, page.cropbox.height)
            page_rotations[page_index] = page.rotation
            if run_pymupdf_heuristic:
                page_candidates[page_index] = _page_candidates(page, page_index, warnings)
            if run_pymupdf_heuristic or pdffigures2_results is not None:
                try:
                    observed_captions[page_index] = _captions_from_page(page, _text_lines(page))
                    if pdffigures2_results is not None and not page.get_text("text").strip():
                        warnings.append(
                            {
                                "code": "page_text_empty_may_hide_figures",
                                "page_index": page_index,
                                "message": (
                                    "No selectable text was found; caption-based extraction may "
                                    "miss figures on this page."
                                ),
                            }
                        )
                except Exception as error:
                    observed_captions[page_index] = []
                    warnings.append(
                        {
                            "code": "page_text_diagnostic_failed",
                            "page_index": page_index,
                            "message": f"{type(error).__name__}: {error}",
                        }
                    )
            else:
                page_candidates[page_index] = []

        excluded_table_count = 0
        if pdffigures2_results is not None:
            official_candidates, excluded_table_count = _pdffigures2_candidates(
                pdffigures2_results,
                page_sizes=page_sizes,
                warnings=warnings,
            )
            for page_index, official in official_candidates.items():
                if official:
                    page_candidates[page_index] = official
                    official_numbers = {
                        candidate.caption.number
                        for candidate in official
                        if candidate.caption is not None
                    }
                    for caption in observed_captions.get(page_index, []):
                        if caption.number not in official_numbers:
                            warnings.append(
                                {
                                    "code": "caption_not_returned_by_pdffigures2",
                                    "page_index": page_index,
                                    "figure_number": caption.number,
                                    "message": (
                                        "PyMuPDF found a Figure/Fig. caption that PDFFigures2 "
                                        "did not return."
                                    ),
                                }
                            )
                elif not (run_pymupdf_heuristic and use_heuristics_for_uncovered_pages):
                    page_candidates[page_index] = []

            if excluded_table_count:
                warnings.append(
                    {
                        "code": "pdffigures2_tables_excluded",
                        "page_index": None,
                        "count": excluded_table_count,
                        "message": (
                            "PDFFigures2 table items were counted and excluded from figure assets."
                        ),
                    }
                )
            warnings.append(
                {
                    "code": "captioned_figures_only",
                    "page_index": None,
                    "message": (
                        "PDFFigures2 only returns captioned figures; uncaptioned visual "
                        "regions may be missing."
                    ),
                }
            )

        all_candidates = [
            candidate for candidates in page_candidates.values() for candidate in candidates
        ]
        any_caption = any(observed_captions.values()) or any(
            candidate.caption is not None for candidate in all_candidates
        )
        if not any_caption:
            warnings.append(
                {
                    "code": "no_figure_captions_detected",
                    "page_index": None,
                    "message": (
                        "No Figure/Fig. caption was detected; caption-based extraction may "
                        "have missed figures."
                    ),
                }
            )
        if pdffigures2_results is not None:
            official_pages_with_candidates = {
                candidate.page_index
                for candidate in all_candidates
                if candidate.source_kind == "pdffigures2"
            }
            for page_index, captions in observed_captions.items():
                if captions and page_index not in official_pages_with_candidates:
                    if not (run_pymupdf_heuristic and use_heuristics_for_uncovered_pages):
                        warnings.append(
                            {
                                "code": "caption_page_missing_pdffigures2_candidate",
                                "page_index": page_index,
                                "figure_numbers": [caption.number for caption in captions],
                                "message": (
                                    "This page has figure captions but no PDFFigures2 figure "
                                    "candidate."
                                ),
                            }
                        )

        all_candidates, expanded_corrections = _split_corrected_candidates(
            all_candidates,
            corrections,
            asset_id=asset_id,
            page_sizes=page_sizes,
            page_rotations=page_rotations,
        )
        for candidate, row in _added_candidates(
            add_figures,
            page_sizes=page_sizes,
            page_rotations=page_rotations,
            existing=[(c.page_index, c.figure_number) for c in all_candidates],
        ):
            all_candidates.append(candidate)
            expanded_corrections[_figure_id(asset_id, candidate, 0)] = row
        correction_map = _build_correction_map(
            expanded_corrections,
            page_sizes=page_sizes,
            page_rotations=page_rotations,
        )
        figure_ids: list[str] = []
        for ordinal, candidate in enumerate(all_candidates, start=1):
            figure_id = _figure_id(asset_id, candidate, ordinal)
            if figure_id in figure_ids:
                figure_id = f"{figure_id}-{ordinal}"
            figure_ids.append(figure_id)
        for figure_id, candidate in zip(figure_ids, all_candidates, strict=True):
            correction = expanded_corrections.get(figure_id)
            if correction is not None and int(correction["page_index"]) != candidate.page_index:
                raise ValueError("A bbox correction must use its figure's page_index.")
        unknown_corrections = set(correction_map) - set(figure_ids)
        if unknown_corrections:
            raise ValueError(
                "Correction file contains figure IDs that are not present in this extraction: "
                + ", ".join(sorted(unknown_corrections))
            )

        used_corrections: set[str] = set()
        figure_records: list[dict[str, Any]] = []
        per_page_overlays: dict[int, list[tuple[str, BBox]]] = {}
        for ordinal, candidate in enumerate(all_candidates, start=1):
            figure_id = figure_ids[ordinal - 1]
            bbox = candidate.bbox
            review_notes = candidate.note
            if figure_id in correction_map:
                bbox, correction_note = correction_map[figure_id]
                used_corrections.add(figure_id)
                review_notes = "BBox correction imported; " + (
                    correction_note or "bbox updated from the correction file."
                )
                if mark_corrected_reviewed:
                    review_notes += " Explicitly marked reviewed by correction import."
            page = document[candidate.page_index]
            width, height = page_sizes[candidate.page_index]
            # Rendering with a rotation-adjusted clip preserves the unrotated
            # stored bbox while returning pixels in the visible page orientation.
            crop = _render_crop(page, bbox, dpi)
            preview = _render_crop(page, bbox, preview_dpi)
            image_rel = Path("figures") / "images" / f"{figure_id}.png"
            preview_rel = Path("figures") / "previews" / f"{figure_id}.png"
            _save_png(crop, asset_directory / image_rel)
            preview_size = _save_png(
                preview,
                asset_directory / preview_rel,
                max_bytes=PREVIEW_MAX_BYTES,
            )
            page_overlay_rel = (
                Path("figures") / "overlays" / f"page-{candidate.page_index + 1:04d}.png"
            )
            label = candidate.figure_number or f"p{candidate.page_index + 1}-{ordinal}"
            per_page_overlays.setdefault(candidate.page_index, []).append((label, bbox))
            figure_records.append(
                {
                    "figure_id": figure_id,
                    "figure_number": candidate.figure_number,
                    "caption": candidate.caption.text if candidate.caption else None,
                    "page": candidate.page_index + 1,
                    "page_index": candidate.page_index,
                    "bbox": [round(value, 2) for value in bbox],
                    "caption_bbox": (
                        [round(value, 2) for value in candidate.caption_bbox]
                        if candidate.caption_bbox is not None
                        else None
                    ),
                    "coordinate_system": COORDINATE_SYSTEM,
                    "page_width": round(width, 2),
                    "page_height": round(height, 2),
                    "rotation": page_rotations[candidate.page_index],
                    "image_path": image_rel.as_posix(),
                    "preview_path": preview_rel.as_posix(),
                    "overlay_path": page_overlay_rel.as_posix(),
                    "preview_bytes": preview_size,
                    "quality_status": (
                        "source_clipped_candidate"
                        if figure_id in used_corrections
                        and expanded_corrections[figure_id].get("source_clipped", False)
                        else "agent_visual_review"
                        if figure_id in used_corrections
                        and expanded_corrections[figure_id].get("reviewer") == "agent-visual-review"
                        and not expanded_corrections[figure_id].get("human_reviewed", False)
                        else "reviewed"
                        if mark_corrected_reviewed and figure_id in used_corrections
                        else "candidate"
                    ),
                    "review_notes": review_notes,
                    "source_clipped": bool((correction or {}).get("source_clipped", False)),
                    "caption_bbox_invalid": any(
                        warning["code"] == "pdffigures2_invalid_caption_bbox"
                        and warning["page_index"] == candidate.page_index
                        and warning.get("figure_number") == candidate.figure_number
                        for warning in warnings
                    ),
                    "detection_source": candidate.source_kind,
                    "correction_provenance": (
                        {
                            "reviewer": expanded_corrections[figure_id].get("reviewer"),
                            "human_reviewed": expanded_corrections[figure_id].get(
                                "human_reviewed", False
                            ),
                            "confidence": expanded_corrections[figure_id].get("confidence"),
                            "review_id": expanded_corrections[figure_id].get("review_id"),
                        }
                        if figure_id in used_corrections
                        else None
                    ),
                }
            )

        overlay_records: list[dict[str, Any]] = []
        for page_index, entries in per_page_overlays.items():
            page = document[page_index]
            overlay = _render_overlay(page, entries, preview_dpi)
            relative = Path("figures") / "overlays" / f"page-{page_index + 1:04d}.png"
            size = _save_png(overlay, asset_directory / relative, max_bytes=PREVIEW_MAX_BYTES)
            overlay_records.append(
                {
                    "page": page_index + 1,
                    "page_index": page_index,
                    "path": relative.as_posix(),
                    "candidate_count": len(entries),
                    "bytes": size,
                }
            )

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "asset_id": asset_id,
            "representation_id": representation_id,
            "source_hash": actual_hash,
            "pdf_sha256": actual_hash,
            "title": title,
            "extractor": (
                {
                    "name": "pdffigures2",
                    "version": "0.1.0",
                    "fallback": f"{EXTRACTOR_NAME}/{EXTRACTOR_VERSION}",
                }
                if pdffigures2_results is not None
                else {"name": EXTRACTOR_NAME, "version": EXTRACTOR_VERSION}
            ),
            "extractor_version": "0.1.0" if pdffigures2_results is not None else EXTRACTOR_VERSION,
            "coordinate_system": COORDINATE_SYSTEM,
            "page_count": len(document),
            "pdffigures2_tables_excluded": excluded_table_count,
            "figures": figure_records,
            "review_overlays": overlay_records,
            "warnings": warnings,
        }

    apply_caption_review_warnings(manifest)
    manifest_path = asset_directory / "figures.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(manifest_path)
    return manifest
