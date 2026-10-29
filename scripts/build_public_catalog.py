"""Build a publication-safe reference catalog from the read-only local export.

Never upload the input JSONL. Only explicitly selected bibliographic fields and
validated public URLs leave this process. No database or source files are modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

import yaml

ROOT = Path(__file__).resolve().parents[1]
PAPER_HOSTS = frozenset(
    {
        "arxiv.org",
        "export.arxiv.org",
        "doi.org",
        "dx.doi.org",
        "proceedings.mlr.press",
        "openreview.net",
        "openaccess.thecvf.com",
        "aclanthology.org",
        "proceedings.neurips.cc",
        "papers.neurips.cc",
        "papers.nips.cc",
        "www.ijcai.org",
        "www.roboticsproceedings.org",
        "roboticsproceedings.org",
        "www.usenix.org",
        "ojs.aaai.org",
        "cdn.aaai.org",
        "www.nature.com",
        "link.springer.com",
        "www.ecva.net",
        "proceedings.mlsys.org",
        "proceedings.iclr.cc",
        "www.jair.org",
        "jair.org",
        "dl.acm.org",
        "jmlr.org",
        "www.jmlr.org",
        "pubmed.ncbi.nlm.nih.gov",
        "www.sciencedirect.com",
        "www.frontiersin.org",
        "cdn.openai.com",
        "www-cdn.anthropic.com",
        "raw.githubusercontent.com",
    }
)
CODE_HOSTS = frozenset({"github.com", "gitlab.com", "huggingface.co"})
RECORD_FIELDS = frozenset(
    {"id", "title", "title_zh", "year", "venue", "domains", "paper_urls", "code_urls"}
)
PRIVATE_MARKERS = re.compile(
    r"(?:[a-z]:[\\/]|\\\\|(?:^|[\s/])(?:localhost|127\.0\.0\.1)(?:[\s/:]|$))", re.I
)


def clean_text(value: Any, limit: int = 600) -> str:
    """Accept only bounded scalar bibliographic text; never stringify rich objects."""
    if not isinstance(value, str) or PRIVATE_MARKERS.search(value):
        return ""
    return " ".join(value.split())[:limit]


def public_url(value: Any, *, code: bool = False) -> str | None:
    """Exact-host allowlist, public identity paths, and closed query policy.

    Unknown domains, credentials, arbitrary ports, fragments, escaped delimiters,
    private paths and tracking/token queries are rejected rather than forwarded.
    This validates URL shape, not online availability or a repository's ownership.
    """
    if not isinstance(value, str) or len(value) > 2048:
        return None
    if re.search(r"[\s\\\x00-\x1f\x7f]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
            or parsed.fragment
            or host not in (CODE_HOSTS if code else PAPER_HOSTS)
        ):
            return None
    except ValueError:
        return None
    path = parsed.path
    decoded = unquote(path)
    if (
        re.search(r"[\\\x00-\x20\x7f]", decoded)
        or any(part in {".", ".."} for part in decoded.split("/"))
        or re.search(r"%2f|%5c|%25", path, re.I)
        or PRIVATE_MARKERS.search(decoded)
    ):
        return None
    query = ""
    if parsed.query:
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if not code and host == "openreview.net" and len(pairs) == 1:
            key, ident = pairs[0]
            if key != "id" or not re.fullmatch(r"[A-Za-z0-9_-]{3,100}", ident):
                return None
            query = urlencode({"id": ident})
        else:
            return None
    if code:
        parts = path.strip("/").removesuffix(".git").split("/")
        if host == "huggingface.co" and parts and parts[0] in {"spaces", "datasets"}:
            parts = parts[1:]
            prefix = "/" + path.strip("/").split("/")[0]
        else:
            prefix = ""
        if len(parts) != 2 or any(not re.fullmatch(r"[\w.-]+", p) for p in parts):
            return None
        if parts[0].lower() in {"settings", "login", "search", "new", "explore"}:
            return None
        path = prefix + "/" + "/".join(parts)
    elif host in {"arxiv.org", "export.arxiv.org"}:
        match = re.fullmatch(
            r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7}))(?:v\d+)?(?:\.pdf)?/?",
            path,
        )
        if not match:
            return None
        host, path = "arxiv.org", "/abs/" + match[1]
    elif host in {"doi.org", "dx.doi.org"}:
        if not re.fullmatch(r"/10\.\d{4,9}/\S+", path):
            return None
        host = "doi.org"
    elif host == "openreview.net":
        if path not in {"/forum", "/pdf"} or not query:
            return None
        path = "/forum"
    elif host == "raw.githubusercontent.com":
        # PMLR's official publication mirror; arbitrary raw GitHub content is not a paper.
        if not re.fullmatch(r"/mlresearch/v\d+/[^?#]+\.pdf", path):
            return None
    elif host == "proceedings.mlr.press":
        if not re.fullmatch(r"/v\d+/[^/]+\.(?:html|pdf)", path):
            return None
    elif host == "aclanthology.org":
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9._-]+/?", path):
            return None
    elif not path.strip("/") or not (
        re.search(r"\.(?:html?|pdf)$", path, re.I)
        or re.search(
            r"/(?:doi|article|articles|paper|papers|abs|content|volume|page|proceedings)/", path
        )
        or host == "pubmed.ncbi.nlm.nih.gov"
        and re.fullmatch(r"/\d+/?", path)
    ):
        return None
    return urlunsplit(("https", host, path, query, ""))


def urls_in(record: dict[str, Any]) -> list[Any]:
    asset = record["asset"]
    return [
        *asset.get("source_urls", []),
        *(item.get("external_url") for item in record.get("representations", [])),
        *(
            item.get("canonical")
            for item in asset.get("external_identifiers", [])
            if item.get("scheme") == "url"
        ),
    ]


def load_domains(path: Path) -> dict[str, str]:
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        item["id"]: item["label"]
        for item in config["domains"]
        if not item.get("hidden_from_catalog", False)
    }


def apply_metadata_corrections(records: list[dict[str, Any]], corrections_path: Path) -> int:
    """Apply explicitly evidenced display-only corrections after identity deduplication."""
    config = json.loads(corrections_path.read_text(encoding="utf-8"))
    if config["schema"] != "research-asset-hub.public-metadata-corrections/v1":
        raise ValueError("Unsupported public metadata corrections schema")
    by_url = {url: entry for entry in records for url in entry["paper_urls"]}
    for correction in config["corrections"]:
        entry = by_url[correction["url"]]
        if entry["title"] != correction["expected_title"]:
            raise ValueError("Public metadata correction title no longer matches its target")
        for field in correction["clear_fields"]:
            if field != "title_zh":
                raise ValueError("Public corrections only allow clearing the translated title")
            entry[field] = ""
    return len(config["corrections"])


def build_catalog(
    input_path: Path, taxonomy: Path, snapshot_date: str, corrections_path: Path | None = None
) -> dict[str, Any]:
    domains = load_domains(taxonomy)
    # A first pass keeps only public repository URLs, keyed transiently by true asset ID.
    # IDs, relations, source metadata and private fields are never emitted.
    repositories: dict[str, list[str]] = {}
    stats: Counter[str] = Counter()
    with input_path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            asset = record["asset"]
            stats["input_" + asset["asset_type"]] += 1
            if asset["asset_type"] == "repository" and not asset.get("deleted_at"):
                repositories[asset["asset_id"]] = sorted(
                    {url for raw in urls_in(record) if (url := public_url(raw, code=True))}
                )

    records: list[dict[str, Any]] = []
    url_index: dict[str, dict[str, Any]] = {}
    with input_path.open(encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            asset = record["asset"]
            if asset["asset_type"] != "paper":
                continue
            if asset.get("deleted_at"):
                stats["excluded_deleted"] += 1
                continue
            paper_urls: set[str] = set()
            for raw in urls_in(record):
                url = public_url(raw)
                if url:
                    paper_urls.add(url)
                elif raw:
                    stats["rejected_url_candidates"] += 1
            if not paper_urls:
                stats["excluded_no_public_paper_url"] += 1
                continue
            public_domains = sorted(set(asset.get("domains", [])) & domains.keys())
            if not public_domains:
                stats["excluded_no_visible_domain"] += 1
                continue
            title = clean_text(asset.get("title"))
            if not title:
                stats["excluded_no_title"] += 1
                continue
            code_urls = {
                url
                for relation in record.get("relations", [])
                if relation.get("relation_type") == "implemented_by"
                and relation.get("confirmation_status") == "confirmed"
                and relation.get("subject_asset_id") == asset["asset_id"]
                for url in repositories.get(relation.get("object_asset_id", ""), [])
            }
            year = asset.get("year")
            entry = {
                "id": hashlib.sha256(min(paper_urls).encode()).hexdigest()[:20],
                "title": title,
                "title_zh": clean_text(asset.get("title_zh")),
                "year": year if type(year) is int and 1900 <= year <= 2100 else None,
                "venue": clean_text(asset.get("venue"), 160),
                "domains": public_domains,
                "paper_urls": sorted(paper_urls, key=paper_url_rank),
                "code_urls": sorted(code_urls),
            }
            matches = {id(url_index[u]): url_index[u] for u in paper_urls if u in url_index}
            if matches:
                # A new record may bridge two earlier records through two public identities.
                # Merge that whole connected component, not only its first matching URL.
                duplicate = next(iter(matches.values()))
                stats["merged_same_public_url"] += len(matches)
                combined = [entry, *matches.values()]
                duplicate["domains"] = sorted({d for item in combined for d in item["domains"]})
                duplicate["paper_urls"] = sorted(
                    {u for item in combined for u in item["paper_urls"]}, key=paper_url_rank
                )
                duplicate["code_urls"] = sorted({u for item in combined for u in item["code_urls"]})
                records = [item for item in records if id(item) not in matches or item is duplicate]
                for url in duplicate["paper_urls"]:
                    url_index[url] = duplicate
            else:
                records.append(entry)
                for url in paper_urls:
                    url_index[url] = entry
    if corrections_path is not None:
        stats["metadata_corrections_applied"] = apply_metadata_corrections(
            records, corrections_path
        )
    records.sort(key=lambda item: (-(item["year"] or 0), item["title"].casefold()))
    used_domains = {domain for item in records for domain in item["domains"]}
    stats["published_papers"] = len(records)
    stats["papers_with_code"] = sum(bool(item["code_urls"]) for item in records)
    stats["public_code_links"] = len({u for item in records for u in item["code_urls"]})
    return {
        "schema": "research-asset-hub.public-bibliography/v1",
        "snapshot_date": snapshot_date,
        "scope": (
            "Public reference metadata and registered code links only; no hosted papers or notes."
        ),
        "domains": [
            {"id": key, "label": value} for key, value in domains.items() if key in used_domains
        ],
        "stats": dict(sorted(stats.items())),
        "papers": records,
    }


def paper_url_rank(url: str) -> tuple[int, str]:
    host = urlsplit(url).hostname
    order = {"doi.org": 0, "arxiv.org": 2, "raw.githubusercontent.com": 9}
    return order.get(host or "", 1), url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=ROOT / "outputs/open-catalog/catalog.jsonl")
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "outputs/open-catalog/manifest.json"
    )
    parser.add_argument("--taxonomy", type=Path, default=ROOT / "config/taxonomy.yaml")
    parser.add_argument("--output", type=Path, default=ROOT / "site/dist/assets/catalog.json")
    parser.add_argument("--corrections", type=Path, default=ROOT / "site/metadata-corrections.json")
    args = parser.parse_args()
    snapshot_date = json.loads(args.manifest.read_text(encoding="utf-8"))["generated_at"][:10]
    catalog = build_catalog(args.input, args.taxonomy, snapshot_date, args.corrections)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"),
        encoding="utf-8",
    )
    print(json.dumps({"snapshot_date": snapshot_date, **catalog["stats"]}, ensure_ascii=False))
    print(f"Public catalog: {args.output.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
