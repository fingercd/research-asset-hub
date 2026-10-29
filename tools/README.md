# 资料工具箱

[返回首页](../README.md) · [日常维护脚本](../scripts/README.md) · [入库流程](../docs/USER_KNOWLEDGE_BASE_GUIDE.md)

这些工具服务于“发现资料 → 核对身份 → 查代码 → 形成证据 → 登记”的不同阶段。**工具产出不等于正式入库，元数据候选不等于论文事实，HTML 生成不等于验收通过。** 每个工具自己的 README 是输入契约与参数的依据。

## 可复用的检查和证据工具

| 工具 | 输入 → 输出 | 边界 |
| --- | --- | --- |
| [paper_census](paper_census/README.md) | 受控材料目录 → PDF/HTML/manifest 文件清单、指纹、基础诊断 | 只读发现，不决定身份合并或归属 |
| [paper_metadata](paper_metadata/README.md) | PDF → 元数据候选、原文线索、JSONL/CSV | 不联网；代码 URL 始终待核验 |
| [paper_title_qa](paper_title_qa/README.md) | PDF 首页/已有记录 → 题名与作者布局证据 | 小样本先行，复杂版式可能需人工确认 |
| [paper_author_resolution](paper_author_resolution/README.md) | 题名 QA、记录与元数据 → 作者覆盖层 | 不修改原记录或 PDF |
| [paper_analysis_evidence](paper_analysis_evidence/README.md) | paper-record、PDF、可选 MinerU → 带页码证据包 | 不自动生成论文结论；上下文仍需阅读 |
| [paper_code_audit](paper_code_audit/README.md) | 代码线索、核对记录与本地 remote → 逐篇审计 | 默认离线；联网/归档模式需审查授权与目标 |
| [paper_closure](paper_closure/README.md) | 文件盘点、来源清单、目录 → 每份 PDF 的结局表 | 只写报告，不修库、不改索引 |
| [paper_intake_html](paper_intake_html/README.md) | 已审查的结构化记录 → HTML 与结构验收报告 | 写新输出，不能替代原文阅读、内容/视觉验收 |

### 选择顺序

1. 不知道磁盘上有什么：先 census，不直接导入。
2. 题名/作者不确定：metadata + title QA，保留冲突；不要只信文件名。
3. 不确定是否官方实现：code audit，区分线索、官方链接与实际本地快照。
4. 写导读：先 evidence，引用实际 PDF 页码，再按制作规范写作与审查。
5. 需要正式可搜索/可打开：回到[入库手册](../docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)，完成身份、旁车、备份、幂等、索引与打开链路。

本资料根制作论文 HTML 必须先完整读取 `D:/ResearchLibrary/docs/paper-html-standard.md`；在别的机器上需取得对应规范后再制作。旧模板不是自动合格标准，也不授权覆盖已登记页面。

## 固定历史批次工具：不要直接套新数据

| 工具 | 固定范围与注意事项 |
| --- | --- |
| [paper_intake_builder](paper_intake_builder/README.md) | 2026-08-31 的 376 篇归一与复制计划，具有固定集合契约 |
| [paper_record_finalizer](paper_record_finalizer/README.md) | 同一 376 条记录的作者、题名、代码证据收口，不是任意记录清洗器 |
| [existing_code_materializer](existing_code_materializer/README.md) | 七个指定领域审计 → 归档授权与 manifest 草案；始终是 dry-run 规划 |
| [compression_unit_builder](compression_unit_builder/README.md) | 三个压缩资料单元与既定批次门禁；不能覆盖冻结单元 |
| [reclassified_paper_builder](reclassified_paper_builder/README.md) | 217 份转投既有领域资料；在独立 staging 生成派生文件 |
| [external_asset_copy](external_asset_copy/README.md) | 审核过的 89 个目标复制台账；实际迁移需要独立授权，不改 source |
| [paper_recovery](paper_recovery/README.md) | 固定缺失 PDF 映射的下载/恢复；候选恢复不等于身份核验完成 |

工具名称中的“builder / finalizer / recovery”不代表可以重跑覆盖历史结果。读清输入 ID 集合、批准文件、输出目录与拒绝覆盖规则后再决定是否适用。

## 测试与贡献

Python 环境与锁文件共用仓库根配置。工具的测试放在各自目录，默认根 pytest 只发现 `tests/`，因此改工具时显式执行对应测试，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest tools/paper_metadata/test_extract.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit -q
```

只在临时 fixture 上验证，不把测试产物写进注册的来源根。未经确认无用不删历史工具；明确被新实现替代且无引用时再连同测试、文档一起移除。新增工具优先简单、边界明确、支持预演，并保持失败与跳过可见。
