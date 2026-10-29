# 真实 PDF 小样本验收（2026-08-31）

在本机以默认阈值、pypdf 布局 visitor 和 `pdftotext -layout` 对 4 份只读 PDF 运行，
批处理结果为 `ok=4, error=0`：

| 样本 | 题名判定 | 作者判定 | 关键 QA 结果 |
|---|---|---|---|
| A11y-Compressor（Embodied） | `accepted_high_confidence` | `review_low_confidence` | 正确拼合三行正式题名；单一布局作者证据未冒充事实 |
| Video 文件名错配样本 | `accepted_high_confidence` | `review_low_confidence` | 正式题名判为 `Video-LLMs with Temporal Visual Screening`；明确报告文件名 `EMCompress...` 冲突 |
| 恢复 Video：FlexiVideo | `accepted_high_confidence` | `accepted_high_confidence` | PDF / MinerU / 参考题名一致；拒绝 CVPR Open Access 与 proceedings 页眉 |
| VLN：Walk and Read Less | `accepted_high_confidence` | `review_low_confidence` | 补全被截断文件名，正式题名与首页布局一致；未把隶属机构写进作者事实 |

随后运行单元测试和静态检查：

```text
pytest tools/paper_title_qa/test_qa.py -q  -> 9 passed
ruff check tools/paper_title_qa            -> All checks passed
```

`prepare` 也对当前完整输入执行过一次：paper_metadata 最新记录 376 条，其中恢复 Video
40 条由 recovery map 原位补充，最终 `total=376`、`unique_ids=376`、
`video_with_mineru=112`。对相同输出路径再次运行返回 2，输出 SHA-256 保持不变，证明不会覆盖。

真实运行的临时 JSONL 含本机绝对路径和 PDF SHA-256，因此验收后未留在工具目录；
正式批次应按 README 将结果写入本地 `work/validation/`，不得把含私人路径的日志发布出去。
