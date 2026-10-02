from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

REQUIRED_SECTIONS = {
    "overview",
    "problem",
    "method",
    "results",
    "limitations",
    "relationships",
    "code",
    "sources",
    "verification",
}


class _PaperPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.section_ids: set[str] = set()
        self.paper_id: str | None = None
        self.lang: str | None = None
        self.charset: str | None = None
        self.viewport = False
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "html":
            self.lang = values.get("lang")
        elif tag == "meta":
            if values.get("charset"):
                self.charset = values["charset"]
            if values.get("name") == "viewport":
                self.viewport = True
            if values.get("name") == "paper-id":
                self.paper_id = values.get("content")
            if str(values.get("http-equiv") or "").lower() == "refresh":
                content = str(values.get("content") or "")
                if "url=" in content.lower():
                    self.links.append(content.split("=", 1)[1].strip())
        elif tag == "section" and values.get("id"):
            self.section_ids.add(str(values["id"]))
        if tag in {"a", "link", "script"}:
            link = values.get("href") or values.get("src")
            if link:
                self.links.append(link)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=True))
        return True
    except (OSError, ValueError):
        return False


def _repository_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.startswith("git@") and ":" in text:
        host, path = text[4:].split(":", 1)
    else:
        parsed = urlparse(text)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        path = parsed.path
    normalized = re.sub(r"/+", "/", path).strip("/")
    if normalized.lower().endswith(".git"):
        normalized = normalized[:-4]
    return f"{host.lower()}/{normalized.lower()}" if normalized else None


