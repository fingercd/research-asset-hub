"""External knowledge-tool integrations and lossless sync helpers."""

from .citation_enrichment import (
    CitationEnrichmentReport,
    CitationFetchResult,
    CitationProvider,
    CitationProviderError,
    CitationQuery,
    CitationReference,
    CrossrefCitationProvider,
    OpenAlexCitationProvider,
    enrich_citations,
)
from .credentials import CredentialStore, CredentialStoreUnavailable
from .html_audit import (
    DependencyFinding,
    DynamicHtmlProbe,
    DynamicPageCheck,
    DynamicProbeBatch,
    HtmlAuditReport,
    HtmlPageAudit,
    NodePlaywrightGatewayProbe,
    audit_registered_html,
)
from .html_policy import (
    HtmlPolicyInference,
    apply_html_policy_inferences,
    inferences_from_audit,
)
from .models import FieldConflict, SyncPreview, three_way_merge
from .obsidian import ObsidianAssetDocument, ObsidianVaultAdapter
from .obsidian_sync import (
    obsidian_sync_etag,
    preview_obsidian_assets,
    sync_obsidian_assets,
)
from .zotero import ZoteroLocalClient, export_bibtex, export_csl_json, stable_citekey

__all__ = [
    "CredentialStore",
    "CredentialStoreUnavailable",
    "DependencyFinding",
    "DynamicHtmlProbe",
    "DynamicPageCheck",
    "DynamicProbeBatch",
    "CitationEnrichmentReport",
    "CitationFetchResult",
    "CitationProvider",
    "CitationProviderError",
    "CitationQuery",
    "CitationReference",
    "CrossrefCitationProvider",
    "FieldConflict",
    "HtmlAuditReport",
    "HtmlPageAudit",
    "HtmlPolicyInference",
    "ObsidianAssetDocument",
    "ObsidianVaultAdapter",
    "OpenAlexCitationProvider",
    "NodePlaywrightGatewayProbe",
    "audit_registered_html",
    "apply_html_policy_inferences",
    "inferences_from_audit",
    "obsidian_sync_etag",
    "preview_obsidian_assets",
    "sync_obsidian_assets",
    "SyncPreview",
    "ZoteroLocalClient",
    "export_bibtex",
    "export_csl_json",
    "enrich_citations",
    "stable_citekey",
    "three_way_merge",
]
