from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import httpx

_CITEKEY_WORD = re.compile(r"[^a-zA-Z0-9]+")


def stable_citekey(asset: Mapping[str, Any]) -> str:
    authors = asset.get("authors")
    first_author = ""
    if isinstance(authors, list) and authors:
        first_author = str(authors[0]).split(",")[0].split()[-1]
    title = str(asset.get("title") or "untitled")
    title_word = next(
        (word for word in _CITEKEY_WORD.split(unicodedata.normalize("NFKD", title)) if word),
        "untitled",
    )
    year = str(asset.get("year") or "nd")
    # UUIDv7 values created close together share their leading timestamp
    # bytes. Keep the complete compact platform ID so citekeys remain
    # collision-free across an import batch.
    suffix = _CITEKEY_WORD.sub("", str(asset.get("id") or "").casefold())
    if not suffix:
        suffix = "unmapped"
    base = f"{first_author or title_word}{year}{suffix}".lower()
    return _CITEKEY_WORD.sub("", base) or f"rah{suffix}"


def _csl_item(asset: Mapping[str, Any]) -> dict[str, Any]:
    authors = [
        {"literal": str(author)}
        for author in asset.get("authors", [])
        if str(author).strip()
    ]
    item: dict[str, Any] = {
        "id": stable_citekey(asset),
        "type": "article-journal",
        "title": str(asset.get("title") or "Untitled"),
        "author": authors,
        "issued": {"date-parts": [[asset["year"]]]} if asset.get("year") else None,
        "container-title": asset.get("venue"),
        "abstract": asset.get("abstract"),
        "DOI": asset.get("doi"),
        "URL": asset.get("url"),
        "note": f"Research Asset Hub ID: {asset.get('id', '')}",
    }
    return {key: value for key, value in item.items() if value not in (None, "", [])}


def export_csl_json(assets: Iterable[Mapping[str, Any]], path: Path) -> Path:
    payload = [_csl_item(asset) for asset in assets]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def _bibtex_escape(value: Any) -> str:
    return str(value).replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def export_bibtex(assets: Iterable[Mapping[str, Any]], path: Path) -> Path:
    entries: list[str] = []
    for asset in assets:
        fields: list[tuple[str, Any]] = [
            ("title", asset.get("title")),
            ("author", " and ".join(str(author) for author in asset.get("authors", []))),
            ("year", asset.get("year")),
            ("journal", asset.get("venue")),
            ("doi", asset.get("doi")),
            ("url", asset.get("url")),
            ("abstract", asset.get("abstract")),
            ("rah_asset_id", asset.get("id")),
        ]
        lines = [f"@article{{{stable_citekey(asset)},"]
        lines.extend(
            f"  {name} = {{{_bibtex_escape(value)}}},"
            for name, value in fields
            if value not in (None, "", [])
        )
        lines.append("}")
        entries.append("\n".join(lines))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n\n".join(entries) + "\n", encoding="utf-8")
    return path


class ZoteroLocalClient:
    """Read-only Zotero local API plus Better BibTeX JSON-RPC."""

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:23119",
        timeout_seconds: float = 5.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def api_ready(self) -> bool:
        try:
            response = httpx.get(
                f"{self.base_url}/api/",
                timeout=self.timeout_seconds,
                headers={"Zotero-API-Version": "3"},
            )
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    def list_items(self, *, collection_key: str | None = None) -> list[dict[str, Any]]:
        path = "/api/users/0/items"
        if collection_key:
            path = f"/api/users/0/collections/{collection_key}/items"
        response = httpx.get(
            f"{self.base_url}{path}",
            timeout=self.timeout_seconds,
            headers={"Zotero-API-Version": "3", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise ValueError("Zotero local API returned a non-list response")
        return [item for item in payload if isinstance(item, dict)]

    def better_bibtex_ready(self) -> dict[str, str] | None:
        payload = {
            "jsonrpc": "2.0",
            "id": "research-asset-hub",
            "method": "api.ready",
            "params": [],
        }
        try:
            response = httpx.post(
                f"{self.base_url}/better-bibtex/json-rpc",
                json=payload,
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
        except httpx.HTTPError:
            return None
        data = response.json()
        result = data.get("result") if isinstance(data, dict) else None
        if not isinstance(result, dict):
            return None
        return {str(key): str(value) for key, value in result.items()}
