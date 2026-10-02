# 376 篇论文归一与复制计划生成器

这个狭义工具把 `paper_metadata` 的 JSONL/CSV、Video 40 篇恢复映射和现有
`VLM框架3DV压缩` v1 catalog 归一为可审计的 `paper-record`，并为每个 PDF 生成
不覆盖复制计划。它不修改任何现有单元、Hub 配置、已冻结 manifest 或来源文件。

## 固定批次契约

| 分组 | 预期 | 初始去向 |
|---|---:|---|
| Video 当前保留集 | 55 | `video-llm-compression` |
| Video 恢复集 | 40 | `video-llm-compression` |
| Embodied 主集 | 44 | `embodied-ai-compression` |
| VLN 主集 | 20 | `navigation-compression` |
| Video `work/deleted_pdf` | 17 | 现有领域规则或 `needs-review` |
| Embodied 排除集 | 9 | 现有领域规则或 `needs-review` |
| `其他` | 191 | 现有领域规则或 `needs-review` |

默认将精确的 `55+40+44+20+17+9+191=376` 作为硬门禁。不删除任何排除项；
规则冲突或无匹配时进入 `needs-review`，每条记录保留 `routing.rule_ids`、理由和警告。

## 身份与去重

- `paper_id` 优先使用 `arxiv-<YYMM-NNNNN>`；无 arXiv 时使用文件名中的规范方法名
  + 年份。
- 任何新批次内的 `paper_id` 或目标路径冲突都是显式阻断错误，不自动加序号。
- 对现有 VLM catalog 同时比对 SHA-256、arXiv/DOI、规范题名+年份、方法名+年份。
- 相同 SHA-256 在 Video 单元直接 `reuse_existing_vlm`；强身份相同但字节不同标为
  `hold_identity_review`，不会覆盖或自动决定版本。

### 显式版本决策

工具**不会生成或猜测批准**。只有人工核对后提供 `--identity-decisions`，
`hold_identity_review` 才能转换为 `copy_alternate_version`。契约见
`identity-decisions.schema.json`，最小示例：

```json
{
  "schema_version": "paper-intake-identity-decisions/v1",
  "decisions": [
    {
      "source_sha256": "64位小写SHA256",
      "existing_vlm_id": "holitom-2025",
      "decision": "same_identity_alternate_version",
      "version_label": "desktop-intake-2026-08-31",
      "target_relative_path": "library/papers/holitom-2025/versions/desktop-intake-2026-08-31.pdf",
      "evidence": ["Manually compared title, authors and abstract."]
    }
  ]
}
```

决策以 `(source_sha256, existing_vlm_id)` 为唯一复合键，并且必须同时满足：

- 当前动作确实是 `hold_identity_review`；
- 该记录的 VLM 候选中正好有一条匹配 `existing_vlm_id`；
- `decision` 只能是 `same_identity_alternate_version`；
- `target_relative_path` 使用 `/`，且实际解析到该现有论文目录的
  `versions/` 之下；
- 已有不同 hash 目标始终拒绝覆盖。

决策后的 paper record 保留新批次 `source_record_id`，并显式写入
`canonical_paper_id`、`existing_vlm_id` 和 `version_relation` 审计证据。决策文件中未列出的
hold 仍保留，并在 `--apply` 前使整批硬停止。
可选的非空 `evidence: string[]` 会不改写地进入 `version_relation.evidence` 和
`decision_evidence`，不应为了通过 schema 删除审计依据。

### 显式路由决策

`needs-review` 只能由 `--routing-decisions` 解决，工具不会自动生成决策。
契约见 `routing-decisions.schema.json`：

```json
{
  "schema_version": "paper-intake-routing-decisions/v1",
  "decisions": [
    {
      "paper_id": "example-method-2026",
      "source_sha256": "64位小写SHA256",
      "target_domain": "infra",
      "evidence": ["The primary task is model inference systems optimization."]
    }
  ]
}
```

- 复合键是 `(paper_id, source_sha256)`，必须唯一匹配一条当前 `needs-review`。
- `target_domain` 必须已存在于 `config/taxonomy.yaml`，不接受临时或未注册 slug。
- `evidence` 必须是非空字符串数组，原样进入 paper record。
- 未被显式决策覆盖的 `needs-review` 会在任何复制前阻断 `--apply`。

### 正式题名与作者 QA

