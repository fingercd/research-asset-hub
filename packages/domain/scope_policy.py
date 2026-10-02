"""Persistent, reviewable exclusions from user-facing domain membership."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml


@dataclass(frozen=True, slots=True)
class DomainExclusionRule:
    rule_id: str
    domain: str
    reason: str
    asset_ids: frozenset[str]
    title_patterns: tuple[re.Pattern[str], ...]

    def matches(self, *, asset_id: UUID | None, title: str) -> bool:
        if asset_id is not None and str(asset_id) in self.asset_ids:
            return True
        return any(pattern.search(title) is not None for pattern in self.title_patterns)


class DomainScopePolicy:
    def __init__(self, rules: Iterable[DomainExclusionRule] = ()) -> None:
        self.rules = tuple(rules)

    @classmethod
    def empty(cls) -> DomainScopePolicy:
        return cls()

    @classmethod
    def load(cls, path: Path) -> DomainScopePolicy:
        if not path.is_file():
            return cls.empty()
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(document, Mapping):
            raise ValueError("domain scope policy must be a mapping")
        raw_rules = document.get("rules", [])
        if not isinstance(raw_rules, list):
            raise ValueError("domain scope policy rules must be a list")
        rules: list[DomainExclusionRule] = []
        for raw_rule in raw_rules:
            if not isinstance(raw_rule, Mapping):
                raise ValueError("domain scope rule must be a mapping")
            rule_id = str(raw_rule.get("id") or "").strip()
            domain = str(raw_rule.get("domain") or "").strip()
            reason = str(raw_rule.get("reason") or "").strip()
            if not rule_id or not domain or not reason:
                raise ValueError("domain scope rule requires id, domain, and reason")
            asset_ids = frozenset(str(item) for item in raw_rule.get("asset_ids", []))
            for asset_id in asset_ids:
                UUID(asset_id)
            patterns = tuple(
                re.compile(str(item), re.IGNORECASE)
                for item in raw_rule.get("title_patterns", [])
            )
            rules.append(
                DomainExclusionRule(
                    rule_id=rule_id,
                    domain=domain,
                    reason=reason,
                    asset_ids=asset_ids,
                    title_patterns=patterns,
                )
            )
        return cls(rules)

    def excluded_domains(
        self,
        *,
        asset_id: UUID | None,
        title: str,
    ) -> frozenset[str]:
        return frozenset(
            rule.domain
            for rule in self.rules
            if rule.matches(asset_id=asset_id, title=title)
        )

    def filter_domains(
        self,
        domains: Iterable[str],
        *,
        asset_id: UUID | None,
        title: str,
    ) -> list[str]:
        excluded = self.excluded_domains(asset_id=asset_id, title=title)
        return [domain for domain in domains if domain not in excluded]

    def describe_match(
        self,
        *,
        asset_id: UUID | None,
        title: str,
    ) -> list[dict[str, Any]]:
        return [
            {"id": rule.rule_id, "domain": rule.domain, "reason": rule.reason}
            for rule in self.rules
            if rule.matches(asset_id=asset_id, title=title)
        ]
