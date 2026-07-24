"""Windows Tesseract OCR provider that never modifies the source PDF."""

from __future__ import annotations

import importlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .models import OCRProviderStatus


class TesseractOCRProvider:
    def __init__(
        self,
        *,
        executable: Path | None = None,
        tessdata: Path | None = None,
        languages: tuple[str, ...] = ("chi_sim", "chi_tra", "eng"),
        timeout_seconds: float = 90,
        render_scale: float = 2.5,
    ) -> None:
        discovered = shutil.which("tesseract")
        default_executable = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        self.executable = executable or (
            Path(discovered) if discovered else default_executable
        )
        default_tessdata = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "ResearchAssetHub"
            / "tessdata"
        )
        self.tessdata = tessdata or Path(
            os.environ.get("TESSDATA_PREFIX", str(default_tessdata))
        )
        self.languages = languages
        self.timeout_seconds = timeout_seconds
        self.render_scale = render_scale

    @property
    def status(self) -> OCRProviderStatus:
        missing_languages = [
            language
            for language in self.languages
            if not (self.tessdata / f"{language}.traineddata").is_file()
        ]
        available = self.executable.is_file()
        configured = available and not missing_languages
        detail = None
        if not available:
            detail = f"Tesseract executable not found: {self.executable}"
        elif missing_languages:
            detail = "Missing OCR languages: " + ", ".join(missing_languages)
        return OCRProviderStatus(
            provider="tesseract-windows",
            available=available,
            configured=configured,
            detail=detail,
        )

    def recognize_page(self, path: Path, page_number: int) -> str:
        if page_number < 1:
            raise ValueError("page_number is one-based")
        status = self.status
        if not status.available or not status.configured:
            raise RuntimeError(status.detail or "Tesseract OCR is unavailable")
        pdfium: Any = importlib.import_module("pypdfium2")
        document = pdfium.PdfDocument(str(path))
        if page_number > len(document):
            raise IndexError(f"PDF has only {len(document)} pages")
        page = document[page_number - 1]
        bitmap = page.render(scale=self.render_scale)
        image = bitmap.to_pil()
        with tempfile.TemporaryDirectory(prefix="rah-ocr-") as temporary_directory:
            image_path = Path(temporary_directory) / f"page-{page_number}.png"
            image.save(image_path, format="PNG")
            environment = os.environ.copy()
            environment["TESSDATA_PREFIX"] = str(self.tessdata)
            completed = subprocess.run(
                [
                    str(self.executable),
                    str(image_path),
                    "stdout",
                    "-l",
                    "+".join(self.languages),
                    "--psm",
                    "6",
                ],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                env=environment,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        page.close()
        document.close()
        if completed.returncode != 0:
            detail = completed.stderr.strip()[-500:]
            raise RuntimeError(f"Tesseract returned {completed.returncode}: {detail}")
        return completed.stdout
