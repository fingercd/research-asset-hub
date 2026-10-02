from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from paper_html import (
    RecordError,
    load_records,
    render_record,
    summarize_results,
    validate_html_file,
)

TOOL_DIR = Path(__file__).resolve().parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 paper-record JSON 生成自包含、UTF-8、响应式中文论文 HTML。"
    )
    parser.add_argument(
        "--input", type=Path, required=True, help="单记录、数组或 papers/items/records 包装 JSON。"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="单记录可写 .html；批量必须写目录。"
    )
    parser.add_argument("--template", type=Path, default=TOOL_DIR / "template.html")
    parser.add_argument("--styles", type=Path, default=TOOL_DIR / "styles.css")
    parser.add_argument("--report", type=Path, help="可选 JSON 生成/静态验收报告。")
    parser.add_argument(
        "--allowed-root",
        type=Path,
        required=True,
        help="必填：生成页及所有本地链接解析后都必须位于此永久资料根内。",
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="覆盖尚未冻结、且已人工确认可重建的目标。"
    )
    parser.add_argument(
        "--skip-local-link-check",
        action="store_true",
        help="仅模板预览使用；正式交付不得跳过本地链接存在性检查。",
    )
    return parser


def _inside_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _stage_write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".staged.html", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return Path(temp_name)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def _cleanup(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _commit_staged(pairs: list[tuple[Path, Path]]) -> None:
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for target, staged in pairs:
            backup: Path | None = None
            if target.exists():
                handle, backup_name = tempfile.mkstemp(
                    prefix=f".{target.name}.", suffix=".rollback", dir=target.parent
                )
                os.close(handle)
                backup = Path(backup_name)
                backup.unlink()
                os.replace(target, backup)
            backups[target] = backup
            try:
                os.replace(staged, target)
            except Exception:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                raise
            committed.append(target)
    except Exception:
        for target in reversed(committed):
            backup = backups.get(target)
            try:
                if backup is not None and backup.exists():
                    os.replace(backup, target)
                else:
                    target.unlink(missing_ok=True)
            except OSError:
                # Preserve any rollback file for manual recovery if the OS
                # refuses restoration; do not silently delete it below.
                backups[target] = backup
        raise
    else:
        _cleanup([backup for backup in backups.values() if backup is not None])


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    staged = _stage_write(path, content)
    os.replace(staged, path)


def _target_paths(records: list[dict[str, Any]], output: Path) -> list[Path]:
    if output.suffix.lower() == ".html":
        if len(records) != 1:
            raise RecordError("批量输入的 --output 必须是目录，不能是单个 .html 文件。")
        return [output]
    targets: list[Path] = []
    for record in records:
        paper_id = record.get("paper_id")
        targets.append(output / f"{paper_id}.html")
    if len({target.name for target in targets}) != len(targets):
        raise RecordError("批量输入包含重复 paper_id，会写入同一 HTML。")
    return targets


def main() -> int:
    args = _parser().parse_args()
    try:
        allowed_root = args.allowed_root.resolve(strict=True)
        if not allowed_root.is_dir():
            raise RecordError(f"--allowed-root 不是目录：{allowed_root}")
        records = load_records(args.input)
        targets = _target_paths(records, args.output)
        outside_targets = [target for target in targets if not _inside_root(target, allowed_root)]
        if outside_targets:
            raise RecordError(
                "HTML 目标越出 --allowed-root：" + "，".join(str(path) for path in outside_targets)
            )
        if args.report:
            if args.report.suffix.lower() == ".html":
                raise RecordError("--report 不得使用 .html 扩展名。")
            if args.report.resolve() in {target.resolve() for target in targets}:
                raise RecordError("--report 不得与任一输出 HTML 指向同一文件。")
        metadata_warnings: list[dict[str, Any]] = []
        rendered_batch: list[str] = []
        # Validate and render the whole batch before the first write. A bad later
        # record must not leave a misleading partially generated intake behind.
        for record in records:
            rendered, record_issues = render_record(
                record, template_path=args.template, styles_path=args.styles
            )
            rendered_batch.append(rendered)
            metadata_warnings.extend(
                {"paper_id": record.get("paper_id"), **issue.__dict__}
                for issue in record_issues
                if issue.level == "warning"
            )
        if not args.overwrite:
            existing = [target for target in targets if target.exists()]
            if existing:
                raise RecordError(
                    "目标已存在；默认拒绝覆盖：" + "，".join(str(path) for path in existing)
                )
        invalid_existing = [
            target for target in targets if target.exists() and not target.is_file()
        ]
        if invalid_existing:
            raise RecordError(
                "HTML 目标已存在但不是普通文件："
                + "，".join(str(path) for path in invalid_existing)
            )
        staged_paths: list[Path] = []
        try:
            for target, rendered in zip(targets, rendered_batch, strict=True):
                staged_paths.append(_stage_write(target, rendered))
            validation_results = [
                validate_html_file(
                    staged,
                    check_local_links=not args.skip_local_link_check,
                    allowed_root=allowed_root,
                )
                for staged in staged_paths
            ]
            for validation, target, staged in zip(
                validation_results, targets, staged_paths, strict=True
            ):
                validation.path = str(target.resolve())
                for issue in [*validation.errors, *validation.warnings]:
                    if issue.path == str(staged):
                        issue.path = str(target.resolve())
            report = summarize_results(validation_results)
            report.update(
                {
                    "schema_version": "paper-intake-html.generate.v1",
                    "input": str(args.input.resolve()),
                    "output": str(args.output.resolve()),
                    "generated": len(targets) if report["status"] == "passed" else 0,
                    "generated_paths": (
                        [str(path.resolve()) for path in targets]
                        if report["status"] == "passed"
                        else []
                    ),
                    "record_warnings": metadata_warnings,
                    "local_link_check_skipped": bool(args.skip_local_link_check),
                    "allowed_root": str(allowed_root),
                    "transaction": "staged-validated-atomic-commit",
                }
            )
            if report["status"] != "passed":
                if args.report:
                    _write_report(args.report, report)
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
            _commit_staged(list(zip(targets, staged_paths, strict=True)))
        finally:
            _cleanup(staged_paths)
        if args.report:
            _write_report(args.report, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(f"错误（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
