# 论文 HTML 生成与验收流水线

本目录把经过审计的 `paper-record` JSON 确定性地生成成可离线阅读的中文论文页，并在交付前检查编码、结构、链接和离线性。

它只负责“结构化记录 → HTML”这一段，不从 PDF、文件名或网页猜测元数据，也不修改任何已登记来源。输入中没有证据的字段会在页面上明确显示为“待核验”。新数据不得把该页面登记成已经废除的 `guide` 表示；应按项目当前 manifest 契约登记为 HTML 表示，并在完整入库流程中继续执行 dry-run、备份、两次全量导入、HTML 审计、索引和界面验收。

## 文件

- `paper-record.schema.json`：输入记录的 JSON Schema（面向上游台账和编辑器）。
- `template.html`、`styles.css`：自包含页面模板和样式源；生成时 CSS 会内嵌到 HTML，不产生远程子资源依赖。
- `paper_html.py`：规范化、转义、渲染和静态验证核心。
- `generate.py`：批量/单篇生成入口。
- `validate.py`：独立验收入口，可检查单个 HTML 或整个目录。
- `examples/paper-record.example.json`：覆盖完整字段与“待核验”字段的示例。
- `tests/test_pipeline.py`：单元、对抗性输入和命令行回归自测。

## 最短用法

在 `research-asset-hub` 根目录执行：

```powershell
./.venv/Scripts/python.exe tools/paper_intake_html/generate.py `
  --input tools/paper_intake_html/examples/paper-record.example.json `
  --output work/paper-html-preview `
  --allowed-root "D:\ResearchLibrary" `
  --report work/validation/paper-html-generate.json

./.venv/Scripts/python.exe tools/paper_intake_html/validate.py `
  --input work/paper-html-preview `
  --allowed-root "D:\ResearchLibrary" `
  --report work/validation/paper-html-validate.json
```

`--allowed-root` 是正式 CLI 的必填参数；页面目标、PDF、代码和其他本地子资源解析后都必须位于该根内，桌面或其他根下的链接会被直接拒绝。生成器默认拒绝覆盖已有文件，经过人工确认需要重建尚未冻结的新页面时才加 `--overwrite`。批次先在每个目标同目录写 staging 文件，全量通过静态检查、PDF/Git 实物核验后才提交；失败会删除 staging，并保留原页面。默认检查所有本地链接确实存在，临时只做模板预览时可显式加 `--skip-local-link-check`，但它不会关闭根目录边界，正式交付不得跳过。

输入可以是：

1. 单个记录对象；
2. 记录数组；
3. 含 `papers`、`items` 或 `records` 数组的对象。

批量输出时，每篇写为 `<paper_id>.html`。单记录也可把 `--output` 指向一个 `.html` 文件。

## 输入契约

最少需要稳定 `paper_id`。正式发布前还必须核验英文题名；缺失时工具仍会生成说明页，但验证报告会留下 `metadata_pending` 警告。

```json
{
  "paper_id": "stable-paper-id-2026",
  "title": "Official English Title",
  "title_zh": "已核验的中文译名",
  "authors": ["Author One", "Author Two"],
  "year": 2026,
  "venue": "Conference",
  "doi": null,
  "arxiv_id": "2601.01234",
  "abstract": "论文原始摘要，只有已核验时填写。",
  "summary": "中文概述，只有有依据时填写。",
  "tags": ["video-token-compression"],
  "domain_slugs": ["video-llm-compression"],
  "source_urls": [
    {"label": "arXiv", "url": "https://arxiv.org/abs/2601.01234"}
  ],
  "pdf": {
    "path": "../papers/stable-paper-id-2026/paper.pdf",
    "sha256": "64位小写 SHA-256",
    "size": 123456,
    "page_count": 18,
    "version": "arXiv v1"
  },
  "analysis": {
    "problem": "研究问题。",
    "method": ["步骤一", "步骤二"],
    "results": ["带口径的实验结果"],
    "limitations": ["已核验的限制"],
    "relationships": ["与已有工作的关系"],
    "reproduction": ["复现检查项"]
  },
  "code": {
    "status": "official_local",
    "url": "https://github.com/org/repo",
    "local_path": "../code/upstream/stable-paper-id-2026/",
    "commit": "0123456789abcdef0123456789abcdef01234567",
    "license": "LICENSE 中核验的许可证",
    "evidence": ["论文项目页明确链接该仓库"]
  },
  "decision_evidence": ["身份或分类判断依据"],
  "warnings": []
}
```

`code.status` 允许值：

- `official_local`：官方代码已在核心领域内固定 commit 归档；
- `official_url_only`：已核验官方 URL，但尚未本地归档；
- `announced_pending`：官方宣布代码但尚未公开；
- `none_found`：截至审计时未发现可核验的公开官方代码；
- `third_party_only`：仅发现第三方实现；
- `not_applicable`：该记录不适用代码要求；
- `unknown`：尚未完成代码审计。

`none_found` 不能替代联网审计，也不能自动写入 Hub 无代码白名单。精确标题白名单、仓库资产和 `implemented_by` 关系仍由正式入库流程处理。

## 安全与真实性规则

- 所有记录值都以纯文本转义，不能把 JSON 中的 HTML/脚本注入页面。
- PDF `path` 和代码 `local_path` 只允许根内相对本地路径，不接受 HTTP URL；论文页/项目页/官方仓库 URL 才允许 `http://`/`https://`。
- 拒绝 `file:`、`javascript:`、`data:`、绝对本地路径、编码后的越界路径、重复 HTML 属性和内联事件处理器。
- 页面不加载远程 CSS、JavaScript、字体、图片或 iframe；外部论文/项目 URL 只是用户主动点击的普通链接。
- PDF 会实际核验 `%PDF-` 魔数、SHA-256、字节数和页数；`official_local` 代码必须是可读取 HEAD 的 Git 目录，且 HEAD 与完整 40/64 位 `commit` 一致。需要 Hub 虚拟环境中的 `pypdf` 和系统 `git`。
- 缺失字段统一显示 `待核验`，不会用文件名、相邻记录或模型常识补齐。
- 同一 JSON 与模板版本生成相同字节，便于 hash、复核和幂等检查。
- 验证器会检查 UTF-8、DOCTYPE、`zh-CN`、viewport、必需章节、锚点、本地链接、危险协议，以及 `src/srcset/poster/icon/SVG/CSS url/@import` 等远程子资源入口。

## 自测

```powershell
./.venv/Scripts/python.exe -m unittest discover `
  -s tools/paper_intake_html/tests `
  -p "test_*.py" -v
```

本工具的绿色结果只证明生成页面本身满足静态契约，不替代项目级：

```powershell
./.venv/Scripts/python.exe scripts/audit_html.py --report work/validation/html-audit.json --dynamic-samples 12
./.venv/Scripts/python.exe scripts/apply_html_policies.py --report work/validation/html-policy-apply.json
```

以及 Research Asset Hub 的来源完整性、单元按钮和浏览器打开验收。
