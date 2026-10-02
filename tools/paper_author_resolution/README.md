# 论文作者证据归并

`tools.paper_author_resolution` 从三份现有、只读的输入中生成一个作者字段覆盖层：

- 题名/作者首页布局 QA JSONL；
- 已批准的 paper-records JSONL；
- 元数据提取 JSONL。

它不会修改这三种输入、正式 paper records、Hub 配置或 PDF。每行输出一个稳定
`record_key`，且必有以下二者之一：`authors`（明确逗号分隔或官方一手来源）或
`authors_display`（首页提取将名字压扁为连续文本时的保守呈现）。原始候选、坐标行、
QA 判定及官方 URL 都保存在 `evidence`，因此后续可人工复核而无需重新猜测。

当前批次有 6 条首页没有作者候选，固定使用官方 arXiv/ACL Anthology/NeurIPS 记录：
CODEPROMPTZIP、Ground Slow, Move Fast、O1-Pruner、OCP、ReST-KV、SALS。其余条目
只使用其 PDF 首页布局候选；不会利用不受控的 PDF 元数据把作者伪装成事实。

## 运行

```powershell
.\.venv\Scripts\python.exe -m tools.paper_author_resolution `
  --title-qa .\work\validation\2026-08-31-title-author-qa-results-v2.jsonl `
  --records .\work\validation\2026-08-31-paper-intake-approved\paper-records.jsonl `
  --metadata .\work\validation\2026-08-31-acquisition-paper-metadata.jsonl `
  --output .\work\validation\2026-08-31-author-resolution.jsonl
```

门禁为三份输入各 376 个唯一 SHA-256、完全同一集合、输出 376 个唯一 `record_key`，且
没有空作者表示。输出拒绝覆盖；需生成新修订时使用不同文件名。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tools\paper_author_resolution\test_resolve.py -q
.\.venv\Scripts\ruff.exe check tools\paper_author_resolution
```
