from __future__ import annotations

from pathlib import Path

import pytest

from apps.api.code_browser import (
    CodeBrowserPathError,
    list_code_directory,
    read_code_file,
)


def test_directory_listing_is_sorted_and_filters_private_or_generated_entries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / "node_modules").mkdir()
    (root / "token.json").write_text("secret", encoding="utf-8")

    result = list_code_directory(root, representation_id="rep-1")

    assert [item["name"] for item in result["entries"]] == ["src", "README.md"]
    assert result["entries"][0]["kind"] == "directory"
    assert result["entries"][1]["language"] == "markdown"


def test_file_preview_returns_line_numbers_metadata_and_language(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("print('one')\nprint('two')\n", encoding="utf-8")

    result = read_code_file(
        root,
        representation_id="rep-1",
        relative_path="main.py",
    )

    assert result["previewable"] is True
    assert result["language"] == "python"
    assert result["line_count"] == 2
    assert result["content"] == "print('one')\nprint('two')\n"


def test_file_preview_rejects_traversal_and_binary_files(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "weights.bin").write_bytes(b"\x00\x01\x02")

    binary = read_code_file(
        root,
        representation_id="rep-1",
        relative_path="weights.bin",
    )

    assert binary["previewable"] is False
    with pytest.raises(CodeBrowserPathError):
        read_code_file(root, representation_id="rep-1", relative_path="../outside.py")
