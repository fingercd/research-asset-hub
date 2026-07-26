from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from pathlib import Path

THEME_VERSION = "research-theme-v1"
THEME_STYLESHEET_HREF = f"/theme/{THEME_VERSION}.css"
THEME_STYLESHEET_PATH = (
    Path(__file__).resolve().parent / "static" / f"{THEME_VERSION}.css"
)
_THEME_LINK = (
    f'<link rel="stylesheet" href="{THEME_STYLESHEET_HREF}" '
    f'data-research-hub-theme="{THEME_VERSION}">'
)
_CHARSET_PATTERN = re.compile(
    br"""(?:charset\s*=\s*["']?\s*|<meta[^>]+charset\s*=\s*["'])([a-zA-Z0-9._-]+)""",
    re.IGNORECASE,
)


class ThemeInjectionError(ValueError):
    """Raised when a registered HTML document cannot be themed safely."""


def _csp_allows_theme_stylesheet(value: str) -> bool:
    directives: dict[str, list[str]] = {}
    for directive in value.split(";"):
        parts = directive.strip().split()
        if parts:
            directives[parts[0].casefold()] = [
                token.casefold() for token in parts[1:]
            ]
    sources = (
        directives.get("style-src-elem")
        or directives.get("style-src")
        or directives.get("default-src")
    )
    if sources is None:
        return True
    return any(
        token in {"'self'", "*", "http:", "https:"}
        or "127.0.0.1" in token
        or "localhost" in token
        for token in sources
    )


class _ThemeInjector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []
        self.injected = False
        self.html_insertion_index: int | None = None
        self.body_insertion_index: int | None = None
        self.declaration_insertion_index = 0

    @staticmethod
    def _reject_conflicting_csp(
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.casefold() != "meta":
            return
        normalized_attrs = {
            key.casefold(): value
            for key, value in attrs
            if value is not None
        }
        if (
            normalized_attrs.get("http-equiv", "").casefold()
            == "content-security-policy"
            and not _csp_allows_theme_stylesheet(
                normalized_attrs.get("content", "")
            )
        ):
            raise ThemeInjectionError(
                "The document CSP does not allow the versioned Hub stylesheet."
            )

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        self._reject_conflicting_csp(tag, attrs)
        if normalized == "body" and self.body_insertion_index is None:
            self.body_insertion_index = len(self.parts)
        self.parts.append(self.get_starttag_text() or f"<{tag}>")
        if normalized == "html" and self.html_insertion_index is None:
            self.html_insertion_index = len(self.parts)
        if normalized == "head" and not self.injected:
            self.parts.append(_THEME_LINK)
            self.injected = True

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self._reject_conflicting_csp(tag, attrs)
        self.parts.append(self.get_starttag_text() or f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "head" and not self.injected:
            self.parts.append(_THEME_LINK)
            self.injected = True
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")
        self.declaration_insertion_index = len(self.parts)

    def handle_pi(self, data: str) -> None:
        self.parts.append(f"<?{data}>")

    def unknown_decl(self, data: str) -> None:
        self.parts.append(f"<![{data}]>")

    def document(self) -> str:
        if not self.injected:
            insertion_index = (
                self.body_insertion_index
                if self.body_insertion_index is not None
                else self.html_insertion_index
                if self.html_insertion_index is not None
                else self.declaration_insertion_index
            )
            self.parts.insert(insertion_index, f"<head>{_THEME_LINK}</head>")
            self.injected = True
        return "".join(self.parts)


def _decode_html(content: bytes) -> str:
    candidates: list[str] = []
    if content.startswith(b"\xef\xbb\xbf"):
        candidates.append("utf-8-sig")
    match = _CHARSET_PATTERN.search(content[:4096])
    if match is not None:
        try:
            candidates.append(match.group(1).decode("ascii"))
        except UnicodeDecodeError:
            pass
    candidates.extend(["utf-8", "gb18030", "windows-1252"])
    tried: set[str] = set()
    for encoding in candidates:
        normalized = encoding.casefold()
        if normalized in tried:
            continue
        tried.add(normalized)
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    raise ThemeInjectionError("The registered HTML encoding could not be decoded.")


def inject_theme_html(content: bytes) -> bytes:
    try:
        parser = _ThemeInjector()
        parser.feed(_decode_html(content))
        parser.close()
        document = parser.document()
    except ThemeInjectionError:
        raise
    except Exception as error:
        raise ThemeInjectionError(
            f"HTML parser rejected the registered document: {type(error).__name__}."
        ) from error
    if f'data-research-hub-theme="{THEME_VERSION}"' not in document:
        raise ThemeInjectionError("The versioned theme marker was not injected.")
    return document.encode("utf-8")


def theme_etag(content: bytes) -> str:
    digest = hashlib.sha256(
        THEME_VERSION.encode("ascii") + b"\0" + content
    ).hexdigest()
    return f'"{digest}"'
