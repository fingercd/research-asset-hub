# -*- coding: utf-8 -*-
"""四按钮验收脚本：断言每个论文研究单元的 PDF/HTML/代码 按钮状态与库内资料一致。

用法:
  ./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing

口径:
  - 每个 unit_kind=="paper" 单元必须满足:
      pdf  status == "available"
      html status == "available"
      code status == "available"（或 == "not_provided" 且论文标题在白名单内）
  - guide 动作不得再出现（2026-08-11 已废除导读按钮）。
退出码 0 = 全部符合;1 = 存在不匹配。可重复运行，是未来入库的硬门禁。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

DEFAULT_ALLOWLIST = Path(__file__).resolve().parents[1] / "config" / "check-unit-actions-allowlist.yaml"


def fetch_units(api_base: str, domain: str) -> list[dict]:
    units: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"q": "", "mode": "keyword", "group_by": "paper"}
        if domain:
            params["domain"] = domain
        if cursor:
            params["cursor"] = cursor
        url = f"{api_base}/api/v1/search?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = json.load(response)
        units.extend(data.get("items", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    parser.add_argument("--domain", default="image-editing")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    args = parser.parse_args()

    allowlist = yaml.safe_load(Path(args.allowlist).read_text(encoding="utf-8")) or {}
    no_code_titles = set((allowlist.get(args.domain) or {}).get("no_code_titles", []))

    units = fetch_units(args.api, args.domain)
    papers = [unit for unit in units if unit.get("unit_kind") == "paper"]
    failures: list[str] = []
    guide_found = False
    print(f"论文单元 {len(papers)} 个（domain={args.domain}）")

    for unit in sorted(papers, key=lambda item: (item.get("primary_asset") or {}).get("title", "")):
        primary = unit.get("primary_asset") or {}
        title = primary.get("title") or "?"
        actions = {action["kind"]: action for action in unit.get("quick_actions", [])}
        row = [f"[{primary.get('id', '?')[:8]}]"]
        for kind in ("pdf", "html"):
            status = (actions.get(kind) or {}).get("status", "missing")
            ok = status == "available"
            row.append(f"{kind}={status}{'✓' if ok else '✗'}")
            if not ok:
                failures.append(f"{title}: {kind} 期望 available，实际 {status}")
        code_status = (actions.get("code") or {}).get("status", "not_provided")
        code_ok = code_status == "available" or (
            code_status == "not_provided" and title in no_code_titles
        )
        row.append(f"code={code_status}{'✓' if code_ok else '✗'}")
        if not code_ok:
            failures.append(f"{title}: code 期望 available（或属于无代码白名单），实际 {code_status}")
        if "guide" in actions:
            guide_found = True
            failures.append(f"{title}: 仍存在 guide 动作（已废除，应删除）")
        print("  " + " | ".join(row) + " | " + title[:60])

    if failures:
        print(f"\n❌ {len(failures)} 处不匹配：")
        for failure in failures:
            print("  - " + failure)
        if guide_found:
            print("  - （guide 动作残留提示：后端 kinds 元组未移除 guide）")
        return 1
    print("\n✅ 全部论文单元按钮状态符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
