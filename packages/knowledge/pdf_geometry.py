"""PDF page geometry shared by figure detection and PDFium rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pdfplumber
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image

if TYPE_CHECKING:
    from pdfplumber.page import Page

BBox = tuple[float, float, float, float]


def unrotate_visible_bbox(bbox: BBox, width: float, height: float, rotation: int) -> BBox:
    """Convert a displayed, top-left bbox to unrotated CropBox-local points."""
    x0, y0, x1, y1 = bbox
    if rotation == 0:
        return bbox
    if rotation == 90:
        return y0, height - x1, y1, height - x0
    if rotation == 180:
        return width - x1, height - y1, width - x0, height - y0
    if rotation == 270:
        return width - y1, x0, width - y0, x1
    raise ValueError("page rotation must be one of 0, 90, 180, or 270 degrees.")


@dataclass(frozen=True, slots=True)
class PdfPageGeometry:
    """CropBox dimensions and transforms for one pdfplumber page."""

    page_index: int
    width: float
    height: float
    rotation: int
    crop_left: float
    crop_top: float

    @classmethod
    def from_page(cls, page: Page, page_index: int) -> PdfPageGeometry:
        rotation = int(page.rotation) % 360
        if rotation not in {0, 90, 180, 270}:
            raise ValueError("page rotation must be one of 0, 90, 180, or 270 degrees.")
        x0, top, x1, bottom = page.cropbox
        visible_width = float(x1 - x0)
        visible_height = float(bottom - top)
        if visible_width <= 0 or visible_height <= 0:
            raise ValueError("PDF CropBox must have positive width and height.")
        width, height = (
            (visible_height, visible_width)
            if rotation in {90, 270}
            else (visible_width, visible_height)
        )
        return cls(
            page_index=page_index,
            width=width,
            height=height,
            rotation=rotation,
            crop_left=float(x0),
            crop_top=float(top),
        )

    @property
    def visible_width(self) -> float:
        return self.height if self.rotation in {90, 270} else self.width

    @property
    def visible_height(self) -> float:
        return self.width if self.rotation in {90, 270} else self.height

    def page_bbox_to_pdf_points(self, bbox: BBox) -> BBox:
        """Convert pdfplumber page coordinates to CropBox-local PDF points."""
        x0, top, x1, bottom = bbox
        local_visible_bbox = (
            x0 - self.crop_left,
            top - self.crop_top,
            x1 - self.crop_left,
            bottom - self.crop_top,
        )
        return unrotate_visible_bbox(local_visible_bbox, self.width, self.height, self.rotation)

    def pdf_points_to_visible_bbox(self, bbox: BBox) -> BBox:
        """Convert CropBox-local PDF points to displayed, top-left points."""
        x0, y0, x1, y1 = bbox
        if self.rotation == 0:
            return bbox
        if self.rotation == 90:
            return self.height - y1, x0, self.height - y0, x1
        if self.rotation == 180:
            return self.width - x1, self.height - y1, self.width - x0, self.height - y0
        return y0, self.width - x1, y1, self.width - x0

    def bbox_to_pixels(self, bbox: BBox, image_size: tuple[int, int]) -> tuple[int, int, int, int]:
        visible = self.pdf_points_to_visible_bbox(bbox)
        scale_x = image_size[0] / self.visible_width
        scale_y = image_size[1] / self.visible_height
        x0, y0, x1, y1 = visible
        left = max(0, min(image_size[0], round(x0 * scale_x)))
        top = max(0, min(image_size[1], round(y0 * scale_y)))
        right = max(0, min(image_size[0], round(x1 * scale_x)))
        bottom = max(0, min(image_size[1], round(y1 * scale_y)))
        if right <= left and left < image_size[0]:
            right = left + 1
        if bottom <= top and top < image_size[1]:
            bottom = top + 1
        return left, top, right, bottom


def read_pdf_page_geometries(pdf_path: Path) -> list[PdfPageGeometry]:
    """Read each page's CropBox and rotation with pdfplumber."""
    with pdfplumber.open(pdf_path) as document:
        return [PdfPageGeometry.from_page(page, index) for index, page in enumerate(document.pages)]


class PdfiumRenderer:
    """Render visible PDF pages with PDFium and crop them with Pillow."""

    def __init__(self, pdf_path: Path, page_geometries: list[PdfPageGeometry]) -> None:
        self._document = pdfium.PdfDocument(str(pdf_path))
        self._page_geometries = page_geometries
        self._rendered: dict[tuple[int, int], Image.Image] = {}
        if len(self._document) != len(page_geometries):
            self._document.close()
            raise ValueError("pdfplumber and PDFium reported different PDF page counts.")

    def __enter__(self) -> PdfiumRenderer:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for image in self._rendered.values():
            image.close()
        self._rendered.clear()
        self._document.close()

    def render_page(self, page_index: int, dpi: int) -> Image.Image:
        if dpi <= 0:
            raise ValueError("dpi must be positive.")
        key = page_index, dpi
        if key not in self._rendered:
            page = self._document[page_index]
            try:
                bitmap = page.render(scale=dpi / 72)
                try:
                    image = bitmap.to_pil().convert("RGB")
                finally:
                    bitmap.close()
            finally:
                page.close()
            self._rendered[key] = image
        return self._rendered[key]

    def render_crop(self, page_index: int, bbox: BBox, dpi: int) -> Image.Image:
        page = self.render_page(page_index, dpi)
        pixels = self._page_geometries[page_index].bbox_to_pixels(bbox, page.size)
        if pixels[2] <= pixels[0] or pixels[3] <= pixels[1]:
            raise ValueError("The bbox does not intersect the visible page.")
        return page.crop(pixels)

    def release_page(self, page_index: int) -> None:
        for key in [key for key in self._rendered if key[0] == page_index]:
            self._rendered.pop(key).close()
