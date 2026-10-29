"""Build the audited 2026-09-04 VLN intake without mutating frozen sources."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT.parent
SOURCE_ID = "vln-2026-update-20260904"
DEFAULT_ASSET_ROOT = CORE_ROOT / "个人新增资料" / "2026-09-04-vln-update"
DEFAULT_CODE_ROOT = CORE_ROOT / "共享代码" / "2026-09-04-vln-update" / "upstream"
DEFAULT_MANIFEST = PROJECT_ROOT / "config" / "intake" / SOURCE_ID / "manifest.json"
DEFAULT_WORK = PROJECT_ROOT / "work" / "validation" / SOURCE_ID
AUDITED_AT = "2026-09-03"
ATOM = {"atom": "http://www.w3.org/2005/Atom"}


@dataclass(frozen=True, slots=True)
class Candidate:
    arxiv_id: str
    category: str
    code_open: str = "unknown"
    repository_url: str | None = None
    repository_commit: str | None = None
    repository_license: str = "NOASSERTION"
    checkpoint_open: str = "not_found"
    checkpoint_urls: tuple[str, ...] = ()
    project_url: str | None = None
    note: str | None = None
    venue: str | None = None


CANDIDATES = (
    Candidate(
        "2603.13133",
        "indoor-continuous",
        "official_announced",
        "https://github.com/Allenxinn/DecoVLN",
        checkpoint_open="announced",
        project_url="https://allenxinn.github.io/DecoVLN/",
        note="Repository is a project-assets placeholder; code and checkpoints remain TODO.",
        venue="CVPR 2026",
    ),
    Candidate(
        "2607.22226",
        "outdoor",
        "official_open",
        "https://github.com/aliiisa1375/Edge_Behav",
        "0ee891c19a1a7be6dd45fcf7edb0afc101243ab6",
        checkpoint_open="not_applicable",
    ),
    Candidate("2607.23181", "indoor-continuous"),
    Candidate("2607.23504", "indoor-continuous"),
    Candidate(
        "2607.26148",
        "agentic",
        checkpoint_open="not_applicable",
        note="The paper links mini-swe-agent as a dependency, not a paper-specific implementation.",
    ),
    Candidate(
        "2607.28474",
        "agricultural",
        "official_announced",
        "https://github.com/AlexTraveling/TEA-AgriVLN",
        note="Public placeholder says the authors are packaging the code.",
    ),
    Candidate("2607.29600", "agentic", checkpoint_open="not_applicable"),
    Candidate(
        "2608.07548",
        "world-model",
        "official_open",
        "https://github.com/sunrise-ikun/SC2_WM",
        "b75d049965e76243883effc58cbb9e053995ece7",
        checkpoint_open="open",
        checkpoint_urls=(
            "https://github.com/sunrise-ikun/SC2_WM",
        ),
        venue="ICML 2026",
    ),
    Candidate("2608.00970", "aerial"),
    Candidate(
        "2608.07557",
        "aerial",
        "official_open",
        "https://github.com/XuPeng23/AeroDPO",
        "0720e145b54fb2bf90fd78c843ae81ca14364e14",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/XuPeng23/AeroDPO",),
    ),
    Candidate(
        "2608.03143",
        "indoor-continuous",
        "official_open",
        "https://github.com/BUAA-GAMMA-LAB/Route2Step",
        "abafa952037ad845d67a37a29911121bef168da4",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/XiangyunHuang/Route2Step",),
        project_url="https://buaa-gamma-lab.github.io/Route2Step/",
    ),
    Candidate(
        "2608.07267",
        "world-model",
        "official_open",
        "https://github.com/TeleHuman/WNM-3D",
        "7fe2d5f93a8dc4e2a5a2ec6fe174746928ecdcbb",
        checkpoint_open="open",
        checkpoint_urls=("https://huggingface.co/TeleEmbodied/WNM-3D",),
        project_url="https://wnm-3d.github.io/",
    ),
    Candidate("2608.09467", "aerial"),
    Candidate(
        "2608.09564",
        "aerial",
        "official_open",
        "https://github.com/mksasx/S2D-UAV-VLN",
        "458db95fe8b9f1b311c6c012c31359513a230660",
    ),
    Candidate(
        "2608.11901",
        "outdoor-benchmark",
        checkpoint_open="not_applicable",
        project_url="https://xzh0312.github.io/DaViNCi/",
        note="The linked GitHub repository is the project-page source, not implementation code.",
    ),
    Candidate("2608.12308", "aerial"),
    Candidate(
        "2608.12835",
        "aerial",
        "official_open",
        "https://github.com/iLearn-Lab/MM26-AirForesight",
        "57c81d7c26a363b88b1071bc4613601242f882d5",
        checkpoint_open="open",
        checkpoint_urls=(
            "https://huggingface.co/iLearn-Lab/MM26-AirForesight",
        ),
        venue="ACM Multimedia 2026",
    ),
    Candidate(
        "2608.12860",
        "humanoid-benchmark",
        "official_announced",
        checkpoint_open="announced",
        project_url="https://humanoid-vln.github.io/",
        note="Code, benchmark, and data are promised upon acceptance.",
    ),
    Candidate(
        "2608.17318",
        "diagnostic-benchmark",
        checkpoint_open="not_applicable",
        project_url="https://condvln.github.io/",
        note="No paper-specific implementation repository was verified.",
    ),
    Candidate(
        "2608.17512",
        "agentic",
        "official_open",
        "https://github.com/ZJU-OmniAI/Embodied-Omni",
        "d25f7efc67522d37d792e922352a2caf74223f8d",
        checkpoint_open="open",
        checkpoint_urls=(
            "https://huggingface.co/UnderTides/Embodied-Navigator-7B-GRPO",
        ),
        project_url="https://zju-omniai.github.io/Embodied-Navigator/",
        note="The checkpoint is public but manually gated.",
    ),
    Candidate("2608.22678", "aerial"),
    Candidate("2609.00920", "indoor-continuous"),
    Candidate(
        "2609.02350",
        "indoor-continuous",
        "official_announced",
        "https://github.com/kunyang-YU/LookStep",
        checkpoint_open="announced",
        note="The paper announces code/model availability, but no usable release was verified.",
        venue="EMNLP 2026 Main (author statement)",
    ),
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-root", type=Path, default=DEFAULT_ASSET_ROOT)
    parser.add_argument("--code-root", type=Path, default=DEFAULT_CODE_ROOT)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--download-pdfs", action="store_true")
    return parser.parse_args()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
    ) as handle:
        handle.write(rendered)
        temporary = Path(handle.name)
    temporary.replace(path)


def _http_bytes(url: str, *, timeout: float) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=timeout,
                headers={"User-Agent": "ResearchAssetHub/1.0"},
            ) as client:
                response = client.get(url)
                response.raise_for_status()
                return response.content
        except (httpx.HTTPError, OSError) as error:
            last_error = error
            if attempt < 3:
                time.sleep(2**attempt)
    raise RuntimeError(f"failed to fetch {url}: {last_error}") from last_error


def _fetch_feed(candidates: tuple[Candidate, ...]) -> dict[str, dict[str, Any]]:
    identifiers = ",".join(item.arxiv_id for item in candidates)
    content = _http_bytes(
        f"https://export.arxiv.org/api/query?id_list={identifiers}&max_results={len(candidates)}",
        timeout=90,
    )
    root = ET.fromstring(content)  # noqa: S314
    result: dict[str, dict[str, Any]] = {}
    for entry in root.findall("atom:entry", ATOM):
        identifier = (entry.findtext("atom:id", default="", namespaces=ATOM)).rsplit("/", 1)[-1]
        identifier = identifier.split("v", 1)[0]
        result[identifier] = {
            "title": " ".join(entry.findtext("atom:title", default="", namespaces=ATOM).split()),
            "abstract": " ".join(
                entry.findtext("atom:summary", default="", namespaces=ATOM).split()
            ),
            "authors": [
                " ".join(author.findtext("atom:name", default="", namespaces=ATOM).split())
                for author in entry.findall("atom:author", ATOM)
            ],
            "published": entry.findtext("atom:published", default="", namespaces=ATOM)[:10],
        }
    missing = sorted({item.arxiv_id for item in candidates} - set(result))
    if missing:
        raise RuntimeError(f"arXiv metadata missing for: {missing}")
    return result


def _slug(title: str, arxiv_id: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.casefold())
    stem = "-".join(words[:14]).strip("-")
    return f"{stem or 'vln-paper'}-2026-{arxiv_id.replace('.', '-')}"


def _download_pdf(arxiv_id: str, target: Path) -> str:
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        content = _http_bytes(f"https://arxiv.org/pdf/{arxiv_id}", timeout=180)
        if not content.startswith(b"%PDF-"):
            raise RuntimeError(f"download is not a PDF: {arxiv_id}")
        target.write_bytes(content)
    content = target.read_bytes()
    if not content.startswith(b"%PDF-"):
        raise RuntimeError(f"existing file is not a PDF: {target}")
    return hashlib.sha256(content).hexdigest()


def _repository_path(code_root: Path, repository_url: str | None) -> Path | None:
    if not repository_url:
        return None
    parts = repository_url.removesuffix(".git").rstrip("/").split("/")
    if len(parts) < 2:
        return None
    return code_root / "github" / parts[-2] / parts[-1]


def _repository_is_local(path: Path | None, commit: str | None) -> bool:
    if path is None or commit is None or not (path / ".git").exists():
        return False
    head = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    dirty = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return (
        head.returncode == 0
        and head.stdout.strip().casefold() == commit.casefold()
        and dirty.returncode == 0
        and not dirty.stdout.strip()
    )


def build(
    arguments: argparse.Namespace,
    *,
    candidates: tuple[Candidate, ...] = CANDIDATES,
    source_id: str = SOURCE_ID,
) -> dict[str, Any]:
    metadata = _fetch_feed(candidates)
    items: list[dict[str, Any]] = []
    paper_records: list[dict[str, Any]] = []
    approvals: list[dict[str, Any]] = []
    for candidate in candidates:
        facts = metadata[candidate.arxiv_id]
        source_record_id = _slug(facts["title"], candidate.arxiv_id)
        pdf_path = arguments.asset_root / "papers" / source_record_id / "paper.pdf"
        pdf_sha256 = (
            _download_pdf(candidate.arxiv_id, pdf_path)
            if arguments.download_pdfs
            else hashlib.sha256(pdf_path.read_bytes()).hexdigest()
            if pdf_path.exists()
            else None
        )
        repository_path = _repository_path(arguments.code_root, candidate.repository_url)
        code_local = (
            "full"
            if candidate.code_open == "official_open"
            and _repository_is_local(repository_path, candidate.repository_commit)
            else "absent"
        )
        source_urls = [
            f"https://arxiv.org/abs/{candidate.arxiv_id}",
            f"https://arxiv.org/pdf/{candidate.arxiv_id}",
            candidate.project_url,
            candidate.repository_url,
            *candidate.checkpoint_urls,
        ]
        source_urls = list(dict.fromkeys(value for value in source_urls if value))
        release_status = {
            "code_open": candidate.code_open,
            "code_local": code_local,
            "checkpoint_open": candidate.checkpoint_open,
            "repository_url": candidate.repository_url,
            "checkpoint_urls": list(candidate.checkpoint_urls),
            "verified_at": AUDITED_AT,
            "evidence_urls": source_urls,
            "note": candidate.note,
        }
        representations: dict[str, Any] = {
            "pdf": {
                "path": f"papers/{source_record_id}/paper.pdf" if pdf_sha256 else None,
                "sha256": pdf_sha256,
            }
        }
        if candidate.code_open == "official_open" and candidate.repository_url:
            representations["code"] = {
                "path": (
                    f"github/{repository_path.parent.name}/{repository_path.name}"
                    if code_local == "full" and repository_path is not None
                    else None
                ),
                "url": candidate.repository_url,
                "commit": candidate.repository_commit,
                "license": candidate.repository_license,
                "status": "official_local" if code_local == "full" else "official_url_only",
            }
        item = {
            "id": source_record_id,
            "title": facts["title"],
            "title_zh": None,
            "authors": facts["authors"],
            "year": int(facts["published"][:4]),
            "venue": candidate.venue or "arXiv preprint",
            "doi": f"10.48550/arXiv.{candidate.arxiv_id}",
            "arxiv_id": candidate.arxiv_id,
            "abstract": facts["abstract"],
            "summary": None,
            "tags": [
                "vision-language-navigation",
                "vln-2026-update",
                candidate.category,
            ],
            "source_urls": source_urls,
            "attributes": {
                "kind": "research",
                "first_public_date": facts["published"],
                "publication_status": "公开论文",
                "research_scope": "strict-vln",
                "release_status": release_status,
            },
            "representations": representations,
        }
        items.append(item)
        paper_records.append(
            {
                "paper_id": source_record_id,
                "title": facts["title"],
                "authors": facts["authors"],
                "year": int(facts["published"][:4]),
                "venue": candidate.venue or "arXiv preprint",
                "doi": item["doi"],
                "arxiv_id": candidate.arxiv_id,
                "domain_slugs": ["vision-language-navigation"],
                "code": {"status": "unknown"},
            }
        )
        if candidate.code_open == "official_open" and candidate.repository_url:
            assert candidate.repository_commit is not None
            parts = candidate.repository_url.removesuffix(".git").rstrip("/").split("/")
            approvals.append(
                {
                    "url": candidate.repository_url,
                    "official": True,
                    "commit": candidate.repository_commit,
                    "destination": f"github/{parts[-2]}/{parts[-1]}",
                    "papers": [
                        {
                            "paper_id": source_record_id,
                            "exact_title": facts["title"],
                        }
                    ],
                    "sparse_paths": [],
                    "evidence": [
                        f"https://arxiv.org/abs/{candidate.arxiv_id}",
                        candidate.repository_url,
                        f"Audited {AUDITED_AT}: official implementation with source code.",
                    ],
                    "licenses": [candidate.repository_license],
                }
            )

    document = {
        "schema_version": "research-asset-hub.generic-manifest/v1",
        "source_id": source_id,
        "audited_at": AUDITED_AT,
        "items": items,
    }
    _write_json(arguments.manifest, document)
    arguments.work_dir.mkdir(parents=True, exist_ok=True)
    records_path = arguments.work_dir / "paper-records.jsonl"
    records_path.write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in paper_records),
        encoding="utf-8",
    )
    approvals_path = arguments.work_dir / "paper-code-approvals.json"
    _write_json(
        approvals_path,
        {"schema_version": "paper-code-approvals/v1", "repositories": approvals, "no_code": []},
    )
    report = {
        "status": "built",
        "source_id": source_id,
        "papers": len(items),
        "pdfs_present": sum(bool(item["representations"]["pdf"]["path"]) for item in items),
        "official_open_code": sum(
            item["attributes"]["release_status"]["code_open"] == "official_open"
            for item in items
        ),
        "local_code": sum(
            item["attributes"]["release_status"]["code_local"] == "full"
            for item in items
        ),
        "checkpoint_open": sum(
            item["attributes"]["release_status"]["checkpoint_open"] == "open"
            for item in items
        ),
        "manifest": str(arguments.manifest.resolve()),
        "asset_root": str(arguments.asset_root.resolve()),
        "code_root": str(arguments.code_root.resolve()),
        "paper_records": str(records_path.resolve()),
        "approvals": str(approvals_path.resolve()),
    }
    _write_json(arguments.work_dir / "build-report.json", report)
    return report


def main() -> None:
    print(json.dumps(build(_arguments()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
