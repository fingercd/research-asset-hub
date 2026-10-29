# Paper census scanner

`census.py` is the read-only discovery layer for the core-paper consolidation plan. It inventories
PDF, HTML, manifest, and catalog representations, computes SHA-256, and inspects PDF magic,
encryption, page count, and embedded title/author hints.

It does **not** decide canonical paper identity, disposition, domain routing, or PDF-to-HTML/code
closure. Those decisions consume this scanner's JSON/CSV evidence in the subsequent review.

## Safety boundary

- Scanned roots are only opened for reading; no file is created, renamed, or removed below them.
- Writes go only to the explicit `--output-prefix` and checkpoint path.
- Directory components `.git`, `node_modules`, `.venv`, cache, weights/checkpoint, and recovery
  locations are excluded by default. Matching is by whole component, not a substring in a paper
  title.
- Symlinks and Windows junction/reparse points are never followed, preventing cycles and accidental
  traversal into compatibility roots.
- `.env`, `auth.json`, credential/token/secret JSON, `.sandbox-secrets`, and private-key extensions
  are skipped without being opened, hashed, or listed.
- CSV is UTF-8 with BOM for reliable Chinese display in Excel; JSON and Markdown are UTF-8.

## Small scan

Run from the `research-asset-hub` repository root:

```powershell
& .\.venv\Scripts\python.exe tools\paper_census\census.py scan `
  'D:\ResearchLibrary\VLM框架3DV压缩\library' `
  --core-root 'D:\ResearchLibrary' `
  --max-files 10 `
  --output-prefix "$env:TEMP\paper-census-sample"
```

This creates `paper-census-sample.json`, `.csv`, `.md`, and `.checkpoint.json` next to the prefix.
Repeating the same command reuses hashes whose size and nanosecond mtime still match.

## Full scan in resumable shards

Use a different prefix/checkpoint for every shard. Candidate assignment is deterministic by root ID
and relative path, so shards do not overlap.

```powershell
$core = 'D:\ResearchLibrary'
$desktop = 'C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向'
$out = 'D:\ResearchLibrary\research-asset-hub\work\validation'

0..7 | ForEach-Object {
  & .\.venv\Scripts\python.exe tools\paper_census\census.py scan $core $desktop `
    --core-root $core `
    --shard-count 8 `
    --shard-index $_ `
    --output-prefix "$out\2026-08-31-core-paper-census-shard-$($_)"
}
```

An interrupted shard can be rerun with the identical command; completed file hashes are restored
from its checkpoint. The checkpoint is file-granular: interruption during a single very large file
rehashes only that file, not all completed files.

Merge all shard JSON reports only after every shard finishes:

```powershell
& .\.venv\Scripts\python.exe tools\paper_census\census.py merge `
  "$out\2026-08-31-core-paper-census-shard-0.json" `
  "$out\2026-08-31-core-paper-census-shard-1.json" `
  "$out\2026-08-31-core-paper-census-shard-2.json" `
  "$out\2026-08-31-core-paper-census-shard-3.json" `
  "$out\2026-08-31-core-paper-census-shard-4.json" `
  "$out\2026-08-31-core-paper-census-shard-5.json" `
  "$out\2026-08-31-core-paper-census-shard-6.json" `
  "$out\2026-08-31-core-paper-census-shard-7.json" `
  --output-prefix "$out\2026-08-31-core-paper-census"
```

`merge` rejects conflicting duplicate records and marks the result complete only when it receives
every index for one shard count and none of the shard scans used `--max-files`.

## Exit codes

- `0`: reports written and no discovery/stat/hash errors were recorded.
- `2`: invalid arguments/checkpoints, conflicting merge data, or discovery/stat/hash errors.
- `3`: `--strict-pdf` was requested and at least one PDF failed magic, parsing, or page-count checks.

PDF health warnings remain visible in each record even without `--strict-pdf`.