376 篇正式执行必须同时传入只读 `--title-qa-results` 和对低置信项的
`--title-decisions`（契约见 `title-decisions.schema.json`）：

- QA 结果按源 PDF SHA-256 与 paper record 一对一匹配，人工决策按 QA
  `record_key` 匹配。
- `title.decision=accepted_high_confidence` 才直接采用 QA 题名；其余每项必须有
  显式人工 title decision 和非空证据。
- 作者只在 `authors.decision=accepted_high_confidence` 时覆盖；低置信候选不采用，
  保留上游作者并加 warning。
- QA 覆盖题名不改 `paper_id/source_record_id`；旧采集题名保留为 alias，
  canonical method/title 来自 QA。
- 全批 `--apply` 要求 376 项全覆盖、题名非空、且不再存在“可能截断文件名”
  正式题名 warning。
- `EMCompress` 错配样本保留稳定采集 ID，但 canonical title 必须是
  `Video-LLMs with Temporal Visual Screening`；该 alias 不得自动推导代码仓库。

### 作者解析

`--author-resolution` 读取已审核 JSONL，并同时校验 QA `record_key` 与源 PDF
SHA-256：

- 输入有 `authors: string[]` 时原样写入 record `authors`。
- 只有 `authors_display` 时原样写入 `authors_display`，不根据逗号、空格或换行
  伪拆作者；为兼容 Hub 数组契约，同时写 `authors=[authors_display]` 并标记
  `display_fallback=true`。
- `author_resolution` 保留 `raw`、完整 `evidence`、`status`、record key 和 PDF hash。
- 376 篇必须 376/376 匹配且作者值 0 空，否则 `--apply` 在写入前失败。

## 运行

在 `research-asset-hub` 根目录：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_intake_builder `
  --metadata work\validation\2026-08-31-acquisition-paper-metadata.jsonl `
  --video-recovery-map work\validation\2026-08-31-video-missing-40-recovery-map.json `
  --video-recovery-report work\validation\2026-08-31-video-missing-40-download.json `
  --desktop-root "C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向" `
  --vlm-catalog "D:\ResearchLibrary\VLM框架3DV压缩\library\catalog\papers.v1.json" `
  --identity-decisions work\validation\2026-08-31-video-version-decisions.json `
  --routing-decisions work\validation\2026-08-31-routing-decisions.json `
  --title-qa-results work\validation\2026-08-31-title-author-qa-results-v2.jsonl `
  --title-decisions work\validation\2026-08-31-title-review-decisions.json `
  --author-resolution work\validation\2026-08-31-author-resolution-v2.jsonl `
  --core-root "D:\ResearchLibrary" `
  --output-dir work\validation\2026-08-31-paper-intake-plan
```

不加 `--apply` 时只计算指纹并写报告，不复制 PDF。如恢复报告与恢复映射按
`*-download.json` / `*-recovery-map.json` 命名，可省略 `--video-recovery-report`。

首次 dry-run 可不传 `--identity-decisions`，以导出 hold 项。人工审核后传入显式
决策文件，确认 `unresolved_holds=0`，才能执行：

```powershell
# 仍然使用同一组输入，只增加 --apply
.\.venv\Scripts\python.exe -m tools.paper_intake_builder <同上参数> --apply
```

`--apply` 在任何写入前重算全部源/ 已有目标 hash。新目标用排他创建，不覆盖；
已有同 hash 文件作为幂等复用，已有不同 hash 文件使整批预检失败。
任何未决的 `hold_identity_review` 都会让 `--apply` 在首个写入之前失败。

## 输出

`--output-dir` 包含：

- `build-report.json`：硬门禁、数量、路由、VLM 匹配和阻断错误摘要。
- `paper-records.json` / `.jsonl` / `.csv`：HTML 子 agent 可直接使用的结构化记录；
  `pdf.path` 已是相对最终 HTML 的本地链接，绝对源/目标另存审计字段。
- `copy-plan.json` / `.csv` / `.md`：376 项逐条动作、源/目标、hash、理由和应用状态。

本工具只解决“规范记录 + PDF 复制”；HTML 生成、公开官方代码核验/克隆、Hub
sidecar、两次全量导入、关系、索引和界面验收仍是后续独立门禁。

## 自测

```powershell
.\.venv\Scripts\python.exe -m pytest tools\paper_intake_builder\test_builder.py -q
.\.venv\Scripts\ruff.exe check tools\paper_intake_builder
```
