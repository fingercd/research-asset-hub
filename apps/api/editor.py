"""Local editor integration for registered code representations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.models import Representation

CODE_REPRESENTATION_TYPES = frozenset({"repository", "source_code", "notebook"})
_HUGGINGFACE_PREFIXES = frozenset({"datasets", "models"})
_CODE_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})


@dataclass(frozen=True, slots=True)
class EditorResolution:
    available: bool
    editor: str = "vscode"
    path: str | None = None
    uri: str | None = None
    reason: str | None = None


def repository_components(external_url: str | None) -> tuple[str, str] | None:
    """Extract ``(owner, repo)`` from a GitHub or Hugging Face URL."""

    if not external_url:
        return None
    try:
        parsed = urlparse(external_url)
    except ValueError:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    hostname = (parsed.hostname or "").casefold()
    if hostname == "huggingface.co":
        if parts and parts[0] in _HUGGINGFACE_PREFIXES:
            parts = parts[1:]
    elif hostname not in _CODE_HOSTS:
        return None
    parts = [part[:-4] if part.endswith(".git") else part for part in parts]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1]
    if not owner or not repo:
        return None
    return owner, repo


def repository_slug(external_url: str | None) -> str | None:
    components = repository_components(external_url)
    if components is None:
        return None
    return f"{components[0]}-{components[1]}"


def is_public_code_repository_url(external_url: str | None) -> bool:
    """Return whether a URL points to a public Git hosting code repository."""

    if not external_url:
        return False
    try:
        parsed = urlparse(external_url)
    except ValueError:
        return False
    return (parsed.hostname or "").casefold() in _CODE_HOSTS


def _matches_slug(path_name: str, slug: str) -> bool:
    normalized_name = path_name.replace("_", "").replace("-", "").casefold()
    normalized_slug = slug.replace("_", "").replace("-", "").casefold()
    return normalized_name == normalized_slug


def _candidate_repository_dirs(root: Path, owner: str, repo: str) -> list[Path]:
    slug = f"{owner}-{repo}"
    candidates = [
        root / slug,
        root / f"{owner}--{repo}",
        root / f"{owner}__{repo}",
    ]
    try:
        children = list(root.iterdir())
    except OSError:
        return candidates
    for child in sorted(children, key=lambda item: item.name.casefold()):
        if child.is_dir() and _matches_slug(child.name, slug):
            candidates.append(child)
    return candidates


def resolve_editor_path(
    registry: SourceRegistry,
    representation: Representation,
    *,
    source_alias: str | None = None,
) -> EditorResolution:
    """Resolve a registered code representation to a local editor target."""

    if representation.representation_type not in CODE_REPRESENTATION_TYPES:
        return EditorResolution(available=False, reason="该入口不是代码项目。")
    if (
        not representation.path_ref
        and representation.materialization_kind == "url"
        and not is_public_code_repository_url(representation.external_url)
    ):
        return EditorResolution(available=False, reason="源码未下载/未开源")
    source_id = source_alias or (
        str(representation.source_id) if representation.source_id is not None else None
    )
    if source_id is None:
        return EditorResolution(available=False, reason="源码未下载/未开源")
    root_key = representation.root_key or "repository"
    try:
        root = registry.root_for(source_id, root_key)
    except UnsafeAssetPath:
        return EditorResolution(available=False, reason="源码未下载/未开源")
    if not root.is_dir():
        return EditorResolution(available=False, reason="源码未下载/未开源")

    candidate: Path | None = None
    if representation.path_ref:
        try:
            resolved = registry.resolve(source_id, root_key, representation.path_ref)
        except UnsafeAssetPath:
            return EditorResolution(available=False, reason="源码路径无效")
        if resolved.is_dir():
            candidate = resolved
        elif resolved.is_file() and representation.materialization_kind == "file":
            parent = resolved.parent
            if parent.is_dir():
                candidate = parent
    else:
        components = repository_components(representation.external_url)
        if components is not None:
            owner, repo = components
            for candidate_path in _candidate_repository_dirs(root, owner, repo):
                if candidate_path.is_dir():
                    candidate = candidate_path
                    break

    if candidate is None or not candidate.is_dir():
        return EditorResolution(available=False, reason="源码未下载/未开源")
    try:
        candidate.relative_to(root)
    except ValueError:
        return EditorResolution(available=False, reason="源码路径无效")
    path_text = candidate.resolve().as_posix()
    return EditorResolution(
        available=True,
        path=path_text,
        uri=f"vscode://file/{path_text}",
    )
