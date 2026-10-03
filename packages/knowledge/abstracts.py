"""Extract an author's labelled abstract, never a generated paper summary."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html.parser import HTMLParser

_START = re.compile(r"(?im)^\s*(?:a\s*b\s*s\s*t\s*r\s*a\s*c\s*t|摘\s*要)\s*(?:[.:：—–-]\s*)?$")
_INLINE_START = re.compile(r"(?im)^\s*(?:abstract|摘\s*要)(?:\s*[.:：—–-]\s*|[ \t]+(?=[A-Z]))")
_END = re.compile(
    r"(?im)^\s*(?:[1ⅠI][.\s]+)?(?:i\s*ntroduction|introduction|引\s*言|绪\s*论)"
    r"(?:\s*$|[ \t]+(?=[A-Z]))"
    r"|^\s*(?:index terms|keywords|key words|关\s*键\s*词)\s*[.:：—–-]"
    r"|^\s*(?:index terms|keywords|key words)[^\S\r\n]+(?=[A-Z])"
    r"|^\s*(?:CCS CONCEPTS|ACM Reference Format|PVLDB Reference Format)\s*[:：]?\s*$"
    r"|^\s*[∗*†]\s*(?:Equal Contribution|Corresponding author)"
    r"|^\s*(?:Code|Project|Website)\s*[:：—–]"
    r"|[©]\s*\d{4}\s*Published by"
)


class _ArxivAbstractParser(HTMLParser):
    """Read only arXiv's labelled title and author abstract, not page prose."""

    def __init__(self) -> None:
        super().__init__()
        self.current: str | None = None
        self.external_url: str | None = None
        self.values: dict[str, list[str]] = {"title": [], "abstract": []}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "h1" and "title" in classes:
            self.current = "title"
        elif tag == "blockquote" and "abstract" in classes:
            self.current = "abstract"
        elif tag == "a" and self.current == "abstract":
            href = attributes.get("href") or ""
            if href.startswith(("http://", "https://")):
                self.external_url = href

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.external_url = None
        if (tag == "h1" and self.current == "title") or (
            tag == "blockquote" and self.current == "abstract"
        ):
            self.current = None

    def handle_data(self, data: str) -> None:
        if self.current:
            if self.external_url and data.strip().casefold() in {"this https url", "this http url"}:
                data = self.external_url
            self.values[self.current].append(data)


def parse_arxiv_abstract(html: str) -> tuple[str, str] | None:
    parser = _ArxivAbstractParser()
    parser.feed(html)
    title = " ".join("".join(parser.values["title"]).split())
    abstract = " ".join("".join(parser.values["abstract"]).split())
    if not title.startswith("Title:") or not abstract.startswith("Abstract:"):
        return None
    return title.removeprefix("Title:").strip(), abstract.removeprefix("Abstract:").strip()


class _ProceedingsAbstractParser(HTMLParser):
    """Labelled metadata for CVF, NeurIPS, PMLR and ACL proceedings pages."""

    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.current: str | None = None
        self.root_tag = ""
        self.depth = 0
        self.parts: dict[str, list[str]] = {"title": [], "abstract": [], "heading": []}
        self.abstract_heading = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name") == "citation_title":
            self.title = attributes.get("content") or ""
        if self.current:
            if tag == self.root_tag:
                self.depth += 1
            return
        identifier = attributes.get("id")
        classes = (attributes.get("class") or "").split()
        role = None
        if identifier in {"papertitle", "page-title"}:
            role = "title"
        elif (
            identifier in {"abstract", "Abs1-content"}
            or attributes.get("data-title") == "Abstract"
            or any(
                name in classes
                for name in (
                    "abstract",
                    "acl-abstract",
                    "paper-abstract",
                    "article-abstract",
                    "field-name-field-paper-description",
                )
            )
        ):
            role = "abstract"
        elif tag in {"h2", "h3", "h4"}:
            role = "heading"
            self.parts["heading"] = []
        elif tag == "p" and self.abstract_heading and not self.parts["abstract"]:
            role = "abstract"
        if role:
            self.current, self.root_tag, self.depth = role, tag, 1

    def handle_endtag(self, tag: str) -> None:
        if self.current and tag == self.root_tag:
            self.depth -= 1
            if self.depth == 0:
                if self.current == "heading":
                    self.abstract_heading = "".join(self.parts["heading"]).strip() == "Abstract"
                self.current = None

    def handle_data(self, data: str) -> None:
        if self.current:
            self.parts[self.current].append(data)


