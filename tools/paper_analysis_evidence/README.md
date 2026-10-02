# 论文 HTML 写作证据包生成器

本工具为逐篇 HTML 子 Agent 准备**可追溯原文证据**。它离线读取 `paper-record`
JSON、对应 PDF，以及可选的 MinerU Markdown；按论文常见章节抽取原文片段，并把每条片段固定到
PDF 页码和章节。工具不会生成中文总结、结论或“论文贡献”判断，也不会联网验证代码仓库。

输出用于辅助写作，不是可直接发布的论文解读。HTML 作者仍需阅读证据、检查上下文，并把自己的
中文概括明确当作概括；不能把机器分段标签或代码 URL 候选当成论文事实。

## 输入

`--input` 接受与 `tools/paper_intake_html/paper-record.schema.json` 兼容的三种 JSON：

1. 单个记录对象；
2. 记录数组；
3. 含 `papers`、`items` 或 `records` 数组的包装对象。

每条记录必须有稳定的 `paper_id`（也接受 `id`）。兼容 intake builder 时，PDF 按以下
优先级选择**第一个实际存在的文件**：

1. `target_pdf_path`：优先使用已经归拢到核心领域的目标 PDF；
2. `source_pdf_path`：目标尚未复制时回退到原始采集 PDF；
3. `pdf.path`；
4. 兼容字段 `pdf_path` 或 `representations.pdf.path`。

因此，对 intake builder 的批量 `paper-records.json` 使用核心领域作为 `--base-dir` 时，不会把
每条记录里的相对 `paper.pdf` 错误解析为核心领域根目录下同一个文件。

可选 MinerU Markdown 只接受显式字段，避免把人工笔记误当原文：

- `mineru_markdown.path` 或 `mineru_markdown` 字符串；
- `mineru_markdown_path`；
- `representations.mineru_markdown.path`。

相对路径默认相对于 `--input` 所在目录；也可以用 `--base-dir` 指定稳定资料根。单记录可用
`--pdf` 和 `--mineru-markdown` 临时覆盖路径。示例见
`examples/paper-record.example.json`。

MinerU 页码可由下列标记直接提供：

```markdown
<!-- Page 3 -->
<!-- page_number: 3 -->
<!-- page_idx: 2 -->
# Page 3
```

`page_idx` 按 0 起算转换为 PDF 页码。没有标记时，工具只在 Markdown 片段与唯一 PDF 页文本
能够可靠匹配时使用该片段；无法定位的 Markdown 片段进入 `warnings`，不会伪造页码。

## 批量运行

在 `research-asset-hub` 根目录执行：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_analysis_evidence `
  --input "D:\ResearchLibrary\work\paper-records.json" `
  --base-dir "D:\ResearchLibrary" `
  --output "D:\ResearchLibrary\work\paper-analysis-evidence"
```

单篇覆盖示例：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_analysis_evidence `
  --input .\tools\paper_analysis_evidence\examples\paper-record.example.json `
  --pdf "D:\papers\example.pdf" `
  --mineru-markdown "D:\papers\example.md" `
  --output .\work\paper-analysis-evidence-preview
```

面向 376 篇批次的默认行为：

- 每篇写 `<paper_id>.evidence.json`；
- 每处理完一篇就原子更新 `checkpoint.json`，进程中断后可继续；
- 断点比较 paper-record、PDF、MinerU Markdown、工具版本和选项的 SHA-256，不依赖
  `size + mtime`；输出文件自身的 SHA-256 也必须匹配才会跳过；
- 只跳过上次成功且完全未变的记录；上次失败的记录会重试；
- 单篇缺失、加密、损坏或无可定位正文时，写 `status=error` 的逐篇 JSON 并继续其他记录；
- 最后写 `batch-report.json`。存在逐篇失败时 CLI 返回 1；输入 JSON、断点或输出存储本身损坏时
  返回 2；
- 所有 JSON 使用 UTF-8、`ensure_ascii=false` 和 LF 换行。

`--force` 会重跑成功断点。断点损坏时工具失败关闭，不会静默丢弃历史状态。

## 输出契约

成功文件的 `schema_version` 是 `paper-analysis-evidence.v1`，可用
`evidence.schema.json` 做结构校验。`evidence` 固定含六组数组：

- `abstract`；
- `introduction_contributions`；
- `method`；
- `experiments_results`；
- `limitations_conclusion`；
- `code_urls`。

每条证据至少包含：

- `evidence_id`：单篇内稳定顺序号；
- `section`：检测到的原文章节标题；
- `page`：1 起算 PDF 页码；
- `excerpt`：原文抽取片段，只做空白折叠、长度截断和 URL 凭据/查询参数清洗；
- `excerpt_sha256`；
- `source_kind`：`pdf_text`、`mineru_markdown` 或 `pdf_annotation`；
- `locator`：PDF 页内行号、Markdown 行号或 PDF annotation 序号；
- `page_mapping`：原生 PDF 页、MinerU 页标记或文本匹配方式；
- `selection_reason`：机械选择规则，不是论文事实；
- `truncated`：是否因长度门限截断。

`code_urls` 额外包含 `url`、`candidate_kind` 和固定的
`verification=candidate_unverified`。发现 URL 仅说明论文文件里出现线索，**不证明它是官方代码、
开源、许可合规或对应当前论文**；正式归档前必须联网核验项目页/作者关系、仓库、许可证与 commit。
URL 输出会去掉 userinfo、端口、query 和 fragment，避免泄露签名参数或 token。

默认每篇所有 `excerpt` 合计不超过 18,000 字符，单条不超过 1,200 字符，每类最多 3 条。
可调整：

```text
--max-evidence-chars 18000
--max-excerpt-chars 1200
--max-items-per-category 3
```

`missing_categories` 与 `warnings` 必须交给 HTML 作者，不能因为工具退出成功而隐藏。扫描版 PDF
若既无可抽取文本，也没有带页码/可匹配的 MinerU Markdown，会明确失败，而不是产生无来源内容。

## HTML 子 Agent 使用规则

1. 只根据证据包和已核验 paper-record 写作；不要补入模型记忆中的方法或结果。
2. 每个方法、结果、局限性陈述都保留所用 `evidence_id`、`section` 和 `page`，方便复核。
3. `excerpt` 是原文证据，中文段落是作者概括；两者不能混称原文。
4. 对 `missing_categories` 显示“待核验”，不要自动填充。
5. 代码 URL 始终是待联网核验线索；本工具不会决定 `code.status`，也不会把题名加入无代码白名单。
6. 本工具的成功不替代 HTML 静态审计、Hub dry-run、两次幂等导入、关系对账和界面打开验收。

## 小样本自测

不要用测试命令扫描真实论文。测试会在临时目录构造 5 页小 PDF 和 MinerU Markdown：

```powershell
.\.venv\Scripts\python.exe -m pytest tools\paper_analysis_evidence\tests -q
.\.venv\Scripts\ruff.exe check tools\paper_analysis_evidence
```

覆盖：六类证据、页码与章节定位、PDF 回退、URL 凭据清洗、字符上限、UTF-8、内容哈希断点、
同尺寸同 mtime 输入替换、逐篇错误和扫描件无证据失败。该自测不会运行 376 篇全量批次，也不会
修改任何来源 PDF。
