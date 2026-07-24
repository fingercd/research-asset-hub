# Research Asset Hub 后续 Agent 工作规范

本文件是本项目内后续 Agent 的强制执行约定。只要任务涉及新增、更新、删除、合并或重新索引论文、PDF、代码、Notebook、Markdown、导读或 HTML，必须先完整阅读：

1. `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md`
2. `docs/USER_KNOWLEDGE_BASE_GUIDE.md`
3. `docs/architecture/source-boundaries.md`

## 不可破坏的不变量

- 所有已经登记的来源根、来源 manifest 和表示文件都是冻结的只读证据。Hub 与 Agent 均不得改写、移动、重命名、覆盖或删除它们。用户交给 Agent 的新资料默认通过 `config/intake/<source-id>/manifest.json` 旁车清单登记，原 PDF、代码和 HTML 保持原位。
- 每个 source 首次正式导入时，系统会把 manifest SHA-256 和 source 配置摘要冻结到 PostgreSQL；以后任一摘要变化都会使正式导入和发布门禁失败。首次导入前必须把 ID、领域、路径、条目数和映射检查完毕；发现历史错误时创建更正来源并走审计流程，不得删除冻结字段或直接改库绕过。
- 不得把来源大文件复制进 `research-asset-hub`。平台只保存来源别名、受控根目录、相对路径、指纹、抽取结果和用户状态。
- 不得直接修改 PostgreSQL、Meilisearch、Qdrant 或 Zotero SQLite 来绕过正式导入、API、同步器或迁移。
- PostgreSQL 是操作主库；Meilisearch、Qdrant 和抽取缓存必须保持可重建。
- 路径必须来自 `config/sources.yaml` 中声明的来源根或 `asset_roots`。manifest 记录中的路径必须是相对路径，不得包含绝对盘符、空段、`.`、`..` 或 NUL。
- 一个知识单元必须有永久稳定且在来源内唯一的 `source_record_id`。它是独立的来源键，不等于 DOI、arXiv、题名或文件名；这些只作为外部标识和身份匹配证据。
- generic manifest 的 `relations.related_record_ids` 只能引用当前旁车 source 同一份 manifest 中其他 `items[].id`；不得填资产 UUID、DOI/arXiv 或其他 source 的记录 ID。推荐一知识单元一旁车 source；跨来源关系须在首次导入后用真实 asset UUID 通过正式 `/api/v1/relations` 建立，并用 relation evidence 留下审计记录。
- DOI、arXiv、正式仓库 URL 等强标识必须尽量保留；不得为了消除重复而凭模糊题名自动强行合并。
- 缺失、跳过、候选、拒绝和失败都必须在报告或网站中可见，不得静默丢弃。
- 任何写操作都要保留用户已有改动；开始前运行 `git status --short`，不得重置或覆盖无关修改。
- `.env`、API key、Windows 凭据、token、认证缓存和私人正文不得写入提交、日志或交付报告。

## 收到“把这组 PDF、代码、HTML 加入知识库”时

先把用户提供的材料归一为一个“知识单元”：

- 主记录：论文、技术报告或其他研究对象；
- 表示：PDF、代码目录/仓库、Notebook、导读 HTML、原始 HTML、Markdown 笔记等；
- 身份：独立稳定的 `source_record_id`，以及 DOI/arXiv 等外部标识、正式题名、作者和年份证据；
- 归属：已有领域和来源，或新的稳定领域与来源；
- 关系：代码是否实现该论文、与哪些已登记记录相关。

能够从文件和 manifest 安全确定的信息应自行提取。只有以下情况才需要向用户询问：

- 材料位于下载目录、临时附件目录或其他不适合长期引用的位置；
- 无法判断它应更新已有记录还是创建新记录；
- 同一强标识指向冲突内容；
- 需要移动、覆盖、删除来源文件，或需要扩大到用户未授权的目录；
- 新领域名称、隐私级别或关系语义会实质改变结果。

默认处理顺序：

1. 材料已经登记：复用现有资产，不改来源；阅读状态、批注等用户状态通过网站/API 更新。
2. 新材料位于稳定目录：创建新的 `config/intake/<source-id>/manifest.json` 和只读 source 条目，优先使用 `generic_manifest`。
3. 材料位于临时目录：停止正式登记，请用户先选择永久位置；不得把临时路径写入主库。
4. 复杂多 manifest：只有通用适配器无法无损表达时才新增专用适配器，并补契约测试。

## 每次正式新增必须完成

1. 只读检查：`preflight`、`validate`、`inventory`。正式 CLI 还会在写库前自动重跑完整 dry-run；记录数、弱合并、来源声明和适配器实际输出领域均为硬门禁。
2. 对目标来源执行 dry-run，检查记录数、路径、身份候选、表示和关系结局。
3. 正式导入前建立可恢复备份；小范围只读演练可不写数据库。
4. 正式导入后，用未改变的输入再导入一次；第二次不得创建重复资产、表示或关系。
5. 为新增/变化表示排队抽取，等待任务收敛；失败必须解释。
6. 重建正式关键词/向量索引；不得把 hashing 降级伪装成 BGE-M3 正式索引。
7. 有 HTML 时完成静态审计、策略应用和必要的浏览器抽样。
8. 运行来源完整性校验，确认已登记来源没有被平台修改。
9. 在网站中验证：可以搜索到、详情元数据正确、PDF/代码/导读/HTML 可以安全打开、关系和阅读状态正常。
10. 刷新开放目录；按用户需要同步 Obsidian、生成 Zotero 包。
11. 运行与改动相称的测试，并把报告留在 `work/validation/`。
12. 完成报告必须列出：新增/更新/合并/缺失/失败数量、幂等结果、索引状态、来源完整性、备份 ID、未完成人工步骤。

详细命令、判断标准和异常处理见 `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md`。不得仅因为命令退出码为 0 就宣称完成；上述可见性和打开链路也必须验证。