def parse_proceedings_abstract(html: str) -> tuple[str, str] | None:
    parser = _ProceedingsAbstractParser()
    parser.feed(html)
    title = normalize_pdf_text(parser.title or "".join(parser.parts["title"]))
    abstract = normalize_pdf_text("".join(parser.parts["abstract"]))
    abstract = re.sub(r"^Abstract\s*[:：]?\s*", "", abstract)
    return (title, abstract) if title and len(abstract) >= 120 else None


def title_identity(title: str, first_page: str) -> dict[str, object]:
    """Check the registered title in the first-page header, before the abstract.

    Permit typography and the catalog's trailing parenthesized nickname, but
    never use abstract/body words as identity evidence or fuzzy semantic match.
    """
    marker = _START.search(first_page) or _INLINE_START.search(first_page)
    header = first_page[: marker.start() if marker else 1800]
    header = header[:2400]

    def compact(value: str) -> str:
        value = unicodedata.normalize("NFKD", value).casefold()
        # Standard acronym expansions in the catalog's title fields. Apply to
        # both sides, with word boundaries; this is not a semantic/fuzzy match.
        for short, full in {
            "gans": "generative adversarial networks",
            "llms": "large language models",
            "rlhf": "reinforcement learning from human feedback",
            "rl": "reinforcement learning",
            "π": "pi",
        }.items():
            value = re.sub(r"\b" + short + r"\b", full, value)
        return "".join(char for char in value if char.isalnum())

    variants = [title, re.sub(r"\s*[（(][^()（）]+[）)]\s*$", "", title)]
    # These exact catalog wording variants have been checked against the
    # labelled first page and official title metadata, not inferred from prose.
    aliases = {
        "Deep Reinforcement Learning: A Brief Survey": (
            "A Brief Survey of Deep Reinforcement Learning"
        ),
        "SpecInfer: Accelerating Large Language Model Serving with Tree-based "
        "Speculative Inference and Verification": (
            "SpecInfer: Accelerating Generative Large Language Model Serving with "
            "Tree-based Speculative Inference and Verification"
        ),
        "Reinforced Self-Training for Language Modeling": (
            "Reinforced Self-Training (ReST) for Language Modeling"
        ),
    }
    if title in aliases:
        variants.append(aliases[title])
    # Catalogs commonly prepend a short model nickname to the actual title.
    # Match the entire substantive suffix, rather than the acronym alone.
    prefix, separator, suffix = title.partition(":")
    if separator and len(prefix.split()) <= 3 and len(suffix.split()) >= 4:
        variants.append(suffix.strip())
    normalized_header = compact(header)
    matched = next(
        (value for value in variants if compact(value) and compact(value) in normalized_header),
        None,
    )
    return {
        "status": "verified" if matched else "unverified",
        "method": "normalized_title_in_first_page_header",
        "matched_title": matched,
        "first_page_header": header.strip(),
    }


@dataclass(frozen=True)
class AbstractExcerpt:
    text: str
    language: str
    start: int
    end: int
    end_marker: str


def normalize_pdf_text(value: str) -> str:
    """Only typographic transformations; do not paraphrase or translate."""
    value = unicodedata.normalize("NFKC", value).replace("\u00ad", "")
    value = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[a-z])", "", value)
    return " ".join(value.split())


def extract_abstract(value: str) -> tuple[AbstractExcerpt | None, str]:
    """Require an explicit heading and explicit end to avoid copying body text."""
    start = _START.search(value) or _INLINE_START.search(value)
    if start is None:
        return None, "abstract_heading_missing"
    end = _END.search(value, start.end())
    if end is None:
        return None, "abstract_end_missing"
    raw = value[start.end() : end.start()]
    if "(cid:" in raw or "\ufffd" in raw or re.search(r"\b(?:0|O)?~-learning", raw):
        return None, "abstract_glyph_encoding_corrupt"
    text = normalize_pdf_text(raw)
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(text.split())
    if not (45 <= words <= 650 or 120 <= chinese <= 1800):
        return None, "abstract_length_outside_bounds"
    if not re.search(r"[.!?。！？][\"'’”)]*$", text):
        return None, "abstract_tail_incomplete_or_polluted"
    # These indicate a figure, footer, heading or body mixed into the abstract.
    if re.search(
        r"(?im)^\s*(?:figure\s*\d|fig\.\s*\d|table\s*\d|references|"
        r"arxiv:|©|copyright|permission to|proceedings of|published as|"
        r"[2-9]\.\s+[A-Z]|\d{1,3}\s*$)",
        raw,
    ):
        return None, "abstract_layout_contamination"
    return AbstractExcerpt(
        text, "zh" if chinese >= 30 else "en", start.end(), end.start(), end.group().strip()
    ), "labelled_author_abstract"
