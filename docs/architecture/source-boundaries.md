# Source boundaries and extension contract

## Safety boundary

All entries in `config/sources.yaml` are imported as read-only sources. The hub
stores normalized records, user-authored state, indexes, and reports inside the
`research-asset-hub` project. It must not rename, move, delete, annotate, or
rewrite files below a registered source root.

Paths persisted by the hub use:

```text
source_id + source-relative path
```

Absolute Windows paths are resolved only at runtime from `sources.yaml`. This
keeps asset identities stable when the workspace is moved or later mounted on a
server.

## Adding papers to an existing domain

1. Do not modify an already registered source or manifest.
2. Create an immutable sidecar manifest below
   `config/intake/<new-source-id>/manifest.json`.
3. Register a new source that reuses the existing domain slug and declares the
   user's stable external directory through explicit `asset_roots`.
4. Keep the original PDF/code/HTML in place; do not copy it into the Hub.
5. Re-run:

   ```powershell
   .\scripts\hub.ps1 ingest -Source <source-id> -DryRun
   ```

6. Review the new/update/merge counts, then run the all-source dry-run.
7. Commit only with `ingest -All`; partial formal import is rejected.

`(source_id, source_record_id)` is the idempotency key. Re-running an unchanged
source creates no duplicate platform record.

The first formal import stores both the manifest SHA-256 and the semantic source
configuration digest in `Source.config`. Every later formal import and release
report checks those frozen values. A changed manifest, adapter mapping, root, or
other source setting therefore fails closed instead of silently creating a new
source-record version. The formal CLI also performs a fresh all-source dry-run
before opening the write transaction.

## Adding a new domain

1. Add a domain entry to `config/taxonomy.yaml`.
2. Add a source entry to `config/sources.yaml`.
3. Select one of:
   - `generic_manifest` with a record selector and field mapping for a regular
     JSON manifest;
   - a registered custom adapter for a domain with multiple related manifests.
4. Add a small, non-sensitive contract fixture and expected-record test.
5. Run preflight and dry-run before enabling a real import.

Every title/year-only cross-source identity candidate must exactly match an
entry in `config/identity-merge-approvals.yaml`. DOI/arXiv matches remain
strong identifiers; an unapproved weak candidate is a hard stop for both
dry-run and formal import.

Adapters are discovered from the built-in registry and, later, from the
`research_asset_hub.adapters` Python entry-point group. Core ingestion does not
branch on domain names.

## Formal assets versus generated noise

Only files reached from normalized manifest fields or explicit
`formal_html`/`asset_roots` rules become formal representations. Recursive file
inventory is evidence for review, not automatic publication.

The following remain excluded unless a source override explicitly includes
them:

- `.git`, dependency environments, caches, build output, browser profiles;
- failed/orphan clones and temporary preview sites under `work/`;
- model weights, checkpoints, training data, and generated samples;
- unrelated HTML documentation inside upstream repositories.

Every skipped path class must be visible in an inventory or indexing report.