def _git(path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 顶层必须是对象：{path}")
    return value


def _has_guide(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() == "guide":
                return True
            if str(key).lower() == "role" and str(item).lower() == "guide":
                return True
            if _has_guide(item):
                return True
    elif isinstance(value, list):
        return any(_has_guide(item) for item in value)
    return False


def validate(unit_root: Path, manifest_path: Path) -> dict[str, Any]:
    unit_root = unit_root.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    errors: list[str] = []
    warnings: list[str] = []
    manifest = _load(manifest_path)
    if manifest.get("schema_version") != "compression-unit.outputs-manifest.v2":
        errors.append("manifest_schema_invalid")
    stored_manifest_hash = manifest.get("manifest_payload_sha256")
    manifest_payload = dict(manifest)
    manifest_payload.pop("manifest_payload_sha256", None)
    calculated_manifest_hash = hashlib.sha256(
        (json.dumps(manifest_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
            "utf-8"
        )
    ).hexdigest()
    if stored_manifest_hash != calculated_manifest_hash:
        errors.append("manifest_payload_sha256_mismatch")
    interfaces = manifest.get("interfaces") or {}
    catalog_rel = interfaces.get("papers")
    if not isinstance(catalog_rel, str):
        errors.append("papers_catalog_interface_missing")
        catalog: dict[str, Any] = {}
    else:
        catalog_path = unit_root / Path(catalog_rel)
        if not _inside(catalog_path, unit_root) or not catalog_path.is_file():
            errors.append(f"papers_catalog_missing:{catalog_rel}")
            catalog = {}
        else:
            catalog = _load(catalog_path)

    source_records = catalog.get("source_records") or []
    recovered_versions = catalog.get("recovered_versions") or []
    canonical = catalog.get("canonical_papers") or []
    if (
        not isinstance(source_records, list)
        or not isinstance(recovered_versions, list)
        or not isinstance(canonical, list)
    ):
        errors.append("catalog_arrays_invalid")
        source_records = []
        recovered_versions = []
        canonical = []
    counts = manifest.get("counts") or {}
    expected_source = counts.get("source_records")
    expected_canonical = counts.get("canonical_papers")
    if expected_source != len(source_records):
        errors.append(f"source_record_count:{len(source_records)}!={expected_source}")
    if expected_canonical != len(canonical):
        errors.append(f"canonical_count:{len(canonical)}!={expected_canonical}")
    if counts.get("recovered_versions") != len(recovered_versions):
        errors.append(
            f"recovered_version_count:{len(recovered_versions)}!={counts.get('recovered_versions')}"
        )
    alternate_versions = sum(
        1
        for item in source_records
        if isinstance(item, dict)
        and isinstance(item.get("version_relation"), dict)
        and item["version_relation"].get("relation_type") == "alternate_version_of"
    )
    if counts.get("version_pages") != alternate_versions + len(recovered_versions):
        errors.append("version_page_count_mismatch")
    if counts.get("generated_html") != len(source_records) + len(recovered_versions):
        errors.append("generated_html_count_mismatch")

    source_ids = [item.get("source_record_id") for item in source_records if isinstance(item, dict)]
    if len(source_ids) != len(set(source_ids)):
        errors.append("duplicate_source_record_id")
    canonical_ids = [item.get("canonical_paper_id") for item in canonical if isinstance(item, dict)]
    if len(canonical_ids) != len(set(canonical_ids)):
        errors.append("duplicate_canonical_paper_id")
    version_ids = [item.get("version_id") for item in recovered_versions if isinstance(item, dict)]
    if len(version_ids) != len(set(version_ids)):
        errors.append("duplicate_recovered_version_id")

    if manifest.get("no_guide") is not True or _has_guide(catalog):
        errors.append("guide_representation_forbidden")

    repository_rel = interfaces.get("repositories")
    repository_catalog: dict[str, Any] = {}
    if not isinstance(repository_rel, str):
        errors.append("repositories_catalog_interface_missing")
    else:
        repository_path = unit_root / Path(repository_rel)
        if not _inside(repository_path, unit_root) or not repository_path.is_file():
            errors.append(f"repositories_catalog_missing:{repository_rel}")
        else:
            repository_catalog = _load(repository_path)
    baseline_repositories = repository_catalog.get("baseline_v1") or []
    repository_counts = repository_catalog.get("counts") or {}
    if not isinstance(baseline_repositories, list) or not isinstance(repository_counts, dict):
        errors.append("baseline_repository_catalog_invalid")
        baseline_repositories = []
        repository_counts = {}
    calculated_repository_counts = {
        "total": len(baseline_repositories),
        "official_local": sum(
            1
            for item in baseline_repositories
            if isinstance(item, dict) and item.get("status") == "official_local"
        ),
        "announced_pending": sum(
            1
            for item in baseline_repositories
            if isinstance(item, dict) and item.get("status") == "announced_pending"
        ),
        "none_found": sum(
            1
            for item in baseline_repositories
            if isinstance(item, dict) and item.get("status") == "none_found"
        ),
        "blocked": sum(
            1
            for item in baseline_repositories
            if isinstance(item, dict) and item.get("status") == "blocked"
        ),
    }
    if repository_counts != calculated_repository_counts:
        errors.append("baseline_repository_counts_mismatch")
    expected_counts = manifest.get("expected_counts") or {}
    for key in (
        "baseline_official_local",
        "baseline_announced_pending",
        "baseline_none_found",
    ):
        if counts.get(key) != expected_counts.get(key):
            errors.append(f"expected_{key}_mismatch")
    if counts.get("baseline_official_local") != calculated_repository_counts["official_local"]:
        errors.append("baseline_official_local_manifest_mismatch")
    if (
        counts.get("baseline_announced_pending")
        != calculated_repository_counts["announced_pending"]
    ):
        errors.append("baseline_announced_pending_manifest_mismatch")
    if counts.get("baseline_none_found") != calculated_repository_counts["none_found"]:
        errors.append("baseline_none_found_manifest_mismatch")

    for repository in baseline_repositories:
        if not isinstance(repository, dict):
            errors.append("baseline_repository_not_object")
            continue
        paper_id = str(repository.get("paper_id") or "unknown")
        status = repository.get("status")
        if paper_id == "liteframe-2026" and status != "announced_pending":
            errors.append("liteframe_must_be_announced_pending")
        if status != "official_local":
            continue
        local_rel = repository.get("path")
        repo = unit_root / Path(str(local_rel)) if local_rel else None
        if repo is None or not _inside(repo, unit_root.parent) or not repo.is_dir():
            errors.append(f"baseline_repository_missing:{paper_id}")
            continue
        head = _git(repo, "rev-parse", "HEAD")
        remote = _git(repo, "remote", "get-url", "origin")
        worktree = _git(repo, "status", "--porcelain", "--untracked-files=all")
        if (
            head.returncode != 0
            or head.stdout.strip().lower() != str(repository.get("commit") or "").lower()
        ):
            errors.append(f"baseline_repository_commit_mismatch:{paper_id}")
        if remote.returncode != 0 or _repository_key(remote.stdout.strip()) != _repository_key(
            repository.get("url")
        ):
            errors.append(f"baseline_repository_remote_mismatch:{paper_id}")
        if worktree.returncode != 0 or worktree.stdout.strip():
            errors.append(f"baseline_repository_not_clean:{paper_id}")

    for interface_name in ("entry", "site_index"):
        rel = interfaces.get(interface_name)
        path = unit_root / Path(str(rel)) if rel else None
        if path is None or not _inside(path, unit_root) or not path.is_file():
            errors.append(f"{interface_name}_missing")
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            errors.append(f"{interface_name}_unreadable:{type(exc).__name__}")
            continue
        parser = _PaperPageParser()
        parser.feed(text)
        if not re.match(r"\s*<!doctype\s+html>", text, flags=re.IGNORECASE):
            errors.append(f"{interface_name}_doctype_missing")
        if (parser.lang or "").lower() != "zh-cn":
            errors.append(f"{interface_name}_lang_invalid")
        if (parser.charset or "").lower() != "utf-8":
            errors.append(f"{interface_name}_charset_missing")
        if not parser.viewport:
            errors.append(f"{interface_name}_viewport_missing")
        for link in parser.links:
            parsed = urlparse(link)
            if parsed.scheme in {"http", "https"} or link.startswith("#"):
                continue
            candidate = (path.parent / parsed.path).resolve(strict=False)
            if not _inside(candidate, unit_root):
                errors.append(f"{interface_name}_link_outside_root:{link}")
            elif not candidate.exists():
                errors.append(f"{interface_name}_link_missing:{link}")

    html_checked = 0
    pdf_checked = 0
    repo_checked = calculated_repository_counts["official_local"]
    for record in [*source_records, *recovered_versions]:
        if not isinstance(record, dict):
            errors.append("source_record_not_object")
            continue
        source_id = str(record.get("version_id") or record.get("source_record_id") or "unknown")
        pdf = record.get("pdf") or {}
        pdf_rel = pdf.get("path")
        if not isinstance(pdf_rel, str):
            errors.append(f"pdf_path_missing:{source_id}")
        else:
            pdf_path = unit_root / Path(pdf_rel)
            if not _inside(pdf_path, unit_root) or not pdf_path.is_file():
                errors.append(f"pdf_missing:{source_id}:{pdf_rel}")
            else:
                raw_head = pdf_path.read_bytes()[:5]
                if raw_head != b"%PDF-":
                    errors.append(f"pdf_magic_invalid:{source_id}")
                expected_hash = pdf.get("sha256")
                if expected_hash and _sha256(pdf_path) != expected_hash:
                    errors.append(f"pdf_sha256_mismatch:{source_id}")
                if pdf.get("size") and pdf_path.stat().st_size != int(pdf["size"]):
                    errors.append(f"pdf_size_mismatch:{source_id}")
                pdf_checked += 1

        for field in ("analysis_path", "evidence_path"):
            rel = record.get(field)
            asset_candidate = unit_root / Path(str(rel)) if rel else None
            if (
                asset_candidate is None
                or not _inside(asset_candidate, unit_root)
                or not asset_candidate.is_file()
            ):
                errors.append(f"{field}_missing:{source_id}")

        html_rel = record.get("html_path")
        html_path = unit_root / Path(str(html_rel)) if html_rel else None
        if html_path is None or not _inside(html_path, unit_root) or not html_path.is_file():
            errors.append(f"html_missing:{source_id}")
        else:
            try:
                text = html_path.read_text(encoding="utf-8", errors="strict")
                parser = _PaperPageParser()
                parser.feed(text)
                missing = REQUIRED_SECTIONS - parser.section_ids
                if not re.match(r"\s*<!doctype\s+html>", text, flags=re.IGNORECASE):
                    errors.append(f"html_doctype_missing:{source_id}")
                if (parser.lang or "").lower() != "zh-cn":
                    errors.append(f"html_lang_invalid:{source_id}")
                if (parser.charset or "").lower() != "utf-8":
                    errors.append(f"html_charset_missing:{source_id}")
                if not parser.viewport:
                    errors.append(f"html_viewport_missing:{source_id}")
                if parser.paper_id != record.get("paper_id"):
                    errors.append(f"html_paper_id_mismatch:{source_id}")
                if missing:
                    errors.append(f"html_sections_missing:{source_id}:{','.join(sorted(missing))}")
                html_checked += 1
            except (OSError, UnicodeError) as exc:
                errors.append(f"html_unreadable:{source_id}:{type(exc).__name__}")

        code = record.get("code") or {}
        if code.get("status") == "official_local":
            local_rel = code.get("local_path")
            repo = unit_root / Path(str(local_rel)) if local_rel else None
            core_root = unit_root.parent
            if repo is None or not _inside(repo, core_root) or not repo.is_dir():
                errors.append(f"official_code_missing:{source_id}")
            else:
                completed = subprocess.run(
                    ["git", "-C", str(repo), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                actual = completed.stdout.strip().lower()
                if completed.returncode != 0 or actual != str(code.get("commit") or "").lower():
                    errors.append(f"official_code_commit_mismatch:{source_id}")
                repo_checked += 1

    for artifact in manifest.get("artifacts") or []:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            errors.append("artifact_entry_invalid")
            continue
        path = unit_root / Path(artifact["path"])
        if not _inside(path, unit_root) or not path.is_file():
            errors.append(f"artifact_missing:{artifact.get('path')}")
            continue
        if _sha256(path) != artifact.get("sha256"):
            errors.append(f"artifact_sha256_mismatch:{artifact.get('path')}")

    return {
        "schema_version": "compression-unit.validation.v2",
        "status": "passed" if not errors else "failed",
        "domain": manifest.get("domain"),
        "manifest": str(manifest_path),
        "counts": {
            "source_records": len(source_records),
            "recovered_versions": len(recovered_versions),
            "canonical_papers": len(canonical),
            "pdfs_checked": pdf_checked,
            "html_checked": html_checked,
            "repositories_checked": repo_checked,
            "errors": len(errors),
            "warnings": len(warnings),
        },
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 compression unit v2 的目录闭环。")
    parser.add_argument("--unit-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    unit_root = args.unit_root.resolve(strict=True)
    manifest = args.manifest
    if manifest is None:
        candidates = [unit_root / "outputs/manifest.v2.json", unit_root / "outputs/manifest.json"]
        manifest = next((path for path in candidates if path.is_file()), candidates[0])
    try:
        report = validate(unit_root, manifest)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
                newline="\n",
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "passed" else 1
    except Exception as exc:
        print(f"验证失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
