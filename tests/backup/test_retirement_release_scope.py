from __future__ import annotations

from packages.backup.release_report import ReleaseReportInputs, build_release_report


def _retirement_gate(database: dict[str, object]) -> dict[str, object]:
    report = build_release_report(
        ReleaseReportInputs(database=database, reports={})
    )
    return next(
        gate
        for gate in report["gates"]
        if gate["id"] == "source-retirement-ledger"
    )


def test_release_retirement_gate_is_noop_without_retirement() -> None:
    gate = _retirement_gate(
        {
            "schema_revision": "0004_source_retirement",
            "source_retirement": {
                "status": "passed",
                "total": 0,
                "active": 0,
                "rolled_back": 0,
                "invalid": 0,
                "items": [],
            },
        }
    )
    assert gate["status"] == "pass"


def test_release_retirement_gate_fails_closed_on_invalid_ledger() -> None:
    gate = _retirement_gate(
        {
            "schema_revision": "0004_source_retirement",
            "source_retirement": {
                "status": "failed",
                "total": 1,
                "active": 1,
                "rolled_back": 0,
                "invalid": 1,
                "items": [{"retirement_id": "stable-id", "valid": False}],
            },
        }
    )
    assert gate["status"] == "fail"
