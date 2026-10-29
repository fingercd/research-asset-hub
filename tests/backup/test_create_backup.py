from __future__ import annotations

import json
from pathlib import Path

from scripts import create_backup


def test_main_writes_utf8_privacy_safe_report_without_running_real_backup(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    payload = {
        "status": "verified",
        "label": "备份完成",
        "credential_created": True,
        "password": "do-not-write",
        "repository": "D:/backups",
    }
    called: list[Path] = []

    def fake_backup(project_root: Path) -> dict[str, object]:
        called.append(project_root)
        return payload

    monkeypatch.setattr(create_backup, "create_platform_backup", fake_backup)
    report = tmp_path / "work" / "validation" / "backup.json"
    assert create_backup.main(["--project-root", str(tmp_path), "--report", str(report)]) == 0

    assert called == [tmp_path.resolve()]
    document = json.loads(report.read_text(encoding="utf-8"))
    assert document["label"] == "备份完成"
    assert document["password"] == "<redacted>"
    assert document["credential_created"] is True
    assert "do-not-write" not in report.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out)["status"] == "verified"


def test_main_returns_nonzero_and_does_not_leak_error(tmp_path: Path, monkeypatch, capsys) -> None:
    def failing_backup(_: Path) -> dict[str, object]:
        raise RuntimeError("RESTIC_PASSWORD=top-secret")

    monkeypatch.setattr(create_backup, "create_platform_backup", failing_backup)
    report = tmp_path / "report.json"
    assert create_backup.main(["--project-root", str(tmp_path), "--report", str(report)]) == 1
    captured = capsys.readouterr()
    assert "top-secret" not in captured.err
    assert not report.exists()
