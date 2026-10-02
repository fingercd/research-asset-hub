# 最终 paper-record 元数据收口

`paper_record_finalizer` 把已经应用的 376 条 paper-record、作者证据层、五批官方代码研究、人工题名决定及 EMCompress 版本恢复，编译为一个只读派生产物。它不修改 PDF、HTML、代码目录、Hub 配置、manifest 或数据库。

输入必须严格使用同一个 376 条 `paper_id` 集合；官方代码研究也必须保持 237 条 `official_public`、139 条已审计非公开状态。输出不会把仅有 URL 的公开代码误报为本地代码：状态为 `official_url_only`，直到独立的归档流程完成并核验本地仓库。

题名规则是可审计的：先尊重已批准人工决定；仅当两方是同一题名的版式小型大写差异时采用 Agent 的精确题名；一方只是另一方的完整前缀时采用更完整者。`Cut Your Losses`、`Beyond Variance`、`MemER`、`FASTer`、`S²Q-VDiT` 与 EMCompress 有显式一手证据规则。EMCompress 保持单一 canonical paper，并额外登记恢复的 TVS PDF representation。

对于原记录缺失的摘要，工具只会抽取 PDF 中明确的 `Abstract` 到 `Introduction` 文本；无法可靠切分时写明“待核验”，而不是伪造摘要。

```powershell
$batches = Get-ChildItem work/validation/code-research/batch-*.jsonl | Sort-Object Name
$args = foreach ($batch in $batches) { '--batch'; $batch.FullName }

.\.venv\Scripts\python.exe -m tools.paper_record_finalizer.finalize `
  --records work/validation/2026-08-31-paper-intake-applied/paper-records.jsonl `
  --author-resolution work/validation/2026-08-31-author-resolution-v2.jsonl `
  --title-decisions work/validation/2026-08-31-title-review-decisions.json `
  --emcompress-recovery work/validation/2026-08-31-emcompress-version-recovery.json `
  @args `
  --output-prefix work/validation/2026-08-31-paper-records-final
```

生成 `<prefix>.json`、`.jsonl`、`.csv`、`.md` 和 `<prefix>-title-changes.json`。所有输出默认拒绝覆盖，以防误替换审计证据；修正工具本身后可显式传 `--replace-output` 重建这一组派生产物。

当 237 个已批准公开仓库均已完成本地归档和审计时，追加
`--local-code-audit <完整376条paper_code_audit JSON>` 与
`--core-root <核心领域根>`，并把输出前缀改为
`2026-08-31-paper-records-final-v2`。该模式要求审计 ID 集合严格等于
376 条记录；每一条 `official_public` 都必须是 `official_local`，且已选
本地归档的路径、官方 remote、固定 commit、clean 状态、许可证均匹配并
在核心领域内。任一 `official_url_only` 或不匹配都会拒绝生成 v2；139 条
非公开细分状态保持不变。

## 合并中文题名

`merge_title_zh` 是独立子命令：从 `final-v2` 复制整条记录，只添加已审计的
`title_zh`。它要求恰好五份 JSONL、376 条唯一 ID、每条批次英文题名与
`final-v2.title` 字符串完全相等，并拒绝空值、英文原样或“待翻译”等占位题名。

```powershell
$batches = Get-ChildItem work/validation/title-zh/batch-*.jsonl | Sort-Object Name
$batchArgs = foreach ($batch in $batches) { '--batch'; $batch.FullName }

.\.venv\Scripts\python.exe -m tools.paper_record_finalizer.merge_title_zh `
  --records work/validation/2026-08-31-paper-records-final-v2.jsonl `
  @batchArgs `
  --output-prefix work/validation/2026-08-31-paper-records-final-v3
```

输出 `final-v3` 的 JSON、JSONL、CSV、Markdown 和 `-title-zh-audit.json`；
相同输入可用 `--replace-output` 确定性重建。

```powershell
.\.venv\Scripts\python.exe -m pytest tools/paper_record_finalizer/test_finalizer.py -q
.\.venv\Scripts\ruff.exe check tools/paper_record_finalizer
```
