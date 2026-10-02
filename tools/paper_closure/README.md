# Paper closure auditor

这是一个只读的逐篇闭环审计器。它把 `paper-census/v1` 文件清单与 Research Asset Hub 的来源 manifest、辅助 catalog 和开放目录合并成一张“每份 PDF 的结局表”，同时输出 JSON、CSV 和 Markdown。

它不会修改来源、Hub 配置、数据库或索引。工具只写 `--output-prefix` 指定的三份报告。

## 它回答什么

每份 census PDF 都会有：

- `classification` 与 `classification_reason`：`formal_paper` 才进入论文闭环门禁；代码仓库中的 README/figure/example 等 PDF 保留为 `repository_document`，附件类保留为 `supplement_or_attachment`，测试/临时产物保留为 `invalid_or_nonpaper`，均不会被误报为待登记论文；
- `disposition` 与 `required_actions`；
- PDF 健康、是否位于核心领域、是否已登记；
- canonical 候选、匹配证据与冲突；
- 本地 HTML、HTML URL 及是否仍在核心领域之外；
- 官方代码 URL、本地代码目录及是否仍在核心领域之外；
- 来源 adapter 的 `asset.attributes.code_status` / `no_code_reason`，以及
  代码指南（`representation_type: guide` / `role: guide`）中的明确结论：
  `none_found`、`no_public_code_found`、`no_confirmed_code_found`、
  `no_official_code` 等明确无公开代码状态映射为 `explicit_no_code`，并保留
  `announced_pending`、`third_party_only` 及无代码原因。这些结论不会退化为
  `official_code_review_required`；只有原因而没有受支持的状态仍需人工复核。
- Hub `implemented_by` 关系状态；
- 所有 `unresolved_flags`。

匹配策略有一道硬边界：

1. DOI、arXiv、PDF SHA-256、精确登记路径、Hub provenance 以及明确的 `same_as`/`version_of` 可以建立 canonical 候选。
2. 规范化题名或高相似题名只写入 `weak_title_candidates`。
3. 弱题名候选始终带 `auto_merge: false`，永远不会自动成为 `canonical_match.canonical_id`。

只有开放目录中实际存在的 `implemented_by` 边才会报告为 `confirmed`。manifest 里只有仓库表示、但没有提供开放目录证据时，状态是 `declared_unverified`，避免把“应该已入库”误报成“数据库关系已确认”。

代码结论兼容以下形式：来源 adapter 的 `asset.attributes.code_status` 与
`asset.attributes.no_code_reason`；catalog 的 `asset.attributes`、顶层
`attributes` 或 `source_payload.attributes`（含嵌套 `source_payload.asset.attributes`）；
以及表示的 `metadata.status` / `metadata.code_status` 或
`metadata.catalog_code_guide.status`。未知状态会被忽略，以保持旧 manifest 的审查语义；
多个明确状态互相冲突时会 fail closed，产生
`official_code_evidence_conflict`，不会静默关闭。已存在的 repository 本地表示始终
优先于指南中的待发布/第三方说明。输出的 `code.guide_status`、
`code.explicit_status`、`code.explicit_no_code_reason` 和
`code.explicit_status_conflict` 保存归一化结论与审计信号。

## 当前 336 份检查

在项目根目录运行（路径按本机实际报告位置替换）：

```powershell
./.venv/Scripts/python.exe -m tools.paper_closure `
  --census work/validation/paper-census-full.json `
  --sources-yaml config/sources.yaml `
  --catalog outputs/open-catalog/catalog.jsonl `
  --core-root "D:/ResearchLibrary" `
  --batch-root "C:/Users/ExampleUser/Desktop/顶会论文PDF_按方向" `
  --batch-expected-current 336 `
  --batch-expected-target 376 `
  --require-batch-count current `
  --output-prefix work/validation/paper-closure-current
```

`batch_check.current_count_matches` 必须为 `true`。当前 336 份时，`remaining_to_target` 应为 40。

## 恢复后的 376 份检查

恢复并重新生成 census 后复跑同一命令，把核心领域 staging 加入同一采集批次作用域，并把门禁改为：

```powershell
  --batch-additional-root "D:/ResearchLibrary/VLM框架3DV压缩/work/intake-20260831/recovered-pdfs" `
  --require-batch-count target `
  --output-prefix work/validation/paper-closure-target
```

`--batch-root` 继续指向只读 Desktop 采集源；`--batch-additional-root` 可重复，专门计入已经写到核心领域的恢复 PDF，不要求也不允许把恢复文件写回 Desktop。此时 `batch_check.target_reached` 必须为 `true`，`remaining_to_target` 必须为 0。SHA 重复数会单独显示；数量相符不能代替逐篇 closure 验收。

## 输入

- `--census`：必须是 `paper-census/v1` JSON；只审计其中 `kind: pdf` 的记录。
- `--sources-yaml`：Hub 的 `config/sources.yaml`。工具调用项目现有只读 adapter 来读取各 manifest 与其声明的辅助 catalog。
- `--manifest`：可重复，补充 JSON/YAML manifest。
- `--catalog`：可重复；推荐传 `outputs/open-catalog/catalog.jsonl`，这样才能核验真实 Hub asset ID、provenance 和关系边。
- `--no-code-allowlist`：按英文精确标题登记的无代码白名单。省略时自动尝试 `sources.yaml` 同目录的 `check-unit-actions-allowlist.yaml`。
- `--retirement-status`：可选的 source lifecycle status JSON（例如
  `work/validation/2026-08-31-source-retirement-status.json`）。其中
  `lifecycle_status: retired` 的 source 表示只作为历史证据保留，不参与主动
  PDF/HTML/代码路径聚合；完整 `records` 仍写入报告，并在 `retirement`、
  `stats.historical_retired_count` 和 `stats.historical_retired_aliases` 中标明。
- `--weak-title-threshold`：只影响候选提示，默认 0.94，不会改变“不自动合并”的规则。

工具不会递归扫描 manifest/catalog 之外的代码仓库，也不会读取 `.env`、认证缓存或密钥文件。

## 输出和退出码

- `<prefix>.json`：完整机器可读证据，包括每篇的表示路径、URL、关系目标和 flags。
- `<prefix>.csv`：逐篇平表，UTF-8 BOM，便于 Excel 审阅。
- `<prefix>.md`：数量门禁、处置统计、未闭环 flags 和逐篇摘要。

JSON 报告保持 `paper-closure/v1` schema；每条 `code` 对象新增可选
`guide_status`、`explicit_status`、`explicit_no_code_reason` 和
`explicit_status_conflict` 字段（旧报告可缺省），而 `status` 兼容既有值并额外允许
`explicit_no_code`、`announced_pending`、`third_party_only`。表示证据的
`metadata` 与来源 asset 的 `attributes` 都是可选对象，默认 `{}`，因此旧输入无需迁移。

退出码：

- `0`：输入可读且所选数量门禁通过；这不表示所有论文已闭环，除非同时加 `--strict`。
- `2`：输入/schema/解析错误。
- `3`：`--require-batch-count` 不符，或 `--strict` 下仍有未闭环项/证据不完整。

如果任何 source adapter 失败，报告会保留其他来源的结果，同时将总状态设为 `incomplete_evidence`；不得把这类报告当作全量完成证明。

## 测试

```powershell
./.venv/Scripts/python.exe -m pytest tools/paper_closure/test_closure.py -q
./.venv/Scripts/ruff.exe check tools/paper_closure
```
