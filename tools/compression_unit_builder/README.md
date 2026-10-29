# 三个压缩论文物理单元构建器

本工具把最终 `paper-records`、逐篇 `paper_analysis_evidence` 和最终官方代码审计合成为三个可独立浏览、可验证的物理资料单元：

| domain | 目标目录 | 正式批次门禁 |
|---|---|---|
| `video-llm-compression` | `VLM框架3DV压缩` | v1 33 + 新来源 95，4 个同字节复用、9 个历史替代版本、1 个 EMCompress 标题修订恢复版本，v2 规范论文 115 / 独立版本页 10 |
| `embodied-ai-compression` | `具身智能压缩` | 44 |
| `navigation-compression` | `导航压缩` | 20 |

默认只生成 staging；只有显式 `--apply` 才会事务式提交。工具不复制 PDF、不克隆代码：这两类正式资产必须先由 `paper_intake_builder --apply` 与 `paper_code_audit --apply` 落到对应单元。构建器会在提交前重新核验 PDF hash/大小、官方代码目录与固定 commit。

## 安全边界

- VLM v1 的 33 篇 PDF、旧 HTML、旧分析、旧代码、`*.v1.json`、旧 README/入口/manifest 全部列入保护集；v2 使用 `README.v2.md`、`index.v2.html`、`*.v2.json`、`library/analysis/v2/` 等独立路径。
- VLM v1 仓库状态不会照抄旧 catalog：每个 `valid` 条目都会重验 origin、固定 commit 和干净工作树。LiteFrame 的历史目录只有 README/assets 且声明 code soon，因此 v2 固定为 `announced_pending`；公开可用官方代码基线是 28，不是旧 manifest 的 29。
- `video-xl-2025` 与 `videochat-flash-2026` 的历史 worktree 存在 tracked deletion，不能成为 `official_local`。构建器只接受最终代码审计给出的、位于核心领域内、同一规范 remote、干净且固定 commit 的共享代码归档；找不到即硬阻断。
- 新数据不生成 `guide` 表示。每条新来源记录都有证据 JSON、证据定位 Markdown 和由 `tools/paper_intake_html` 生成的逐篇 HTML。
- staging 必须位于核心领域根内。HTML 先做无本地链接写入的 staging 校验；`--apply` 提交后立即做 PDF/Git 实物严格校验，失败则回滚本事务写入。
- 后续更新只能覆盖本构建器上一份 v2 manifest 明确声明拥有、且当前 hash 未被用户改动的文件。任何未知文件或用户改动均失败关闭。
- shared CSS、catalog、HTML、README、验证脚本与 outputs manifest 在同一事务中提交；目标已存在同 hash 时按幂等复用。

## 正式用法

在 Hub 根执行（证据目录和最终代码审计路径以实际产物为准）：

```powershell
.\.venv\Scripts\python.exe -m tools.compression_unit_builder `
  --paper-records work\validation\2026-08-31-paper-intake-approved\paper-records.json `
  --evidence-dir work\validation\2026-08-31-paper-analysis-evidence `
  --code-audit work\validation\2026-08-31-final-code-audit.jsonl `
  --version-recovery work\validation\2026-08-31-emcompress-version-recovery.json `
  --core-root "D:\ResearchLibrary" `
  --staging-root "D:\ResearchLibrary\work\compression-unit-builder\2026-08-31-dry-run" `
  --snapshot-date 2026-08-31
```

检查 `<staging-root>/build-report.json`，要求：

- `status=ready`；
- 三个领域计数与上表完全一致；
- `blockers=[]`；
- 160 个 HTML 都已生成（159 条新来源记录 + 1 个 EMCompress 当前 canonical PDF 页）；
- VLM `planned_files` 不含任何 v1 保护路径。

PDF 与代码资产均已落地且 dry-run 审核完成后，换一个全新的 staging 目录并在完全相同输入末尾增加 `--apply`。工具不允许复用旧 staging 目录。

## 产物

每个单元包含：

- `library/catalog/papers.v2.json`：canonical paper 与本次 source record/版本映射；
- `library/catalog/repositories.v2.json`：逐篇代码审计与本地归档状态；
- `library/evidence[/v2]/<source-record>.evidence.json`：原文证据包副本；
- `library/analysis[/v2]/<source-record>.md`：带页码/证据 ID 的定位页，缺失处明确“待核验”；
- `library/site/.../<source-record>.html`：由 `paper_intake_html` 生成的逐篇正式 HTML；
- `library/site/index[.v2].html` 与共用 CSS；
- `scripts/validate_unit[_v2].py`；
- `outputs/manifest[.v2].json`。

`--version-recovery` 只接受 `paper-version-recovery/v1` 的显式身份决定，不会凭题名
相似自动合并。EMCompress 的 canonical 题名、代码归属和当前 28 页 PDF 进入主页面；
26 页 TVS 旧题名 PDF 作为 alias/alternate 保留在独立版本页，两份 PDF 都不会被覆盖。

共享代码归档可以位于三个单元之外，但必须仍在核心领域根内。catalog 中使用相对单元的
可移植路径；HTML 和验证器解析后会再次确认它没有越出核心领域。旧 VLM worktree 即使
remote/HEAD 正确，只要 `git status --porcelain --untracked-files=all` 非空就不会通过。

本工具只完成物理单元构建。Hub 的新 taxonomy/source/sidecar、全量 dry-run、备份、两次 `ingest -All`、repository relation reconcile、抽取、索引、来源完整性、三按钮和浏览器验收仍必须按项目手册继续执行。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tools\compression_unit_builder\tests -q
.\.venv\Scripts\ruff.exe check tools\compression_unit_builder
```

测试只在 pytest 临时目录构造小 PDF、v1 fixture 和三领域小样本，不运行真实 `--apply`，也不修改三个真实单元。
