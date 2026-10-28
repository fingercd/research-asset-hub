from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from scripts.check_repository import check_repository, main, markdown_links


def repository(tmp_path: Path, files: dict[str, str | bytes]) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True)
    return root


def test_local_links_support_unicode_spaces_directories_queries_and_fragments(
    tmp_path: Path,
) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "[中文](docs/%E4%B8%AD%E6%96%87%20file.md?raw=1#section)\n"
            "[目录](docs/)\n[空格](<docs/中文 file.md>)\n"
            '[括号](docs/a(b).md "title")\n<a href="docs/page.html#anchor">HTML</a>\n'
            "![图](docs/image.png)\n[参考][ref]\n[ref]: docs/page.html#anchor\n",
            "docs/中文 file.md": "# 中文\n",
            "docs/a(b).md": "# Parentheses\n",
            "docs/page.html": '<a id="anchor"></a>',
            "docs/image.png": b"fixture-image",
        },
    )
    report = check_repository(root)
    assert report.errors == 0
    assert report.local_links == 7


def test_code_templates_external_links_and_anchor_only_are_not_checked(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "```md\n[x](missing.md)\n```\n~~~\n[x](missing.md)\n~~~\n"
            "    [indented](missing.md)\n`[inline](missing.md)`\n"
            "``[inline ` nested](missing.md)``\n<!-- [comment](missing.md) -->\n"
            "[web](https://example.com/)\n[email](mailto:example@example.com)\n"
            "[anchor](#missing-anchor)\n[template](docs/<source-id>/README.md)\n"
            "[variable](docs/${NAME}/README.md)\n",
        },
    )
    report = check_repository(root)
    assert report.errors == 0
    assert report.local_links == 0
    assert report.skipped_links == 5


def test_active_errors_history_warnings_and_strict_history(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "[history](docs/reports/old.md)\n[missing](docs/current.md)\n",
            "docs/reports/old.md": "[old log](../../work/deleted-log.json)\n",
            "docs/plans/old.md": "[missing](gone.md)\n",
            "docs/superpowers/specs/old.md": "[missing](gone.md)\n",
        },
    )
    report = check_repository(root)
    assert report.errors == 1
    assert sum(item.code == "missing_link" for item in report.findings) == 4
    assert not any(item.code == "historical_reference" for item in report.findings)
    assert check_repository(root, strict_history=True).errors == 4


def test_default_ignores_untracked_but_explicit_files_check_drafts(tmp_path: Path) -> None:
    root = repository(tmp_path, {"README.md": "# OK\n"})
    (root / "draft.md").write_text("[bad](missing.md)", encoding="utf-8")
    assert check_repository(root).errors == 0
    report = check_repository(root, ["draft.md"])
    assert report.markdown_files == 1
    assert report.errors == 1


def test_external_tree_is_not_enumerated_or_statted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path, {"README.md": "[outside](../private-secret.md)\n"})
    original = Path.exists

    def guarded_exists(path: Path) -> bool:
        assert path.is_relative_to(root), "External link target must not be inspected"
        return original(path)

    monkeypatch.setattr(Path, "exists", guarded_exists)
    report = check_repository(root)
    assert report.errors == 0
    assert report.findings[0].code == "external_path_skipped"
    assert check_repository(root, ["../private-secret.md"]).errors == 1


def test_hygiene_allows_small_real_fixtures_and_reports_but_not_secrets(tmp_path: Path) -> None:
    with sqlite3.connect(":memory:") as database:
        database.execute("CREATE TABLE fixture (id INTEGER PRIMARY KEY)")
        sqlite_fixture = database.serialize()
    root = repository(
        tmp_path,
        {
            "README.md": "# OK",
            ".env.example": "TOKEN=example",
            "tests/fixtures/minimal.pdf": b"%PDF-1.4\n%%EOF\n",
            "tests/fixtures/small.sqlite3": sqlite_fixture,
            "outputs/release/report.json": '{"status":"passed"}',
            "docs/reports/report.md": "# Report",
            ".env.production": "PRIVATE_CONTENT_NOT_TO_PRINT",
            "data/runtime.sqlite3": b"PRIVATE_CONTENT_NOT_TO_PRINT",
            "model.pth": b"PRIVATE_CONTENT_NOT_TO_PRINT",
        },
    )
    report = check_repository(root)
    assert report.errors == 3
    assert "PRIVATE_CONTENT_NOT_TO_PRINT" not in json.dumps(report.to_dict())


