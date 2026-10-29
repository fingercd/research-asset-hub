# Domain contracts

`packages.domain.schemas.NormalizedRecord` is the shared adapter contract.
The API, import worker and every source adapter must validate through that
class rather than maintaining source-specific copies.

## Extensibility

- Core asset types are listed in `AssetType`.
- A plugin can add an asset type without a migration by using a namespaced
  value such as `lab:simulation`.
- Domain slugs are open strings (`world-model`, `new-domain`) and never a
  database enum.
- Relation and workflow state enums are closed. Unknown values are rejected
  until their semantics, inverse and UI behavior are explicitly defined.

## Safe source paths

A discovered representation is addressed by:

```text
source_id + root_key + path_ref
```

`path_ref` is normalized to `/` separators and must not be absolute or contain
`.` / `..`. A controlled sibling directory is declared once in
`SourceConfig.asset_roots` and selected with `root_key`. This lets the current
reinforcement-learning source refer to its sibling PDF library without
accepting arbitrary traversal from manifest data.

## JSON Schema

`asset.schema.json` is the stable standalone asset schema. The complete
adapter schema, including representations, relations and warnings, is
generated from Pydantic with:

```python
from packages.domain.schemas import normalized_record_json_schema

schema = normalized_record_json_schema()
```

The generated document targets JSON Schema 2020-12.

`canonical-source-snapshot.schema.json` is the immutable intake-sidecar
contract for rich sources. It records every explicit catalog input and its
SHA-256, exact upstream field pointers, all representation entrypoints,
revisions, tree digests, materialization metadata and mounts, plus confirmed
repository relations with evidence. The sidecar may live only below
`config/intake/<source-id>/`; its `path_ref` values remain relative to the
separate, read-only source root.

Regenerate that checked-in schema from the Pydantic contract with:

```python
from packages.adapters import canonical_snapshot_json_schema

schema = canonical_snapshot_json_schema()
```

## Open exports

- JSONL is the lossless interchange format and includes a type envelope per
  line.
- Markdown provides one stable UUID-named note per asset.
- RO-Crate writes `ro-crate-metadata.json` and describes source files through
  `source://<source>/<declared-root>/<relative-path>` identifiers; it does not
  copy or mutate the files.
- `domain-catalog.schema.json` validates each `domains/<slug>/domain.json`
  summary. Hidden domain-overview collection assets are excluded from content
  counts.
- `domain-overview.schema.json` validates the stable representation pointer
  used by domain portals without exposing an absolute source path.
- `domain-portal-v2.schema.json` documents the native full-page portal
  response. Curated inputs live in `config/domain-portals/v2/<slug>/`, always
  assemble into the same nine ordered sections (`scope`, `catalog`, `timeline`,
  `capability-method-map`, `landmarks`, `benchmarks`, `reading-routes`,
  `frontiers`, `evidence`), and carry source digests plus stale status. Database
  counts and the research-unit catalog are merged at request/export time rather
  than copied into the content files; `paper_count` is the paper-counting source
  of truth, while asset and repository counts remain separate measures.
