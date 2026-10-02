"""Read source-bound figure and methodology derivatives; never read arbitrary paths."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from .service import KnowledgeError

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
_FIGURE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _json(path: Path, root: Path) -> dict[str, Any]:
    try:
        path.resolve().relative_to(root.resolve())
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("Derivative exceeds size limit.")
        result = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(result, dict) or result.get("schema_version") != 1:
            raise ValueError("Unsupported derivative schema.")
        return result
    except (OSError, ValueError) as error:
        raise KnowledgeError(
            "invalid_derivative", "The derivative is missing or invalid.", 409
        ) from error


class DerivativeStore:
    def __init__(self, cache_root: Path) -> None:
        self.root = cache_root

    def list_figures(self, asset_id: str | None = None) -> list[dict[str, Any]]:
        paths = (
            [self.root / str(UUID(asset_id)) / "figures.json"]
            if asset_id
            else sorted(self.root.glob("*/figures.json"))
        )
        figures = []
        for path in paths:
            if not path.is_file():
                continue
            document = _json(path, self.root)
            try:
                asset_id_value = str(UUID(document["asset_id"]))
                UUID(document["representation_id"])
                figures_data = document["figures"]
                if not isinstance(figures_data, list) or not isinstance(
                    document["source_hash"], str
                ):
                    raise ValueError("Invalid figure manifest fields.")
            except (KeyError, TypeError, ValueError) as error:
                raise KnowledgeError("invalid_derivative", "Figure metadata is invalid.") from error
            if asset_id_value != path.parent.name:
                raise KnowledgeError("invalid_derivative", "Figure ownership is inconsistent.")
            metadata_path = path.parent / "metadata.json"
            metadata = _json(metadata_path, self.root) if metadata_path.is_file() else {}
            for figure in figures_data:
                if not isinstance(figure, dict) or not _FIGURE_ID.fullmatch(
                    str(figure.get("figure_id", ""))
                ):
                    raise KnowledgeError("invalid_derivative", "Figure identifier is invalid.")
                figures.append(
                    {
                        **figure,
                        "asset_id": document["asset_id"],
                        "representation_id": document["representation_id"],
                        "source_hash": document["source_hash"],
                        "extractor": document.get("extractor"),
                        "extractor_version": document.get("extractor_version"),
                        "quality_status": figure.get("quality_status", "candidate"),
                        "paper_title": metadata.get("title", ""),
                        "domains": metadata.get("domains", []),
                    }
                )
        return figures

    def get_figure(self, figure_id: str) -> dict[str, Any]:
        if not _FIGURE_ID.fullmatch(figure_id):
            raise KnowledgeError("invalid_figure_id", "Invalid figure identifier.", 422)
        matches = [f for f in self.list_figures() if f["figure_id"] == figure_id]
        if not matches:
            raise KnowledgeError(
                "figure_not_found", "No such figure in the current derivatives.", 404
            )
        if len(matches) != 1:
            raise KnowledgeError("ambiguous_figure", "Figure identifier is duplicated.")
        return matches[0]

    def image(self, figure: dict[str, Any], *, preview: bool = True) -> tuple[bytes, str]:
        key = "preview_path" if preview else "image_path"
        relative = figure.get(key)
        if not relative:
            raise KnowledgeError("image_not_available", "The figure has no requested image.", 404)
        paper_root = self.root / str(UUID(figure["asset_id"]))
        path = paper_root / relative
        try:
            path.resolve().relative_to(paper_root.resolve())
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise KnowledgeError("image_too_large", "Use the smaller figure preview.", 413)
            with path.open("rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        except (OSError, ValueError) as error:
            raise KnowledgeError(
                "image_not_available", "The figure image is unavailable.", 404
            ) from error
        if len(data) > MAX_IMAGE_BYTES:
            raise KnowledgeError("image_too_large", "Use the smaller figure preview.", 413)
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data, "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return data, "image/jpeg"
        raise KnowledgeError("invalid_image", "Only PNG and JPEG figure images are supported.")

    def methodology_cards(
        self,
        *,
        kind: str | None = None,
        domain: str | None = None,
        query: str = "",
        include_candidates: bool = False,
    ) -> list[dict[str, Any]]:
        # The cards live next to the versioned derivatives and contain no raw source files.
        path = self.root.parent / "methodology.json"
        if not path.exists():
            return []
        document = _json(path, self.root.parent)
        query = query.strip().casefold()
        return [
            card
            for card in document.get("cards", [])
            if (not kind or card["kind"] == kind)
            and (not domain or domain in card["domains"])
            and (include_candidates or card.get("quality_status") == "reviewed")
            and (not query or query in json.dumps(card, ensure_ascii=False).casefold())
        ]
