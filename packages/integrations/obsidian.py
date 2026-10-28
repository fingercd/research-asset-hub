from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_FRONTMATTER = re.compile(r"\A---\r?\n(?P<yaml>.*?)\r?\n---\r?\n", re.DOTALL)
_USER_BLOCK = re.compile(
    r"<!-- rah:user:start -->\r?\n(?P<body>.*?)\r?\n<!-- rah:user:end -->",
    re.DOTALL,
)
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _digest(fields: dict[str, Any], note_body: str) -> str:
    payload = {"fields": _jsonable(fields), "note_body": note_body}
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _safe_filename(title: str, asset_id: str) -> str:
    safe = _UNSAFE_FILENAME.sub(" ", title).strip(" .")
    safe = re.sub(r"\s+", " ", safe)[:90] or "Untitled"
    # UUIDv7 values created in the same millisecond share a long leading
    # prefix, so truncating from the front can overwrite a sibling asset.
    return f"{safe}--{asset_id}.md"


@dataclass(frozen=True, slots=True)
class ObsidianAssetDocument:
    asset_id: str
    hub_revision: int
    export_digest: str
    fields: dict[str, Any]
    note_body: str
    path: Path | None = None
    content_digest: str = ""


class ObsidianVaultAdapter:
    """Bidirectional, ID-based Obsidian Markdown adapter."""

    def __init__(
        self,
        vault_path: Path,
        hub_directory: str = "00_Home/Research Asset Hub",
    ) -> None:
        self.vault_path = vault_path.resolve()
        self.root = (self.vault_path / hub_directory).resolve()
        if self.vault_path not in self.root.parents and self.root != self.vault_path:
            raise ValueError("hub_directory must stay inside the Obsidian vault")
        self.assets_directory = self.root / "Assets"

    def render(
        self,
        *,
        asset_id: str,
        hub_revision: int,
        fields: dict[str, Any],
        note_body: str = "",
    ) -> str:
        normalized_fields = _jsonable(fields)
        digest = _digest(normalized_fields, note_body)
        title = str(normalized_fields.get("title") or "Untitled")
        properties = {
            "rah_asset_id": asset_id,
            "rah_revision": hub_revision,
            "rah_export_digest": digest,
            "title": title,
            "aliases": normalized_fields.get("aliases") or [],
            "tags": normalized_fields.get("tags") or [],
            "authors": normalized_fields.get("authors") or [],
            "year": normalized_fields.get("year"),
            "venue": normalized_fields.get("venue"),
            "doi": normalized_fields.get("doi"),
            "arxiv": normalized_fields.get("arxiv"),
            "domains": normalized_fields.get("domains") or [],
            "reading_status": normalized_fields.get("reading_status", "unread"),
            "visibility": normalized_fields.get("visibility", "private"),
            "relations": normalized_fields.get("relations") or [],
            "cssclasses": ["research-asset-hub"],
        }
        extra = {
            key: value
            for key, value in normalized_fields.items()
            if key not in properties and not key.startswith("rah_")
        }
        properties.update(extra)
        frontmatter = yaml.safe_dump(
            properties,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).strip()
        relation_links = [
            f"- [[{relation}]]"
            for relation in properties["relations"]
            if isinstance(relation, str) and relation
        ]
        managed = [
            "<!-- rah:managed:start -->",
            f"# {title}",
            "",
            "> [!info] Research Asset Hub",
            f"> 资产 ID：`{asset_id}`。元数据可编辑；系统 ID、来源路径和校验值只读。",
            "",
            "## 关联资产",
            "",
            *(relation_links or ["无"]),
            "<!-- rah:managed:end -->",
        ]
        return (
            f"---\n{frontmatter}\n---\n\n"
            + "\n".join(managed)
            + "\n\n<!-- rah:user:start -->\n"
            + note_body.rstrip()
            + "\n<!-- rah:user:end -->\n"
        )

    def parse(self, path: Path) -> ObsidianAssetDocument:
        resolved = path.resolve()
        if self.root != resolved and self.root not in resolved.parents:
            raise ValueError("note path escapes the Research Asset Hub directory")
        text = resolved.read_text(encoding="utf-8-sig")
        match = _FRONTMATTER.match(text)
        if match is None:
            raise ValueError(f"Missing YAML frontmatter: {resolved}")
        properties = yaml.safe_load(match.group("yaml"))
        if not isinstance(properties, dict):
            raise ValueError(f"Invalid YAML frontmatter: {resolved}")
        asset_id = str(properties.pop("rah_asset_id", "")).strip()
        if not asset_id:
            raise ValueError(f"Missing rah_asset_id: {resolved}")
        revision = int(properties.pop("rah_revision", 0))
        properties.pop("rah_export_digest", None)
        properties.pop("cssclasses", None)
        user_match = _USER_BLOCK.search(text)
        note_body = user_match.group("body").rstrip() if user_match else ""
        return ObsidianAssetDocument(
            asset_id=asset_id,
            hub_revision=revision,
            export_digest=_digest(_jsonable(properties), note_body),
            fields=_jsonable(properties),
            note_body=note_body,
            path=resolved,
            content_digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def update_fields(
        self,
        document: ObsidianAssetDocument,
        *,
        fields: dict[str, Any],
        hub_revision: int,
        on_write: Callable[[Path, str | None, str], None] | None = None,
    ) -> Path:
        """Update frontmatter while retaining the current Markdown body verbatim."""
        assert document.path is not None
        text = document.path.read_text(encoding="utf-8-sig")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != document.content_digest:
            raise ObsidianContentChanged("Obsidian note changed during sync.")
        match = _FRONTMATTER.match(text)
        assert match is not None
        properties = yaml.safe_load(match.group("yaml"))
        properties.update(fields)
        properties["rah_revision"] = hub_revision
        properties["rah_export_digest"] = _digest(fields, document.note_body)
        frontmatter = yaml.safe_dump(properties, allow_unicode=True, sort_keys=False).strip()
        content = f"---\n{frontmatter}\n---\n" + text[match.end() :]
        self._write_content(document.path, content, document.content_digest, on_write)
        return document.path

    def write(
        self,
        *,
        asset_id: str,
        hub_revision: int,
        fields: dict[str, Any],
        note_body: str = "",
        existing_path: Path | None = None,
        expected_content_digest: str | None = None,
        on_write: Callable[[Path, str | None, str], None] | None = None,
    ) -> Path:
        self.assets_directory.mkdir(parents=True, exist_ok=True)
        target = existing_path or self.assets_directory / _safe_filename(
            str(fields.get("title") or "Untitled"),
            asset_id,
        )
        resolved = target.resolve()
        if self.assets_directory.resolve() not in resolved.parents:
            raise ValueError("asset note path escapes the managed directory")
        content = self.render(
            asset_id=asset_id,
            hub_revision=hub_revision,
            fields=fields,
            note_body=note_body,
        )
        self._write_content(resolved, content, expected_content_digest, on_write)
        return resolved

    def _write_content(
        self,
        resolved: Path,
        content: str,
        expected_content_digest: str | None,
        on_write: Callable[[Path, str | None, str], None] | None = None,
    ) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{resolved.stem}-",
            suffix=".tmp",
            dir=resolved.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            temporary.write_text(content, encoding="utf-8", newline="\n")
            original = resolved.read_text(encoding="utf-8-sig") if resolved.exists() else None
            if expected_content_digest is not None:
                if original is None:
                    raise ObsidianContentChanged("Obsidian note was removed during sync.")
                if hashlib.sha256(original.encode("utf-8")).hexdigest() != expected_content_digest:
                    raise ObsidianContentChanged("Obsidian note changed during sync.")
            temporary.replace(resolved)
            if on_write is not None:
                on_write(resolved, original, hashlib.sha256(content.encode("utf-8")).hexdigest())
        finally:
            temporary.unlink(missing_ok=True)

    def scan(self) -> list[ObsidianAssetDocument]:
        if not self.assets_directory.exists():
            return []
        documents: list[ObsidianAssetDocument] = []
        for path in sorted(self.assets_directory.glob("*.md")):
            documents.append(self.parse(path))
        return documents


class ObsidianContentChanged(ValueError):
    """The external document no longer matches the content being applied."""
