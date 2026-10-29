"""Enrich candidate citation edges from Crossref and OpenAlex."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import UUID

from apps.api.config import PROJECT_ROOT, get_settings
from apps.api.database import DatabaseManager
from packages.integrations.citation_enrichment import (
    CitationProvider,
    CrossrefCitationProvider,
    OpenAlexCitationProvider,
    enrich_citations,
)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map official citation metadata only to existing local paper assets."
    )
    parser.add_argument(
        "--provider",
        choices=("all", "openalex", "crossref"),
        default="all",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument(
        "--asset-id",
        action="append",
        dest="asset_ids",
        type=UUID,
        help="Only enrich this asset ID; may be supplied more than once.",
    )
    parser.add_argument(
        "--retry-report",
        type=Path,
        help="Retry only recoverable HTTP 429 failures recorded in this previous report.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=15.0)
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "work" / "validation" / "citation-enrichment.json",
    )
    return parser.parse_args()


def _retry_targets(report_path: Path, requested_provider: str) -> tuple[set[UUID], set[str]]:
    """Read the small, sanitized retry set from a prior enrichment report."""

    try:
        document = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"could not read retry report {report_path}: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("failures"), list):
        raise ValueError("retry report must contain a failures list")

    asset_ids: set[UUID] = set()
    providers: set[str] = set()
    for failure in document["failures"]:
        if not isinstance(failure, dict):
            continue
        provider = failure.get("provider")
        if (
            failure.get("recoverable") is not True
            or failure.get("detail") != "HTTP 429"
            or not isinstance(provider, str)
            or (requested_provider != "all" and provider != requested_provider)
        ):
            continue
        try:
            asset_id = UUID(str(failure.get("asset_id")))
        except (TypeError, ValueError):
            continue
        asset_ids.add(asset_id)
        providers.add(provider)
    unsupported_providers = providers - {"openalex", "crossref"}
    if unsupported_providers:
        names = ", ".join(sorted(unsupported_providers))
        raise ValueError(f"retry report contains unsupported citation provider(s): {names}")
    return asset_ids, providers


def _providers(
    arguments: argparse.Namespace, selected_provider_names: set[str] | None = None
) -> list[CitationProvider]:
    providers: list[CitationProvider] = []
    provider_names = selected_provider_names or {arguments.provider}
    if "all" in provider_names or "openalex" in provider_names:
        providers.append(
            OpenAlexCitationProvider(
                api_key=os.getenv("OPENALEX_API_KEY"),
                timeout_seconds=arguments.timeout_seconds,
                max_attempts=arguments.max_attempts,
            )
        )
    if "all" in provider_names or "crossref" in provider_names:
        providers.append(
            CrossrefCitationProvider(
                mailto=os.getenv("CROSSREF_MAILTO"),
                timeout_seconds=arguments.timeout_seconds,
                max_attempts=arguments.max_attempts,
            )
        )
    return providers


def main() -> int:
    arguments = _arguments()
    if arguments.limit is not None and arguments.limit < 0:
        raise SystemExit("--limit cannot be negative")
    if arguments.asset_ids and arguments.retry_report:
        raise SystemExit("--asset-id and --retry-report cannot be used together")
    try:
        retry_asset_ids, retry_provider_names = (
            _retry_targets(arguments.retry_report, arguments.provider)
            if arguments.retry_report
            else (set(), set())
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error
    asset_ids = retry_asset_ids if arguments.retry_report else arguments.asset_ids
    providers = _providers(
        arguments,
        retry_provider_names if arguments.retry_report else None,
    )
    database = DatabaseManager(get_settings())
    try:
        with database.session() as session:
            report = enrich_citations(
                session,
                providers=providers,
                dry_run=arguments.dry_run,
                limit=arguments.limit,
                asset_ids=asset_ids,
            )
        rendered = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        arguments.report.parent.mkdir(parents=True, exist_ok=True)
        arguments.report.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
    finally:
        for provider in providers:
            provider.close()
        database.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
