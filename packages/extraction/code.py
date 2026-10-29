"""Source-code extraction with Python symbols and text fallback."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path
from typing import Any

from .common import read_text
from .models import (
    ExtractionChunk,
    ExtractionResult,
    ExtractionStatus,
    LineAnchor,
    hash_bytes,
    make_chunk,
)

_CODE_SUFFIXES = {
    ".py",
    ".pyi",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cc",
    ".cpp",
    ".cu",
    ".cuh",
    ".h",
    ".hpp",
    ".cs",
    ".sh",
    ".ps1",
    ".sql",
    ".r",
    ".jl",
    ".lua",
}
_TREE_SITTER_LANGUAGES = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".h": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".sh": "bash",
    ".ps1": "powershell",
    ".sql": "sql",
    ".r": "r",
    ".jl": "julia",
}
_TREE_SITTER_SAFE_SUFFIXES = frozenset(
    {".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs"}
)
_SYMBOL_NODE_TYPES = frozenset(
    {
        "class_declaration",
        "class_definition",
        "constructor_declaration",
        "enum_declaration",
        "function_declaration",
        "function_definition",
        "function_item",
        "interface_declaration",
        "method_declaration",
        "method_definition",
        "module",
        "struct_item",
        "trait_item",
        "type_alias_declaration",
    }
)


class CodeExtractor:
    name = "code"
    version = "5"

    def __init__(
        self,
        *,
        fallback_lines: int = 200,
        max_symbols_per_file: int = 250,
        max_fallback_chunks_per_file: int = 250,
        max_chunk_characters: int = 50_000,
        enable_tree_sitter: bool = True,
    ) -> None:
        self.fallback_lines = fallback_lines
        self.max_symbols_per_file = max_symbols_per_file
        self.max_fallback_chunks_per_file = max_fallback_chunks_per_file
        self.max_chunk_characters = max_chunk_characters
        self.enable_tree_sitter = enable_tree_sitter
        if min(
            fallback_lines,
            max_symbols_per_file,
            max_fallback_chunks_per_file,
            max_chunk_characters,
        ) <= 0:
            raise ValueError("Code extraction limits must be positive.")

    def _bounded_text(self, value: str) -> tuple[str, bool]:
        if len(value) <= self.max_chunk_characters:
            return value, False
        return value[: self.max_chunk_characters].rstrip() + "\n…", True

    def supports(self, path: Path) -> bool:
        return path.suffix.casefold() in _CODE_SUFFIXES

    def _python_symbols(
        self,
        path: Path,
        text: str,
        *,
        representation_id: str,
        representation_hash: str,
    ) -> list[ExtractionChunk]:
        tree = ast.parse(text)
        lines = text.splitlines()
        chunks: list[ExtractionChunk] = []
        for node in tree.body:
            if len(chunks) >= self.max_symbols_per_file:
                break
            if not isinstance(
                node,
                (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
            ):
                continue
            end_line = getattr(node, "end_lineno", node.lineno)
            content, truncated = self._bounded_text(
                "\n".join(lines[node.lineno - 1 : end_line]).strip()
            )
            if not content:
                continue
            anchor: LineAnchor = {
                "kind": "line",
                "path": path.as_posix(),
                "line_start": node.lineno,
                "line_end": end_line,
                "symbol": node.name,
            }
            chunks.append(
                make_chunk(
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                    ordinal=len(chunks),
                    text=content,
                    extractor=self.name,
                    extractor_version=self.version,
                    anchor=anchor,
                    title=node.name,
                    metadata={
                        "symbol_kind": type(node).__name__,
                        "truncated": truncated,
                    },
                )
            )
        return chunks

    def _tree_sitter_symbols(
        self,
        path: Path,
        text: str,
        *,
        representation_id: str,
        representation_hash: str,
    ) -> list[ExtractionChunk]:
        language = _TREE_SITTER_LANGUAGES.get(path.suffix.casefold())
        if language is None:
            return []
        package: Any = importlib.import_module("tree_sitter_language_pack")
        parser: Any = package.get_parser(language)
        encoded = text.encode("utf-8")
        tree: Any = parser.parse(encoded)
        lines = text.splitlines()
        chunks: list[ExtractionChunk] = []
        pending: list[Any] = [tree.root_node]
        while pending and len(chunks) < self.max_symbols_per_file:
            node = pending.pop()
            pending.extend(reversed(list(node.named_children)))
            if str(node.type) not in _SYMBOL_NODE_TYPES:
                continue
            name_node = node.child_by_field_name("name")
            if name_node is None:
                continue
            symbol = encoded[name_node.start_byte : name_node.end_byte].decode(
                "utf-8",
                errors="replace",
            )
            start_line = int(node.start_point.row) + 1
            end_line = int(node.end_point.row) + 1
            content, truncated = self._bounded_text(
                "\n".join(lines[start_line - 1 : end_line]).strip()
            )
            if not symbol or not content:
                continue
            anchor: LineAnchor = {
                "kind": "line",
                "path": path.name,
                "line_start": start_line,
                "line_end": end_line,
                "symbol": symbol,
            }
            chunks.append(
                make_chunk(
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                    ordinal=len(chunks),
                    text=content,
                    extractor=self.name,
                    extractor_version=self.version,
                    anchor=anchor,
                    title=symbol,
                    metadata={
                        "symbol_kind": str(node.type),
                        "parser": "tree_sitter",
                        "language": language,
                        "truncated": truncated,
                    },
                )
            )
        return chunks

    def _fallback_chunks(
        self,
        path: Path,
        text: str,
        *,
        representation_id: str,
        representation_hash: str,
    ) -> list[ExtractionChunk]:
        lines = text.splitlines()
        chunks: list[ExtractionChunk] = []
        for offset in range(0, len(lines), self.fallback_lines):
            if len(chunks) >= self.max_fallback_chunks_per_file:
                break
            end = min(len(lines), offset + self.fallback_lines)
            content, truncated = self._bounded_text(
                "\n".join(lines[offset:end]).strip()
            )
            if not content:
                continue
            anchor: LineAnchor = {
                "kind": "line",
                "path": path.as_posix(),
                "line_start": offset + 1,
                "line_end": end,
            }
            chunks.append(
                make_chunk(
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                    ordinal=len(chunks),
                    text=content,
                    extractor=self.name,
                    extractor_version=self.version,
                    anchor=anchor,
                    title=path.name,
                    metadata={"truncated": truncated},
                )
            )
        return chunks

    def extract(self, path: Path, *, representation_id: str) -> ExtractionResult:
        raw = path.read_bytes()
        representation_hash = hash_bytes(raw)
        text, encoding = read_text(path)
        warnings: list[str] = []
        chunks: list[ExtractionChunk] = []
        parser = "plain_text"
        is_python = path.suffix.casefold() in {".py", ".pyi"}
        if is_python:
            try:
                chunks = self._python_symbols(
                    path,
                    text,
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                )
                parser = "python_ast"
            except SyntaxError as exc:
                warnings.append(f"Python AST parse failed: {exc}")
        elif (
            self.enable_tree_sitter
            and path.suffix.casefold() in _TREE_SITTER_SAFE_SUFFIXES
        ):
            try:
                chunks = self._tree_sitter_symbols(
                    path,
                    text,
                    representation_id=representation_id,
                    representation_hash=representation_hash,
                )
                if chunks:
                    parser = "tree_sitter"
            except Exception as exc:
                warnings.append(f"Tree-sitter parse failed: {exc}")
        if not chunks:
            chunks = self._fallback_chunks(
                path,
                text,
                representation_id=representation_id,
                representation_hash=representation_hash,
            )
        return ExtractionResult(
            representation_id=representation_id,
            source_path=str(path),
            representation_hash=representation_hash,
            extractor=self.name,
            extractor_version=self.version,
            status=(
                ExtractionStatus.SUCCEEDED
                if chunks and not warnings
                else ExtractionStatus.PARTIAL
                if chunks
                else ExtractionStatus.FAILED
            ),
            chunks=tuple(chunks),
            warnings=tuple(warnings),
            metadata={
                "encoding": encoding,
                "language": path.suffix.casefold().lstrip("."),
                "parser": parser,
                "tree_sitter_enabled": self.enable_tree_sitter,
                "symbol_count": sum(
                    1 for chunk in chunks if "symbol_kind" in chunk.metadata
                ),
            },
        )
