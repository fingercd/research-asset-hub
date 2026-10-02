# 离线论文元数据与代码线索抽取器

这个工具递归读取 PDF 前若干页、PDF 内嵌 Document Info/XMP 元数据和链接注释，输出可审计的论文身份候选、书目信息与代码线索。它不联网、不修改输入 PDF，也不把低置信候选写成事实。

## 输出语义

- `<prefix>.jsonl`：追加式断点日志。每行是一份 PDF 的完整抽取记录；源文件内容 SHA-256 或抽取参数变化时追加新修订。日志保留此前运行的历史路径。
- `<prefix>.csv`：每次运行后从 JSONL 重建，但只包含**本次 inputs 当前发现的路径**及其最新记录，不会把此前运行中现已不在扫描范围的路径混进快照；使用 UTF-8 BOM，方便 Excel 打开。
- `candidates`：全部候选，均带 `confidence`、`reason` 和具体 `evidence`。
- `facts`：只有置信度达到 `--fact-threshold`（默认 `0.75`）的候选。CSV 的题名、作者、年份、venue、DOI、arXiv 和摘要列也只来自 `facts`。
- `code_leads`：GitHub/GitLab 仓库、GitHub Pages 项目页和“code available”文字线索。它们永远标记为 `candidate_unverified`；离线发现 URL 不能证明仓库官方性、论文对应关系、许可证或可复现性，后续仍需联网核验。
- `status=error`：文件仍会留下错误记录，不会静默跳过。

题名文件名、PDF 创建日期、未核验的首页行等证据置信度低于默认事实门槛，因此只保留为候选。DOI/arXiv 需要严格边界和可信证据语义：随机数字、文件名中的 arXiv 字样、非 DOI/arXiv 主机里的伪造链接、后页参考文献链接都不会直接升级为事实。作者、题名优先使用内嵌元数据，首页启发式结果不会单独升级为事实；高置信单值字段互相冲突时也不会选一个强行写入事实。

所有输出 URL 都会删除 userinfo、端口、query 和 fragment，避免把密码、签名参数或 token 带入报告。仓库 host 仅接受严格的 `github.com` / `gitlab.com` 边界，项目页仅接受 `*.github.io`；相似恶意域名不会被识别为代码线索。

## 使用

在 `research-asset-hub` 根目录运行：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_metadata `
  "C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向" `
  --output-prefix "D:\ResearchLibrary\work\validation\desktop-paper-metadata"
```

也可以同时传入多个 PDF 或目录：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_metadata paper-a.pdf paper-b.pdf .\papers `
  --output-prefix .\work\validation\paper-metadata `
  --first-pages 4 `
  --fact-threshold 0.80
```

常用选项：

- `--first-pages N`：读取前 N 页正文，默认 3；链接注释仍会遍历全部页面。
- `--max-text-chars N`：限制前页正文总字符数，默认 120000。
- `--fact-threshold 0..1`：控制哪些候选可以出现在 `facts`。
- `--force`：即使文件与参数未变，也追加一次新修订。
- `--overwrite`：清空既有 JSONL 后重建；这会丢弃该输出日志的历史，只应对明确的重跑使用。

显式输入本身若是符号链接、Windows junction 或其他 reparse point，工具会在解析目标前拒绝；递归扫描也不会跟随这些路径，并跳过 `.git`、虚拟环境、依赖、缓存、权重和 checkpoint 目录。断点跳过会在 size/mtime 相同后再次核对 SHA-256，不会仅凭时间戳沿用旧结果。JSONL 尾部只有在“最后一行没有换行且 JSON 不完整”时才按进程中断修复；带正常换行的损坏行和任何中间损坏都会硬失败并报告行号。

## 小样本自测

```powershell
.\.venv\Scripts\python.exe -m pytest tools\paper_metadata\test_extract.py -q
.\.venv\Scripts\ruff.exe check tools\paper_metadata
```

测试生成临时 PDF，覆盖内嵌题名/作者、DOI、arXiv、PDF URI 注释、低置信摘要门禁、伪造标识和恶意 URL、代码线索的未核验状态、SHA-256 断点核验、reparse 拒绝、逐文件错误、当前扫描 CSV 语义及损坏尾行恢复；不会读取或修改真实论文。
