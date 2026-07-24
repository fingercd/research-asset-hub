"""Read-only compatibility and isolation audit for registered HTML assets.

The auditor deliberately resolves files through the same source/root/path
contract as the asset gateway.  Callers cannot provide an arbitrary local
path, and reports never contain resolved absolute filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import Protocol, cast
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from uuid import UUID

import httpx
from sqlalchemy import select

from apps.api.config import PROJECT_ROOT
from apps.api.database import DatabaseManager
from apps.asset_gateway.registry import SourceRegistry, UnsafeAssetPath
from packages.domain.models import Representation, RepresentationMount, Source

_HTML_TYPES = frozenset({"html", "html_document", "guide", "analysis"})
_HTML_SUFFIXES = frozenset({".html", ".htm", ".xhtml"})
_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})
_INLINE_SCHEMES = frozenset({"data", "blob"})
_NAVIGATION_SCHEMES = frozenset({"mailto", "tel"})
_VIRTUAL_AUDIT_PREFIX = "__rah__/"
_LOCAL_SCRIPT_SUFFIXES = frozenset({".js", ".mjs", ".cjs"})
_ACTIVE_LINK_RELS = frozenset(
    {
        "stylesheet",
        "preload",
        "prefetch",
        "modulepreload",
        "icon",
        "manifest",
    }
)
_CSS_REFERENCE = re.compile(
    r"""(?ix)
    @import\s+(?:url\(\s*)?["']?(?P<import>[^"')\s;]+)
    |
    url\(\s*["']?(?P<url>[^"')]+)["']?\s*\)
    """
)
_REMOTE_URL = re.compile(r"""(?i)(?:https?|wss?)://[^\s"'<>`\\)]+""")
_NETWORK_JS = re.compile(
    r"""(?ix)
    \bfetch\s*\(
    |\bXMLHttpRequest\b
    |\baxios\s*\.
    |\bWebSocket\s*\(
    |\bEventSource\s*\(
    |\bnavigator\.sendBeacon\s*\(
    |\bimport\s*\(
    |\bserviceWorker\.register\s*\(
    """
)
_META_REFRESH_URL = re.compile(r"""(?i)(?:^|;)\s*url\s*=\s*["']?([^"']+)""")
_NODE_AUDIT_SOURCE = r"""
const { chromium } = require("@playwright/test");

function safeUrl(value) {
  try {
    const parsed = new URL(value);
    parsed.search = "";
    parsed.hash = "";
    return parsed.toString();
  } catch (_) {
    return "<invalid-url>";
  }
}

(async () => {
  const targets = JSON.parse(process.env.RAH_HTML_AUDIT_TARGETS || "[]");
  const timeoutMs = Number(process.env.RAH_HTML_AUDIT_TIMEOUT_MS || "12000");
  const settleMs = Number(process.env.RAH_HTML_AUDIT_SETTLE_MS || "750");
  const browser = await chromium.launch({ headless: true });
  const results = [];
  try {
    for (const target of targets) {
      const context = await browser.newContext({ serviceWorkers: "block" });
      const page = await context.newPage();
      const consoleErrors = [];
      const pageErrors = [];
      const failedResources = [];
      const externalRequests = [];
      const gatewayOrigin = new URL(target.url).origin;

      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push(String(message.text()).slice(0, 1000));
        }
      });
      page.on("pageerror", (error) => {
        pageErrors.push(String(error && error.message ? error.message : error).slice(0, 1000));
      });
      page.on("request", (request) => {
        try {
          const parsed = new URL(request.url());
          if ((parsed.protocol === "http:" || parsed.protocol === "https:") &&
              parsed.origin !== gatewayOrigin) {
            externalRequests.push({
              url: safeUrl(request.url()),
              method: request.method(),
              resource_type: request.resourceType(),
            });
          }
        } catch (_) {}
      });
      page.on("requestfailed", (request) => {
        const failure = request.failure();
        failedResources.push({
          url: safeUrl(request.url()),
          method: request.method(),
          resource_type: request.resourceType(),
          error: failure ? String(failure.errorText).slice(0, 500) : "request failed",
        });
      });
      page.on("response", (response) => {
        if (response.status() >= 400) {
          const request = response.request();
          failedResources.push({
            url: safeUrl(response.url()),
            method: request.method(),
            resource_type: request.resourceType(),
            error: `HTTP ${response.status()}`,
          });
        }
      });

      let status = "completed";
      let detail = null;
      let policy = null;
      let csp = null;
      let finalUrl = null;
      try {
        const response = await page.goto(target.url, {
          waitUntil: "domcontentloaded",
          timeout: timeoutMs,
        });
        await page.waitForTimeout(settleMs);
        finalUrl = safeUrl(page.url());
        if (response) {
          const headers = response.headers();
          policy = headers["x-research-hub-html-policy"] || null;
          csp = headers["content-security-policy"] || null;
          if (response.status() >= 400) {
            status = "navigation_error";
            detail = `HTTP ${response.status()}`;
          }
        } else {
          status = "navigation_error";
          detail = "navigation returned no response";
        }
      } catch (error) {
        status = "navigation_error";
        detail = String(error && error.message ? error.message : error).slice(0, 1000);
      }
      results.push({
        representation_id: target.representation_id,
        status,
        detail,
        final_url: finalUrl,
        policy,
        csp,
        console_errors: consoleErrors,
        page_errors: pageErrors,
        failed_resources: failedResources,
        external_requests: externalRequests,
      });
      await context.close();
    }
  } finally {
    await browser.close();
  }
  process.stdout.write(JSON.stringify(results));
})().catch((error) => {
  process.stderr.write(String(error && error.message ? error.message : error));
  process.exit(2);
});
"""


def _now() -> datetime:
    return datetime.now(UTC)


def _clean_reference(value: str) -> str:
    """Remove credentials, query strings, and fragments from report values."""

    stripped = value.strip()
    if not stripped:
        return stripped
    if stripped.startswith("//"):
        stripped = "https:" + stripped
    parsed = urlsplit(stripped)
    if parsed.scheme.casefold() in _INLINE_SCHEMES:
        return f"{parsed.scheme.casefold()}:"
    if parsed.scheme.casefold() in _NETWORK_SCHEMES:
        hostname = parsed.hostname or ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{hostname}{port}"
        return urlunsplit((parsed.scheme.casefold(), netloc, parsed.path, "", ""))
    return urlunsplit(("", "", parsed.path, "", ""))


def _policy_from_registration(representation: Representation) -> str:
    configured = str((representation.attributes or {}).get("sandbox_policy") or "")
    if configured in {"static", "trusted_local", "network_required"}:
        return configured
    return "network_required" if representation.network_required else "static"


@dataclass(frozen=True, slots=True)
class _Mount:
    mount_path: str
    source_alias: str
    root_key: str
    path_ref: str
    priority: int


@dataclass(frozen=True, slots=True)
class _Registration:
    representation_id: UUID
    asset_id: UUID
    representation_type: str
    materialization_kind: str
    availability: str
    source_alias: str | None
    root_key: str
    path_ref: str | None
    entrypoint: str | None
    declared_network_required: bool
    declared_policy: str
    mounts: tuple[_Mount, ...]

    def public_location(self) -> dict[str, object]:
        return {
            "source_alias": self.source_alias,
            "root_key": self.root_key,
            "path_ref": self.path_ref,
            "entrypoint": self.entrypoint,
        }


@dataclass(frozen=True, slots=True)
class _RawReference:
    context: str
    value: str
    active: bool
    line: int


@dataclass(frozen=True, slots=True)
class DependencyFinding:
    context: str
    reference: str
    classification: str
    active: bool
    line: int
    resolution: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "context": self.context,
            "reference": self.reference,
            "classification": self.classification,
            "active": self.active,
            "line": self.line,
            "resolution": self.resolution,
        }


@dataclass(frozen=True, slots=True)
class DynamicPageCheck:
    representation_id: UUID
    status: str
    detail: str | None = None
    final_url: str | None = None
    policy: str | None = None
    csp: str | None = None
    console_errors: tuple[str, ...] = ()
    page_errors: tuple[str, ...] = ()
    failed_resources: tuple[Mapping[str, str], ...] = ()
    external_requests: tuple[Mapping[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "detail": self.detail,
            "final_url": self.final_url,
            "policy": self.policy,
            "csp": self.csp,
            "console_errors": list(self.console_errors),
            "page_errors": list(self.page_errors),
            "failed_resources": [dict(item) for item in self.failed_resources],
            "external_requests": [dict(item) for item in self.external_requests],
        }


@dataclass(frozen=True, slots=True)
class DynamicProbeBatch:
    status: str
    reason: str | None
    checks: Mapping[UUID, DynamicPageCheck]


class DynamicHtmlProbe(Protocol):
    def probe(
        self,
        representation_ids: Sequence[UUID],
        *,
        api_origin: str,
        timeout_seconds: float,
    ) -> DynamicProbeBatch:
        """Open signed gateway sessions and return browser observations."""


@dataclass(slots=True)
class HtmlPageAudit:
    registration: _Registration
    static_status: str
    detail: str | None = None
    content_sha256: str | None = None
    size_bytes: int | None = None
    inferred_policy: str | None = None
    dependencies: list[DependencyFinding] = field(default_factory=list)
    inline_script_count: int = 0
    inline_event_handler_count: int = 0
    local_script_count: int = 0
    dynamic_network_signal_count: int = 0
    risk_flags: list[str] = field(default_factory=list)
    dynamic: DynamicPageCheck | None = None

    def to_dict(self) -> dict[str, object]:
        dependency_counts = Counter(item.classification for item in self.dependencies)
        return {
            "representation_id": str(self.registration.representation_id),
            "asset_id": str(self.registration.asset_id),
            "representation_type": self.registration.representation_type,
            "availability": self.registration.availability,
            "registered_location": self.registration.public_location(),
            "static": {
                "status": self.static_status,
                "detail": self.detail,
                "content_sha256": self.content_sha256,
                "size_bytes": self.size_bytes,
                "declared_policy": self.registration.declared_policy,
                "declared_network_required": (
                    self.registration.declared_network_required
                ),
                "inferred_policy": self.inferred_policy,
                "policy_mismatch": (
                    self.inferred_policy is not None
                    and self.inferred_policy != self.registration.declared_policy
                ),
                "dependency_counts": dict(sorted(dependency_counts.items())),
                "dependencies": [item.to_dict() for item in self.dependencies],
                "inline_script_count": self.inline_script_count,
                "inline_event_handler_count": self.inline_event_handler_count,
                "local_script_count": self.local_script_count,
                "dynamic_network_signal_count": self.dynamic_network_signal_count,
                "risk_flags": self.risk_flags,
            },
            "dynamic": (
                self.dynamic.to_dict()
                if self.dynamic is not None
                else {
                    "status": "not_selected",
                    "detail": "Not selected for the optional browser sample.",
                }
            ),
        }


@dataclass(slots=True)
class HtmlAuditReport:
    started_at: datetime
    finished_at: datetime | None = None
    pages: list[HtmlPageAudit] = field(default_factory=list)
    selection_limit: int | None = None
    dynamic_sample_size: int = 0
    dynamic_probe_status: str = "not_requested"
    dynamic_probe_reason: str | None = None

    def to_dict(self) -> dict[str, object]:
        static_statuses = Counter(page.static_status for page in self.pages)
        inferred_policies = Counter(
            page.inferred_policy for page in self.pages if page.inferred_policy
        )
        policy_mismatches = sum(
            1
            for page in self.pages
            if page.inferred_policy is not None
            and page.inferred_policy != page.registration.declared_policy
        )
        external_active = sum(
            1
            for page in self.pages
            for dependency in page.dependencies
            if dependency.classification == "external_network" and dependency.active
        )
        missing_local = sum(
            1
            for page in self.pages
            for dependency in page.dependencies
            if dependency.classification == "local"
            and dependency.resolution == "missing"
        )
        dynamic_checks = [page.dynamic for page in self.pages if page.dynamic is not None]
        completed_checks = sum(
            check is not None and check.status == "completed" for check in dynamic_checks
        )
        unverified: list[str] = []
        if any(status != "audited" for status in static_statuses):
            unverified.append("Some registered HTML entries could not be statically read.")
        if self.dynamic_sample_size == 0:
            unverified.append("Playwright signed-gateway sampling was not requested.")
        elif self.dynamic_probe_status != "completed":
            unverified.append(
                "Playwright signed-gateway sampling was unavailable or incomplete."
            )
        return {
            "schema": "research-asset-hub.html-audit/v1",
            "status": (
                "completed_with_unverified_items"
                if unverified
                else "completed"
            ),
            "read_only": True,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "selection": {
                "registered_html_candidates": len(self.pages),
                "limit": self.selection_limit,
                "types": sorted(_HTML_TYPES),
                "suffixes": sorted(_HTML_SUFFIXES),
            },
            "summary": {
                "static_statuses": dict(sorted(static_statuses.items())),
                "inferred_policies": dict(sorted(inferred_policies.items())),
                "policy_mismatches": policy_mismatches,
                "active_external_dependencies": external_active,
                "missing_local_dependencies": missing_local,
                "dynamic_requested": self.dynamic_sample_size,
                "dynamic_completed": completed_checks,
                "dynamic_probe_status": self.dynamic_probe_status,
                "dynamic_probe_reason": self.dynamic_probe_reason,
            },
            "unverified": unverified,
            "representations": [page.to_dict() for page in self.pages],
        }


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[_RawReference] = []
        self.base_href: str | None = None
        self.inline_scripts: list[str] = []
        self.inline_styles: list[str] = []
        self.event_handlers = 0
        self._script_parts: list[str] | None = None
        self._style_parts: list[str] | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized_tag = tag.casefold()
        values = {name.casefold(): value or "" for name, value in attrs}
        line, _ = self.getpos()
        self.event_handlers += sum(name.startswith("on") for name in values)
        style = values.get("style")
        if style:
            self.inline_styles.append(style)
        if normalized_tag == "base" and values.get("href") and self.base_href is None:
            self.base_href = values["href"]
        if normalized_tag == "script":
            source = values.get("src")
            if source:
                self.references.append(_RawReference("script.src", source, True, line))
            else:
                self._script_parts = []
        elif normalized_tag == "link" and values.get("href"):
            rels = frozenset(values.get("rel", "").casefold().split())
            active = bool(rels & _ACTIVE_LINK_RELS)
            self.references.append(
                _RawReference(
                    f"link[{','.join(sorted(rels)) or 'unspecified'}].href",
                    values["href"],
                    active,
                    line,
                )
            )
        elif normalized_tag in {"img", "source"}:
            if values.get("src"):
                self.references.append(
                    _RawReference(f"{normalized_tag}.src", values["src"], True, line)
                )
            if values.get("srcset"):
                for item in _split_srcset(values["srcset"]):
                    self.references.append(
                        _RawReference(
                            f"{normalized_tag}.srcset",
                            item,
                            True,
                            line,
                        )
                    )
        elif normalized_tag in {"audio", "video", "iframe", "embed", "track", "input"}:
            if values.get("src"):
                self.references.append(
                    _RawReference(f"{normalized_tag}.src", values["src"], True, line)
                )
            if normalized_tag == "video" and values.get("poster"):
                self.references.append(
                    _RawReference("video.poster", values["poster"], True, line)
                )
        elif normalized_tag == "object" and values.get("data"):
            self.references.append(
                _RawReference("object.data", values["data"], True, line)
            )
        elif normalized_tag in {"a", "area"} and values.get("href"):
            self.references.append(
                _RawReference(f"{normalized_tag}.href", values["href"], False, line)
            )
        elif normalized_tag == "form" and values.get("action"):
            self.references.append(
                _RawReference("form.action", values["action"], False, line)
            )
        elif (
            normalized_tag == "meta"
            and values.get("http-equiv", "").casefold() == "refresh"
        ):
            match = _META_REFRESH_URL.search(values.get("content", ""))
            if match:
                self.references.append(
                    _RawReference("meta.refresh", match.group(1), True, line)
                )
        if normalized_tag == "style":
            self._style_parts = []

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized == "script" and self._script_parts is not None:
            self.inline_scripts.append("".join(self._script_parts))
            self._script_parts = None
        elif normalized == "style" and self._style_parts is not None:
            self.inline_styles.append("".join(self._style_parts))
            self._style_parts = None

    def handle_data(self, data: str) -> None:
        if self._script_parts is not None:
            self._script_parts.append(data)
        if self._style_parts is not None:
            self._style_parts.append(data)


def _split_srcset(value: str) -> list[str]:
    if value.lstrip().casefold().startswith("data:"):
        return [value.split()[0]]
    return [
        item.strip().split()[0]
        for item in value.split(",")
        if item.strip()
    ]


def _css_references(css: str, *, context: str) -> list[_RawReference]:
    references: list[_RawReference] = []
    for match in _CSS_REFERENCE.finditer(css):
        value = match.group("import") or match.group("url")
        if value:
            line = css.count("\n", 0, match.start()) + 1
            references.append(_RawReference(context, value.strip(), True, line))
    return references


def _join_registered_ref(base: str, remainder: str) -> str:
    combined = PurePosixPath(base.replace("\\", "/"))
    if remainder:
        combined = combined.joinpath(PurePosixPath(remainder.replace("\\", "/")))
    if combined.is_absolute() or ".." in combined.parts:
        raise UnsafeAssetPath("Registered dependency contains traversal.")
    return combined.as_posix()


def _mount_match(mount_path: str, request_path: str) -> str | None:
    prefix = mount_path.replace("\\", "/").strip("/")
    target = request_path.replace("\\", "/").strip("/")
    if not prefix:
        return target
    if target == prefix:
        return ""
    if target.startswith(prefix + "/"):
        return target[len(prefix) + 1 :]
    return None


class _RegisteredResolver:
    def __init__(self, registry: SourceRegistry) -> None:
        self.registry = registry

    def resolve_request(
        self,
        registration: _Registration,
        request_path: str,
    ) -> Path:
        decoded = unquote(request_path).replace("\\", "/")
        candidate_ref = PurePosixPath(decoded)
        if candidate_ref.is_absolute() or ".." in candidate_ref.parts:
            raise UnsafeAssetPath("Dependency path is absolute or contains traversal.")
        for mount in registration.mounts:
            remainder = _mount_match(mount.mount_path, decoded)
            if remainder is None:
                continue
            mount_ref = PurePosixPath(mount.path_ref.replace("\\", "/"))
            if registration.materialization_kind == "file" and (
                not remainder or remainder == mount_ref.as_posix()
            ):
                registered_ref = mount_ref.as_posix()
            elif registration.materialization_kind == "file":
                registered_ref = _join_registered_ref(".", remainder)
            else:
                registered_ref = _join_registered_ref(
                    mount_ref.as_posix(),
                    remainder,
                )
            candidate = self.registry.resolve(
                mount.source_alias,
                mount.root_key,
                registered_ref,
            )
            if candidate.is_dir() and (
                registration.entrypoint
                or registration.representation_type in _HTML_TYPES
            ):
                child = (
                    registration.entrypoint
                    if not remainder and registration.entrypoint
                    else "index.html"
                )
                index_ref = _join_registered_ref(registered_ref, child)
                index_candidate = self.registry.resolve(
                    mount.source_alias,
                    mount.root_key,
                    index_ref,
                )
                if index_candidate.is_file():
                    candidate = index_candidate
            return candidate
        if registration.source_alias is None or registration.path_ref is None:
            raise FileNotFoundError("Representation has no registered local source.")
        configured = PurePosixPath(registration.path_ref.replace("\\", "/"))
        is_directory = registration.materialization_kind in {"directory", "bundle"}
        if is_directory:
            if decoded:
                remainder = decoded
            elif registration.entrypoint:
                remainder = registration.entrypoint
            elif registration.representation_type in _HTML_TYPES:
                remainder = "index.html"
            else:
                remainder = ""
            registered_ref = _join_registered_ref(configured.as_posix(), remainder)
        else:
            if decoded:
                registered_ref = (
                    configured.as_posix()
                    if decoded == configured.as_posix()
                    else _join_registered_ref(".", decoded)
                )
            else:
                registered_ref = configured.as_posix()
        candidate = self.registry.resolve(
            registration.source_alias,
            registration.root_key,
            registered_ref,
        )
        if candidate.is_dir() and (
            registration.entrypoint
            or registration.representation_type in _HTML_TYPES
        ):
            index_ref = _join_registered_ref(registered_ref, "index.html")
            index_candidate = self.registry.resolve(
                registration.source_alias,
                registration.root_key,
                index_ref,
            )
            if index_candidate.is_file():
                candidate = index_candidate
        return candidate


def _classify_reference(
    raw: _RawReference,
    *,
    base_href: str | None,
    registration: _Registration,
    resolver: _RegisteredResolver,
) -> tuple[DependencyFinding, Path | None]:
    value = raw.value.strip()
    if not value:
        return (
            DependencyFinding(
                raw.context,
                "",
                "empty",
                raw.active,
                raw.line,
            ),
            None,
        )
    effective = urljoin(base_href or "", value)
    if effective.startswith("//"):
        effective = "https:" + effective
    parsed = urlsplit(effective)
    scheme = parsed.scheme.casefold()
    if scheme in _NETWORK_SCHEMES:
        classification = "external_network" if raw.active else "external_navigation"
        return (
            DependencyFinding(
                raw.context,
                _clean_reference(effective),
                classification,
                raw.active,
                raw.line,
            ),
            None,
        )
    if scheme in _INLINE_SCHEMES or effective.startswith("#"):
        return (
            DependencyFinding(
                raw.context,
                _clean_reference(effective),
                "inline_or_fragment",
                raw.active,
                raw.line,
            ),
            None,
        )
    if scheme in _NAVIGATION_SCHEMES:
        return (
            DependencyFinding(
                raw.context,
                f"{scheme}:",
                "external_navigation",
                raw.active,
                raw.line,
            ),
            None,
        )
    if scheme:
        return (
            DependencyFinding(
                raw.context,
                f"{scheme}:",
                "unsafe_or_unsupported_scheme",
                raw.active,
                raw.line,
                "rejected",
            ),
            None,
        )
    if parsed.path.startswith("/"):
        return (
            DependencyFinding(
                raw.context,
                _clean_reference(effective),
                "origin_absolute",
                raw.active,
                raw.line,
                "outside_signed_representation_route",
            ),
            None,
        )
    request_path = unquote(parsed.path)
    if base_href and base_href.startswith(_VIRTUAL_AUDIT_PREFIX):
        if not request_path.startswith(_VIRTUAL_AUDIT_PREFIX):
            return (
                DependencyFinding(
                    raw.context,
                    _clean_reference(value),
                    "unsafe_local",
                    raw.active,
                    raw.line,
                    "escaped_signed_representation_route",
                ),
                None,
            )
        request_path = request_path[len(_VIRTUAL_AUDIT_PREFIX) :]
    try:
        path = resolver.resolve_request(registration, request_path)
    except (UnsafeAssetPath, OSError, ValueError):
        return (
            DependencyFinding(
                raw.context,
                _clean_reference(effective),
                "unsafe_local",
                raw.active,
                raw.line,
                "rejected",
            ),
            None,
        )
    resolution = "available" if path.is_file() else "missing"
    return (
        DependencyFinding(
            raw.context,
            _clean_reference(effective),
            "local",
            raw.active,
            raw.line,
            resolution,
        ),
        path if path.is_file() else None,
    )


def _read_limited(path: Path, maximum_bytes: int) -> tuple[bytes, bool]:
    with path.open("rb") as handle:
        content = handle.read(maximum_bytes + 1)
    return content[:maximum_bytes], len(content) > maximum_bytes


def _audit_page(
    registration: _Registration,
    resolver: _RegisteredResolver,
    *,
    maximum_html_bytes: int,
    maximum_css_bytes: int,
    maximum_css_files: int,
) -> HtmlPageAudit:
    page = HtmlPageAudit(registration=registration, static_status="unresolved")
    try:
        entry = resolver.resolve_request(registration, "")
    except (FileNotFoundError, UnsafeAssetPath, OSError, ValueError):
        page.detail = "The registered entrypoint could not be safely resolved."
        page.risk_flags.append("entrypoint_unresolved")
        return page
    if not entry.is_file():
        page.detail = "The registered entrypoint is missing or is not a file."
        page.risk_flags.append("entrypoint_missing")
        return page
    if entry.suffix.casefold() not in _HTML_SUFFIXES:
        page.static_status = "not_html"
        page.detail = "The registered representation does not resolve to an HTML suffix."
        return page
    try:
        content, truncated = _read_limited(entry, maximum_html_bytes)
    except OSError:
        page.detail = "The registered HTML file could not be read."
        page.risk_flags.append("entrypoint_read_failed")
        return page
    page.size_bytes = entry.stat().st_size
    page.content_sha256 = hashlib.sha256(content).hexdigest()
    if truncated:
        page.static_status = "truncated"
        page.detail = f"Static parsing was capped at {maximum_html_bytes} bytes."
        page.risk_flags.append("html_size_limit")
    else:
        page.static_status = "audited"
    parser = _DocumentParser()
    parser.feed(content.decode("utf-8", errors="replace"))
    parser.close()
    page.inline_script_count = len(parser.inline_scripts)
    page.inline_event_handler_count = parser.event_handlers
    if registration.materialization_kind == "file":
        registered_document_path = registration.path_ref or ""
    elif registration.path_ref:
        entrypoint = registration.entrypoint or "index.html"
        registered_document_path = _join_registered_ref(
            registration.path_ref,
            entrypoint,
        )
    else:
        registered_document_path = ""
    virtual_document_path = (
        f"{_VIRTUAL_AUDIT_PREFIX}{registered_document_path}"
        if registered_document_path
        else _VIRTUAL_AUDIT_PREFIX
    )
    document_base = (
        urljoin(virtual_document_path, parser.base_href)
        if parser.base_href
        else virtual_document_path
    )

    raw_references = list(parser.references)
    for css in parser.inline_styles:
        raw_references.extend(_css_references(css, context="inline-style.url"))

    css_queue: list[Path] = []
    seen_css: set[Path] = set()
    for raw in raw_references:
        finding, resolved = _classify_reference(
            raw,
            base_href=document_base,
            registration=registration,
            resolver=resolver,
        )
        page.dependencies.append(finding)
        if (
            resolved is not None
            and resolved.suffix.casefold() == ".css"
            and len(css_queue) < maximum_css_files
        ):
            css_queue.append(resolved)
        if (
            finding.context == "script.src"
            and finding.classification == "local"
        ):
            page.local_script_count += 1

    while css_queue and len(seen_css) < maximum_css_files:
        css_path = css_queue.pop(0)
        resolved_key = css_path.resolve(strict=False)
        if resolved_key in seen_css:
            continue
        seen_css.add(resolved_key)
        try:
            css_content, css_truncated = _read_limited(css_path, maximum_css_bytes)
        except OSError:
            page.risk_flags.append("stylesheet_read_failed")
            continue
        if css_truncated:
            page.risk_flags.append("stylesheet_size_limit")
        css_text = css_content.decode("utf-8", errors="replace")
        for raw in _css_references(css_text, context="stylesheet.url"):
            finding, nested = _classify_reference(
                raw,
                base_href=document_base,
                registration=registration,
                resolver=resolver,
            )
            page.dependencies.append(finding)
            if (
                nested is not None
                and nested.suffix.casefold() == ".css"
                and len(seen_css) + len(css_queue) < maximum_css_files
            ):
                css_queue.append(nested)

    scripts = "\n".join(parser.inline_scripts)
    page.dynamic_network_signal_count = len(_NETWORK_JS.findall(scripts))
    for url in _REMOTE_URL.findall(scripts):
        page.dependencies.append(
            DependencyFinding(
                "inline-script.remote-url",
                _clean_reference(url),
                "runtime_network_candidate",
                True,
                0,
            )
        )
    has_external_active = any(
        dependency.active
        and dependency.classification
        in {"external_network", "runtime_network_candidate"}
        for dependency in page.dependencies
    )
    has_script = bool(
        page.inline_script_count
        or page.inline_event_handler_count
        or any(
            dependency.context == "script.src"
            for dependency in page.dependencies
        )
    )
    if has_external_active or page.dynamic_network_signal_count:
        page.inferred_policy = "network_required"
    elif has_script:
        page.inferred_policy = "trusted_local"
    else:
        page.inferred_policy = "static"

    if any(
        item.classification == "unsafe_local"
        or item.classification == "unsafe_or_unsupported_scheme"
        for item in page.dependencies
    ):
        page.risk_flags.append("unsafe_dependency_rejected")
    if any(
        item.classification == "origin_absolute" for item in page.dependencies
    ):
        page.risk_flags.append("origin_absolute_dependency_unmapped")
    if any(
        item.classification == "local" and item.resolution == "missing"
        for item in page.dependencies
    ):
        page.risk_flags.append("missing_local_dependency")
    if page.inferred_policy != registration.declared_policy:
        page.risk_flags.append("declared_policy_mismatch")
    page.risk_flags = sorted(set(page.risk_flags))
    return page


def _registrations(
    database: DatabaseManager,
    *,
    limit: int | None,
) -> list[_Registration]:
    with database.session() as session:
        representations = list(
            session.scalars(
                select(Representation)
                .where(
                    Representation.representation_type.in_(_HTML_TYPES),
                    Representation.deleted_at.is_(None),
                )
                .order_by(Representation.id)
            ).all()
        )
        source_by_id = {
            source.id: source
            for source in session.scalars(select(Source)).all()
        }
        representation_ids = [item.id for item in representations]
        mounts_by_representation: dict[UUID, list[_Mount]] = {}
        if representation_ids:
            mounts = session.scalars(
                select(RepresentationMount).where(
                    RepresentationMount.representation_id.in_(representation_ids),
                    RepresentationMount.deleted_at.is_(None),
                )
            )
            for mount in mounts:
                source = source_by_id.get(mount.source_id)
                if source is None or not source.enabled:
                    continue
                mounts_by_representation.setdefault(
                    mount.representation_id,
                    [],
                ).append(
                    _Mount(
                        mount_path=mount.mount_path,
                        source_alias=source.alias,
                        root_key=mount.root_key,
                        path_ref=mount.path_ref,
                        priority=mount.priority,
                    )
                )
        results: list[_Registration] = []
        for representation in representations:
            path_ref = representation.path_ref or ""
            suffix = PurePosixPath(path_ref.replace("\\", "/")).suffix.casefold()
            mime_type = (representation.mime_type or "").split(";", 1)[0].casefold()
            if suffix not in _HTML_SUFFIXES and mime_type not in {
                "text/html",
                "application/xhtml+xml",
            }:
                continue
            source = (
                source_by_id.get(representation.source_id)
                if representation.source_id is not None
                else None
            )
            ordered_mounts = sorted(
                mounts_by_representation.get(representation.id, []),
                key=lambda item: (item.priority, len(item.mount_path)),
                reverse=True,
            )
            results.append(
                _Registration(
                    representation_id=representation.id,
                    asset_id=representation.asset_id,
                    representation_type=representation.representation_type,
                    materialization_kind=representation.materialization_kind,
                    availability=representation.availability,
                    source_alias=source.alias if source and source.enabled else None,
                    root_key=representation.root_key,
                    path_ref=representation.path_ref,
                    entrypoint=representation.entrypoint,
                    declared_network_required=representation.network_required,
                    declared_policy=_policy_from_registration(representation),
                    mounts=tuple(ordered_mounts),
                )
            )
        return results if limit is None else results[: max(0, limit)]


def _loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Dynamic audit API origin must be a plain loopback HTTP origin.")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


class NodePlaywrightGatewayProbe:
    """Optional browser probe using the project's installed Playwright runtime."""

    def __init__(
        self,
        *,
        web_project: Path | None = None,
        node_executable: str | None = None,
        settle_milliseconds: int = 750,
    ) -> None:
        self.web_project = web_project or PROJECT_ROOT / "apps" / "web"
        self.node_executable = node_executable or shutil.which("node")
        self.settle_milliseconds = settle_milliseconds

    def probe(
        self,
        representation_ids: Sequence[UUID],
        *,
        api_origin: str,
        timeout_seconds: float,
    ) -> DynamicProbeBatch:
        origin = _loopback_origin(api_origin)
        if not representation_ids:
            return DynamicProbeBatch("completed", None, {})
        if self.node_executable is None:
            return DynamicProbeBatch(
                "unavailable",
                "Node.js was not found; Playwright checks were not run.",
                {},
            )
        package_path = (
            self.web_project / "node_modules" / "@playwright" / "test"
        )
        if not package_path.exists():
            return DynamicProbeBatch(
                "unavailable",
                "The web project's Playwright package is not installed.",
                {},
            )

        targets: list[dict[str, str]] = []
        checks: dict[UUID, DynamicPageCheck] = {}
        try:
            with httpx.Client(
                base_url=origin,
                timeout=max(1.0, min(timeout_seconds, 30.0)),
                follow_redirects=False,
            ) as client:
                health = client.get("/api/health")
                health.raise_for_status()
                for representation_id in representation_ids:
                    response = client.post(
                        f"/api/v1/representations/{representation_id}/open-actions"
                    )
                    if response.status_code >= 400:
                        checks[representation_id] = DynamicPageCheck(
                            representation_id,
                            "session_error",
                            f"Open-action endpoint returned HTTP {response.status_code}.",
                        )
                        continue
                    payload = response.json()
                    if (
                        not isinstance(payload, dict)
                        or payload.get("kind") != "gateway_session"
                        or not isinstance(payload.get("url"), str)
                    ):
                        checks[representation_id] = DynamicPageCheck(
                            representation_id,
                            "session_error",
                            "Open-action endpoint did not return a signed gateway session.",
                        )
                        continue
                    targets.append(
                        {
                            "representation_id": str(representation_id),
                            "url": cast(str, payload["url"]),
                        }
                    )
        except (httpx.HTTPError, OSError, ValueError):
            return DynamicProbeBatch(
                "unavailable",
                "The loopback API was unavailable; signed-gateway checks were not run.",
                {},
            )

        if not targets:
            return DynamicProbeBatch(
                "partial",
                "No signed gateway sessions could be acquired.",
                checks,
            )
        environment = dict(__import__("os").environ)
        environment["RAH_HTML_AUDIT_TARGETS"] = json.dumps(
            targets,
            separators=(",", ":"),
        )
        environment["RAH_HTML_AUDIT_TIMEOUT_MS"] = str(
            max(1000, round(timeout_seconds * 1000))
        )
        environment["RAH_HTML_AUDIT_SETTLE_MS"] = str(
            max(0, self.settle_milliseconds)
        )
        try:
            completed = subprocess.run(
                [self.node_executable, "-e", _NODE_AUDIT_SOURCE],
                cwd=self.web_project,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(
                    30.0,
                    (timeout_seconds + self.settle_milliseconds / 1000 + 2)
                    * len(targets),
                ),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return DynamicProbeBatch(
                "unavailable",
                "Playwright could not be launched or exceeded the batch timeout.",
                checks,
            )
        if completed.returncode != 0:
            return DynamicProbeBatch(
                "unavailable",
                "Playwright execution failed; browser observations are unverified.",
                checks,
            )
        try:
            raw_results = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return DynamicProbeBatch(
                "unavailable",
                "Playwright returned an invalid result document.",
                checks,
            )
        if not isinstance(raw_results, list):
            return DynamicProbeBatch(
                "unavailable",
                "Playwright result was not a list.",
                checks,
            )
        for raw in raw_results:
            if not isinstance(raw, dict):
                continue
            try:
                representation_id = UUID(str(raw["representation_id"]))
            except (KeyError, ValueError):
                continue
            checks[representation_id] = _dynamic_check_from_json(
                representation_id,
                raw,
            )
        status = "completed" if len(checks) == len(representation_ids) else "partial"
        reason = (
            None
            if status == "completed"
            else "Some selected representations did not produce browser observations."
        )
        return DynamicProbeBatch(status, reason, checks)


def _mapping_rows(value: object) -> tuple[Mapping[str, str], ...]:
    if not isinstance(value, list):
        return ()
    rows: list[Mapping[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                str(key): str(cell)[:1000]
                for key, cell in item.items()
                if cell is not None
            }
        )
    return tuple(rows)


def _string_rows(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item)[:1000] for item in value)


def _optional_string(value: object) -> str | None:
    return str(value)[:4000] if value is not None else None


def _dynamic_check_from_json(
    representation_id: UUID,
    raw: Mapping[str, object],
) -> DynamicPageCheck:
    return DynamicPageCheck(
        representation_id=representation_id,
        status=str(raw.get("status") or "unknown"),
        detail=_optional_string(raw.get("detail")),
        final_url=_optional_string(raw.get("final_url")),
        policy=_optional_string(raw.get("policy")),
        csp=_optional_string(raw.get("csp")),
        console_errors=_string_rows(raw.get("console_errors")),
        page_errors=_string_rows(raw.get("page_errors")),
        failed_resources=_mapping_rows(raw.get("failed_resources")),
        external_requests=_mapping_rows(raw.get("external_requests")),
    )


def _dynamic_selection(
    pages: Sequence[HtmlPageAudit],
    sample_size: int,
) -> list[HtmlPageAudit]:
    risk_order = {
        "network_required": 0,
        "trusted_local": 1,
        "static": 2,
        None: 3,
    }
    return sorted(
        pages,
        key=lambda page: (
            risk_order.get(page.inferred_policy, 4),
            -len(page.risk_flags),
            str(page.registration.representation_id),
        ),
    )[:sample_size]


def audit_registered_html(
    database: DatabaseManager,
    registry: SourceRegistry,
    *,
    limit: int | None = None,
    dynamic_sample_size: int = 0,
    api_origin: str = "http://127.0.0.1:8767",
    dynamic_probe: DynamicHtmlProbe | None = None,
    timeout_seconds: float = 12.0,
    maximum_html_bytes: int = 10 * 1024 * 1024,
    maximum_css_bytes: int = 1024 * 1024,
    maximum_css_files: int = 32,
) -> HtmlAuditReport:
    """Audit every registered HTML entry without mutating DB or source files."""

    if limit is not None and limit < 0:
        raise ValueError("limit cannot be negative")
    if dynamic_sample_size < 0:
        raise ValueError("dynamic_sample_size cannot be negative")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if min(maximum_html_bytes, maximum_css_bytes, maximum_css_files) <= 0:
        raise ValueError("audit size limits must be positive")
    _loopback_origin(api_origin)

    report = HtmlAuditReport(
        started_at=_now(),
        selection_limit=limit,
        dynamic_sample_size=dynamic_sample_size,
    )
    resolver = _RegisteredResolver(registry)
    registrations = _registrations(database, limit=limit)
    report.pages = [
        _audit_page(
            registration,
            resolver,
            maximum_html_bytes=maximum_html_bytes,
            maximum_css_bytes=maximum_css_bytes,
            maximum_css_files=maximum_css_files,
        )
        for registration in registrations
    ]
    if dynamic_sample_size:
        selected = _dynamic_selection(report.pages, dynamic_sample_size)
        probe = dynamic_probe or NodePlaywrightGatewayProbe()
        batch = probe.probe(
            [page.registration.representation_id for page in selected],
            api_origin=api_origin,
            timeout_seconds=timeout_seconds,
        )
        report.dynamic_probe_status = batch.status
        report.dynamic_probe_reason = batch.reason
        for page in selected:
            page.dynamic = batch.checks.get(
                page.registration.representation_id,
                DynamicPageCheck(
                    page.registration.representation_id,
                    "unverified",
                    batch.reason
                    or "The browser probe produced no observation for this page.",
                ),
            )
    report.finished_at = _now()
    return report


__all__ = [
    "DependencyFinding",
    "DynamicHtmlProbe",
    "DynamicPageCheck",
    "DynamicProbeBatch",
    "HtmlAuditReport",
    "HtmlPageAudit",
    "NodePlaywrightGatewayProbe",
    "audit_registered_html",
]
