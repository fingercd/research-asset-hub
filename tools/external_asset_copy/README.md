# External formal asset copy

This tool consumes the reviewed
`work/validation/2026-08-31-external-root-relocation-preflight.json` evidence and builds an exact,
non-overwriting file ledger for the 89 formal external targets. Its default mode is read-only.

It exists only to stage a later, separately approved relocation. It does **not** edit a registered
source, `config/sources.yaml`, an intake manifest, or a desktop junction. Reports are the only files
written by a dry-run.

## Safety policy

- Each formal file is streamed through SHA-256 and checked against the preflight fingerprint.
- Formal directories are recursively inventoried without following symlinks, Windows junctions, or
  other reparse points. Every allowed file has its own SHA-256, size, and destination in the ledger;
  the directory also receives a deterministic content-tree digest.
- `.git`, dependency environments, caches, build output, browser profiles, model weights,
  checkpoints, training/data directories, and generated samples are excluded.
- `.env`, auth/credential/token/secret/key/cookie/password files are listed by relative path and
  reason only. Their contents are never opened or hashed.
- A Git repository's `HEAD` and sanitized `remote.origin.url` may be collected with local read-only
  Git commands. No fetch, checkout, hook, or credential helper is invoked.
- Formal HTML is parsed for local CSS, JavaScript, image/media, CSS `url()`/`@import`, and static JS
  module references. A dependency is allowlisted only when it stays inside the same mapped source
  root, is a regular non-reparse file, and passes the same exclusion policy.
- Destination files are never overwritten. An existing byte-identical file is an idempotent skip;
  any type, content, reparse, or directory-tree conflict blocks apply.

## Reviewed dry-run

Run from the repository root:

```powershell
& .\.venv\Scripts\python.exe tools\external_asset_copy\copy_assets.py `
  --preflight work\validation\2026-08-31-external-root-relocation-preflight.json `
  --core-root 'D:\ResearchLibrary' `
  --report-json tools\external_asset_copy\reports\2026-08-31-dry-run.json `
  --report-md tools\external_asset_copy\reports\2026-08-31-dry-run.md
```

Exit code `0` means the input was valid and every planned destination was either absent or already
byte-identical. Exit code `2` means the report was still written when possible, but an error or
conflict requires review.

## Apply is deliberately gated

Do not run this while reviewing the plan. After the user separately approves creation of the
destination files, use both flags:

```powershell
& .\.venv\Scripts\python.exe tools\external_asset_copy\copy_assets.py `
  --preflight work\validation\2026-08-31-external-root-relocation-preflight.json `
  --core-root 'D:\ResearchLibrary' `
  --report-json tools\external_asset_copy\reports\apply.json `
  --report-md tools\external_asset_copy\reports\apply.md `
  --apply --confirm-apply COPY_FORMAL_ASSETS
```

Apply first rebuilds the entire dry-run plan and refuses to start when it contains any error. Files
are created with exclusive-create semantics, streamed and verified, and removed if their copy fails.
It still does not remove or change the old roots and does not create junctions.

## Tests

```powershell
& .\.venv\Scripts\python.exe -m pytest tools\external_asset_copy\test_copy_assets.py -q
& .\.venv\Scripts\python.exe -m ruff check tools\external_asset_copy
```
