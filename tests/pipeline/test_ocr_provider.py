from __future__ import annotations

from pathlib import Path

from packages.extraction import TesseractOCRProvider


def test_tesseract_provider_reports_missing_languages(tmp_path: Path) -> None:
    executable = tmp_path / "tesseract.exe"
    executable.write_bytes(b"placeholder")
    provider = TesseractOCRProvider(
        executable=executable,
        tessdata=tmp_path / "tessdata",
    )
    assert provider.status.available
    assert not provider.status.configured
    assert "chi_sim" in (provider.status.detail or "")
