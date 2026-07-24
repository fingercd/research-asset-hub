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

1. Add the paper to that domain's existing manifest using its established
   workflow.
2. Re-run:

   ```powershell
   .\scripts\hub.ps1 ingest -Source <source-id> -DryRun
   ```

3. Review the new/update/merge counts.
4. Commit the import only after the dry-run is clean.

`(source_id, source_record_id)` is the idempotency key. Re-running an unchanged
source creates no duplicate platform record.

## Adding a new domain

1. Add a domain entry to `config/taxonomy.yaml`.
2. Add a source entry to `config/sources.yaml`.
3. Select one of:
   - `generic_manifest` with a record selector and field mapping for a regular
     JSON manifest;
   - a registered custom adapter for a domain with multiple related manifests.
4. Add a small, non-sensitive contract fixture and expected-record test.
5. Run preflight and dry-run before enabling a real import.

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

