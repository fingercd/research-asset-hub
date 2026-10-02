# PDF 正式题名 / 作者布局 QA

这是一个**只读、小样本优先**的论文首页核验工具。它用 `pypdf` 的文字 visitor
读取字体大小和坐标，重建首页行与题名块；若本机有 `pdftotext`，再以
`pdftotext -layout` 做独立的文字顺序佐证。工具不会修改 PDF、Markdown 或 Hub，
也不会把低置信候选自动写成正式事实。

## 为什么需要单独 QA

PDF 的 Document Info 经常为空或保留了投稿模板题名；截断文件名可能把真正题名截掉，
甚至像当前 Video 样本一样写着 `EMCompress`，而首页正式题名和 MinerU 一级标题实际是
`Video-LLMs with Temporal Visual Screening`。另一方面，CVPR 首页顶部的 Open Access / proceedings
声明在纯文本顺序中排在题名前，不能把它误当论文题名。

本工具为每个字段保留：

- `candidates`：每个来源的原文、基础置信度、布局证据、交叉一致性和最终置信度；
- `accepted`：仅在候选超过阈值且没有未决歧义时填写，否则为 `null`；
- `decision`：`accepted_high_confidence`、`review_low_confidence`、
  `review_ambiguous` 或 `no_candidate`；
- `conflicts`：文件名、metadata、MinerU、首页布局之间的明显分歧，以及被明确拒绝的
  proceedings / Open Access 页眉；
- `abstract_method_names`：从摘要中 `we propose/introduce/present ...` 句式抽出的短方法名，
  只用于佐证，不作为完整题名事实。

作者块只取题名下方、Abstract 上方的姓名样式行，并过滤 University、Institute、Department、
Correspondence、邮箱等隶属信息。复杂多栏或扫描版仍可能得到低置信结果，必须人工复核。

## JSONL 输入

每行一个对象，最小字段只有 `pdf_path`：

```json
{"id":"paper-001","pdf_path":"D:\\papers\\paper.pdf"}
```

可选字段：

- `mineru_path`：对应 MinerU Markdown；读取第一个非章节 `# 一级标题`，以及其下一条正文作为作者证据；
- `filename_title`：显式覆盖从 PDF 文件名推导的题名候选；
- `metadata_title` / `metadata_authors`：显式提供上游已抽出的 metadata；
- `reference_title`：已有人工或官方映射中的参考题名；仍作为证据候选，不绕过置信门禁。

相对路径以输入 JSONL 所在目录为基准。输出包含绝对来源路径与 SHA-256，适合本地审计，
不应直接发布到外部。

## 从 paper_metadata 生成 376 条 QA 输入

`prepare` 子命令把追加式 `paper_metadata` JSONL 归并为“每个来源路径的最新记录”，
并生成题名 QA 所需的精简输入：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_title_qa prepare `
  .\work\validation\2026-08-31-acquisition-paper-metadata.jsonl `
  --video-mineru-root "C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向\Video" `
  --recovery-map .\work\validation\2026-08-31-video-missing-40-recovery-map.json `
  --output .\work\validation\2026-08-31-paper-title-qa-input.jsonl
```

它执行以下映射和门禁：

- `source.path` → `pdf_path`；`source.sha256` → 稳定且唯一的 `id`；
- `/Title`、`/Author`、XMP 优先，`facts.title/authors` 作为回退，写入
  `metadata_title` / `metadata_authors`；
- 只为路径位于 `Video` 单元的现有 PDF 做标准化 stem 匹配，附加唯一的 MinerU Markdown；
  非 Video 论文即使没有 MinerU 仍完整输出；
- recovery map 的 40 个 index 分别附加其 `markdown` 和 `reference_title`。如果这 40 份 PDF
  已经出现在 paper_metadata 中，就原位补充证据；否则追加为新项；
- 默认要求输出恰好 376 条且 `id`、PDF 路径均唯一，任一门禁失败时不创建输出；
- 输出文件用独占创建模式，已存在时返回批级错误，绝不覆盖。测试小批次可显式传
  `--expected-count N`。

恢复 PDF 路径默认从 recovery map 旁边的 `*-download.json` 报告读取；没有下载报告时，
必须传 `--recovered-pdf-root <目录>`，目录内文件名采用 `missing-<index>-*.pdf`。
`--video-mineru-root` 既接受 Video 单元根，也接受其 `work/mineru` 目录。

## 批量运行与断点

```powershell
.\.venv\Scripts\python.exe -m tools.paper_title_qa `
  .\work\validation\paper-title-qa-input.jsonl `
  --output .\work\validation\paper-title-qa-results.jsonl
```

输出 JSONL 同时是追加式断点日志。只有 `record_key + 源 PDF SHA-256 + 参数摘要` 均相同才跳过；
因此同尺寸、保留 mtime 但内容已替换的 PDF 也会重跑。`--force` 可强制追加新修订。
损坏的中间日志行或带正常换行的损坏末行会硬失败；只有**没有换行的未完成末行**会按进程中断修复。

单个 PDF 消失、无权限、加密、解析失败或 MinerU 路径错误时，都会追加
`status=error` 的逐文件记录，其他输入继续处理。只要本轮存在逐文件错误，CLI 返回 1；
输入/输出日志本身不可读等批级错误返回 2。可用 `--no-pdftotext` 验证纯 pypdf 路径。

默认题名接受阈值为 `0.78`，可用 `--accept-threshold` 调整；候选分差门槛由
`--margin-threshold` 控制。调低门槛不会改变候选证据，只会改变 `accepted` 判定。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tools\paper_title_qa\test_qa.py -q
.\.venv\Scripts\ruff.exe check tools\paper_title_qa
```

测试覆盖 A11y-Compressor 的三行题名/两行作者、恢复 Video 的 CVPR proceedings 页眉拒绝、
VLN 截断文件名交叉评分、Video 错名冲突、SHA-256 断点、逐文件错误和日志尾部修复语义。
同时覆盖 prepare 的 metadata/facts 映射、Video stem 匹配、恢复项追加、非 Video 保留、
严格计数和禁止覆盖。
