from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from paper_html import summarize_results, validate_html_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验收生成的论文 HTML：编码、结构、离线性与链接。")
    parser.add_argument("--input", type=Path, required=True, help="单个 .html 文件或递归扫描目录。")
    parser.add_argument("--report", type=Path, help="可选 JSON 报告路径。")
    parser.add_argument(
        "--allowed-root",
        type=Path,
        required=True,
        help="必填：HTML 及全部本地链接解析后都必须位于此永久资料根内。",
    )
    parser.add_argument(
        "--skip-local-link-check",
        action="store_true",
        help="仅模板预览使用；正式交付不得跳过本地链接存在性检查。",
    )
    return parser


def _discover(path: Path) -> list[Path]:
    if path.is_file():
        if path.suffix.lower() != ".html":
            raise ValueError("--input 文件必须是 .html。")
        return [path]
    if not path.is_dir():
        raise ValueError(f"输入不存在：{path}")
    files = sorted(item for item in path.rglob("*.html") if item.is_file())
    if not files:
        raise ValueError(f"目录中没有 HTML：{path}")
    return files


def main() -> int:
    args = _parser().parse_args()
    try:
        allowed_root = args.allowed_root.resolve(strict=True)
        if not allowed_root.is_dir():
            raise ValueError(f"--allowed-root 不是目录：{allowed_root}")
        files = _discover(args.input)
        if args.report:
            if args.report.suffix.lower() == ".html":
                raise ValueError("--report 不得使用 .html 扩展名。")
            if args.report.resolve() in {path.resolve() for path in files}:
                raise ValueError("--report 不得覆盖正在验收的 HTML。")
        results = [
            validate_html_file(
                path,
                check_local_links=not args.skip_local_link_check,
                allowed_root=allowed_root,
            )
            for path in files
        ]
        report = summarize_results(results)
        report["input"] = str(args.input.resolve())
        report["local_link_check_skipped"] = bool(args.skip_local_link_check)
        report["allowed_root"] = str(allowed_root)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    except Exception as exc:
        print(f"错误（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
