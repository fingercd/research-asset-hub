from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from uuid import UUID

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "enrich_citations.py"
_SPEC = importlib.util.spec_from_file_location("enrich_citations_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_SCRIPT = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_SCRIPT)
_retry_targets = _SCRIPT._retry_targets


def test_retry_targets_select_only_recoverable_http_429s(tmp_path: Path) -> None:
    selected = UUID("019f93e4-aaf2-7c7c-988d-c983abb10b5e")
    report_path = tmp_path / "citation-enrichment.json"
    report_path.write_text(
        json.dumps(
            {
                "failures": [
                    {
                        "asset_id": str(selected),
                        "provider": "crossref",
                        "detail": "HTTP 429",
                        "recoverable": True,
                    },
                    {
                        "asset_id": "019f93e4-ae9c-7b9a-ac4c-125546a3e37e",
                        "provider": "crossref",
                        "detail": "HTTP 500",
                        "recoverable": True,
                    },
                    {
                        "asset_id": "019f93e4-b586-73c4-a395-1b5506749453",
                        "provider": "crossref",
                        "detail": "HTTP 429",
                        "recoverable": False,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    asset_ids, providers = _retry_targets(report_path, "all")

    assert asset_ids == {selected}
    assert providers == {"crossref"}
