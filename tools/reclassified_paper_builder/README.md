# 217 份转投论文配套构建器

本工具只处理 2026-08-31 批次中转投到既有领域的 217 条记录：

- `agent`: 15
- `fine-tuning`: 2
- `generation`: 15
- `infra`: 183
- `world-model`: 2

它读取 final paper-record、`paper_analysis_evidence` 证据目录和最终代码审计，在独立
staging 中生成每篇论文的 `metadata.json`、`paper.html`、`analysis-evidence.json`、
`analysis.zh.md`，并为每个领域生成 `catalog.json`、`index.html`、`README.md`。
现有 `paper.pdf` 只读；工具逐篇核验 `%PDF-`、SHA-256、字节数和页数。

论文 HTML 直接调用 `tools/paper_intake_html/paper_html.py` 的渲染和验证入口。问题、方法、
实验结果只呈现逐页原文证据；局限只有在证据明确出现 limitation/future-work 等语义时才呈现，
否则写“待核验”。新数据不生成、不登记 `guide`。

## 默认 staging

```powershell
./.venv/Scripts/python.exe -m tools.reclassified_paper_builder `
  --paper-records work/validation/2026-08-31-paper-records-final.json `
  --evidence-dir work/validation/2026-08-31-paper-analysis-evidence `
  --code-audit work/validation/2026-08-31-paper-code-approved-audit.json `
  --core-root "D:\ResearchLibrary" `
  --staging-root "D:\ResearchLibrary\work\reclassified-paper-stage-001" `
  --snapshot-date 2026-08-31
```

默认目标是：

`<core-root>/个人新增资料/2026-08-31-顶会压缩论文`

dry-run 即使发现 `official_url_only` 也会完整产生 staging 和 `build-report.json`，状态为
`staged_with_blockers`；只有 `--apply` 会硬阻断。apply 前还要求：

- 五个领域计数精确为 15/2/15/183/2；
- 217 个现有 `paper.pdf` 的 hash/size/pages 与 final record 一致；
- 每篇 evidence 与该 PDF 的 hash/page 对齐，证据摘录 hash 和页码有效；
- `official_local` 的代码路径真实位于核心领域内，Git HEAD 等于审计固定 commit；
- `not_provided` 保留 `none_found` / `third_party_only` / `announced_pending` /
  `not_applicable` 细分状态，并有英文精确题名审计。

## 事务与幂等

`--apply` 先完成全量 staging，再一次性预检所有目标。首次运行拒绝覆盖任何同名文件；后续只会
更新本构建器 manifest 记录为 owned、且当前 hash 仍等于上次产物 hash 的文件。提交失败或最终
HTML/PDF/代码验证失败时回滚已替换文件。输入和日期不变时再次 apply 的 created/updated 均为 0。

本工具不写 Hub manifest、数据库、索引或正式来源配置；这些仍由主流程完成。

## 自测

```powershell
./.venv/Scripts/python.exe -m pytest tools/reclassified_paper_builder/tests -q
./.venv/Scripts/ruff.exe check tools/reclassified_paper_builder
```