def test_large_runtime_file_fails_but_frozen_manifest_only_warns(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "# OK",
            "huge.log": b"x" * (10 * 1024 * 1024 + 1),
            "config/intake/frozen/manifest.json": b" " * (10 * 1024 * 1024 + 1),
        },
    )
    report = check_repository(root)
    assert report.errors == 1
    assert [(item.path, item.severity) for item in report.findings] == [
        ("config/intake/frozen/manifest.json", "warning"),
        ("huge.log", "error"),
    ]


def test_cli_json_exit_codes_and_invalid_utf8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repository(tmp_path, {"README.md": b"\xff"})
    assert main(["--root", str(root), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert payload["counts"]["errors"] == 1
    assert payload["findings"][0]["code"] == "document_unreadable"
    (root / "README.md").write_text("# OK", encoding="utf-8")
    assert main(["--root", str(root)]) == 0
    assert "markdown_files=1" in capsys.readouterr().out


def test_markdown_line_numbers_and_html_entities() -> None:
    assert list(markdown_links("\n`[not](missing)`\n[a](file.md?x=1&amp;y=2)\n")) == [
        (3, "file.md?x=1&y=2"),
    ]


def test_encoded_placeholder_and_nul_targets(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "[template](docs/%3Csource-id%3E/README.md)\n[nul](bad%00.md)\n",
        },
    )
    report = check_repository(root)
    assert report.skipped_links == 1
    assert report.errors == 1
    assert report.findings[0].code == "invalid_link"


def test_fixture_exemption_does_not_allow_large_database_or_secret(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "tests/fixtures/large.sqlite": b"x" * (1024 * 1024 + 1),
            "tests/fixtures/.env": "example-but-secret-filename-is-forbidden",
        },
    )
    assert check_repository(root).errors == 2


def test_missing_explicit_document_and_non_repository_fail(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = repository(tmp_path, {"README.md": "# OK"})
    assert main(["--root", str(root), "--files", "absent.md", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "document_missing"
    assert main(["--root", str(tmp_path), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["findings"][0]["code"] == "repository_unreadable"


def test_existing_untracked_ignored_and_empty_directory_targets_fail(tmp_path: Path) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "[draft](draft.md)\n[ignored](work/report.json)\n[empty](empty/)\n",
            ".gitignore": "work/\n",
        },
    )
    (root / "draft.md").write_text("# Draft", encoding="utf-8")
    (root / "work").mkdir()
    (root / "work/report.json").write_text("{}", encoding="utf-8")
    (root / "empty").mkdir()
    report = check_repository(root)
    assert report.errors == 3
    assert all(item.code == "untracked_link" for item in report.findings)
    assert all("fresh clone does not include" in item.message for item in report.findings)


def test_explicit_drafts_allow_cross_links_but_not_other_untracked_files(tmp_path: Path) -> None:
    root = repository(tmp_path, {"README.md": "[draft](docs/draft.md)\n"})
    (root / "docs").mkdir()
    (root / "docs/draft.md").write_text("[root](../README.md)\n", encoding="utf-8")
    assert check_repository(root).errors == 1
    assert check_repository(root, ["README.md", "docs/draft.md"]).errors == 0
    (root / "docs/draft.md").write_text("[other](other.md)\n", encoding="utf-8")
    (root / "docs/other.md").write_text("# Unlisted", encoding="utf-8")
    report = check_repository(root, ["README.md", "docs/draft.md"])
    assert report.errors == 1
    assert report.findings[0].code == "untracked_link"


def test_historical_untracked_links_warn_and_valid_history_references_are_clean(
    tmp_path: Path,
) -> None:
    root = repository(
        tmp_path,
        {
            "README.md": "[evidence](docs/reports/report.md)\n",
            "docs/reports/report.md": "[untracked](../../draft.md)\n",
        },
    )
    (root / "draft.md").write_text("# Draft", encoding="utf-8")
    report = check_repository(root)
    assert report.errors == 0
    assert len(report.findings) == 1
    assert report.findings[0].code == "untracked_link"
    assert report.findings[0].severity == "warning"
    assert check_repository(root, strict_history=True).errors == 1
