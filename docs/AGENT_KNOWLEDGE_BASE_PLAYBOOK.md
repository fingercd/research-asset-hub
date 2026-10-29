# 后续 Agent 知识库增量操作手册

本手册面向以后替用户操作 Research Asset Hub 的 Agent。目标不是“把文件扫进数据库”，而是让每次新增都有稳定身份、明确来源、可打开表示、可重建索引、可追溯关系和恢复路径。

任何后续 Agent 在新增、更新、合并、重新索引或登记 PDF、代码、导读、HTML 之前，都必须先阅读项目根目录 `AGENTS.md`、本手册、`docs/USER_KNOWLEDGE_BASE_GUIDE.md` 和 `docs/architecture/source-boundaries.md`。这些要求不是建议：来源只读、全量 dry-run、导入前备份和第二次幂等导入属于交付门禁，不能因时间不足或“只是少量文件”而跳过。

## 1. 完成定义

一次知识库新增只有同时满足以下条件才算完成：

- 用户提供的每个知识单元都有唯一、稳定的 `source_record_id`；
- 所有候选记录、PDF、代码、导读、HTML 和关系都有明确结局；
- 来源文件没有被 Hub 移动、重命名、覆盖或删除；
- dry-run 无错误、无重复来源键、记录数符合 manifest；
- 新出现的题名+年份弱合并全部经过显式审批，没有自动误合并；
- 正式导入可重复执行，第二次不产生重复资产、表示、关系或证据；
- 新内容已完成抽取和正式索引，失败项显式可见；
- 可以从桌面界面搜索到，并能安全打开登记的表示；
- 涉及领域时，默认详情为原生全页 v2 门户，固定九章节齐全、无 iframe/双滚动；原始 HTML 只作为可用性受控的次级核对入口；
- 来源完整性检查通过；
- 有导入前恢复点，并留下本次验证报告；
- 最终说明仍需用户手工完成的步骤，例如 Zotero 导入确认。

## 2. 先判断是哪种任务

本手册的 CLI 流程面向外部旁车来源维护，不替代日常界面托管上传。界面仅标题/PDF 必填，新领域仅名称必填；已有原位来源与冻结清单始终只读。精选九章节门户是单独的内容维护任务，不是建立空领域的前提。

| 情况 | 处理方式 |
| --- | --- |
| 新资料与已登记来源属于同一领域 | 不修改原来源；创建新的旁车 intake source，复用 domain slug |
| 新资料属于已有领域，但来自独立目录 | 新增 source，复用已有 domain slug |
| 全新研究领域 | 同时新增 taxonomy 和 source |
| 用户给出一个含 PDF、代码、HTML 的稳定目录 | 将其作为一个知识单元登记；默认不复制、不移动 |
| 用户给出 Downloads、Codex attachment、缓存或临时解压路径 | 不正式登记；先请用户指定永久位置 |
| 只有 URL、没有本地文件 | 可以登记 URL 表示，并明确其联网依赖 |
| manifest 结构规则且是 JSON | 优先 `generic_manifest` |
| 用户只增加一篇论文及 PDF、HTML、代码 | 新建一个 `generic_manifest` 旁车 source；不要使用批量 canonical 构建器 |
| 受支持的 VLN、游戏 RL、WSVAD 固定目录需要首次生成规范快照 | 使用 `scripts/build_canonical_snapshot.py`，输出到新的 `config/intake/<source-id>/manifest.json` |
| 现有 10 个领域的只读整体 HTML 需要登记为原始表示 | 使用 `scripts/build_domain_portal_snapshot.py`；不得改写原 HTML |
| 新增或更新统一领域门户内容 | 在 `config/domain-portals/v2/<slug>/` 维护固定八个内容文件并运行 v2 契约测试；不得从前端写死正文 |
| 多个相互依赖的 manifest 或存在特殊解析语义 | 新增专用 adapter，并补契约 fixture 与测试 |

开始前运行：

```powershell
git status --short
./scripts/hub.ps1 status
```

保留并绕开用户已有修改。不得清理来源目录、工作树或 Docker 卷。

所有已登记来源都保持只读，包括 manifest。用户明确要求 Agent 新增资料时，默认创建新的旁车 intake source；不得向任何现有来源 manifest 追加记录。若用户另行运行某个来源自己的采集/生成工程，那是独立任务，必须由该工程负责来源备份和版本化，再把冻结结果交给 Hub。

### 历史批量规范快照（2026-07 登记基线）

以下是当时 10 个领域、11 个只读 source 和 717 条规范化来源记录的历史基线，不代表当前全库数量。以下四个 `canonical_snapshot` 旁车已完成正式导入并冻结：

| Source | Domain / 用途 | 记录数 | 旁车 |
| --- | --- | ---: | --- |
| `vln-atlas-20260726` | `vision-language-navigation` | 72 | `config/intake/vln-atlas-20260726/manifest.json` |
| `game-rl-atlas-20260724` | `game-rl` | 72 | `config/intake/game-rl-atlas-20260724/manifest.json` |
| `wsvad-atlas-20260724` | `wsvad` | 45 | `config/intake/wsvad-atlas-20260724/manifest.json` |
| `domain-portals-20260726` | 10 个领域整体 HTML | 10 | `config/intake/domain-portals-20260726/manifest.json` |

现有四份旁车不得原地重建、追加或格式化。`canonical_snapshot` 是来源明确、字段可溯源的冻结派生层，不是“扫描整个目录”的通用导入器。普通用户新增资料仍使用 `generic_manifest`。

两个构建入口及其边界：

```powershell
./.venv/Scripts/python.exe scripts/build_canonical_snapshot.py --help
./.venv/Scripts/python.exe scripts/build_domain_portal_snapshot.py --help
```

- `build_canonical_snapshot.py` 要求 `--profile`、`--root`、`--source-id`、`--domain`、`--output`；profile 仅可为 `vln`、`game_rl`、`wsvad`。它只连接 profile 声明的固定 catalog，输出必须在只读来源根之外。
- `build_domain_portal_snapshot.py` 构建项目固定的 10 个门户，默认输出 `config/intake/domain-portals-20260726/manifest.json`，并拒绝输出到该 intake 目录之外。
- 两个脚本都只生成旁车，不修改来源 PDF、代码或 HTML。对已冻结输出只允许读和校验；若确需重新采集，必须新建来源/审计更正流程，不能覆盖现有 manifest。

## 3. 标准知识单元

永久资料根由当前用户选择（例如 `D:/ResearchLibrary`），与应用源码仓库分开。公共发行从空库开始，不附带下文历史批次的来源或原件；新增资料应在用户自己的配置中显式声明对应 `asset_roots`。

一个标准知识单元以论文或技术报告为主记录。原始大文件保留在用户指定的永久目录，Hub 项目只保存小型旁车 manifest：

```text
<用户永久资料目录>/
├─ paper.pdf
├─ code/
├─ analysis.html
├─ original.html
└─ note.md

research-asset-hub/
└─ config/
   └─ intake/
      └─ <source-id>/
         └─ manifest.json
```

文件不是必须全部存在；只有实际存在或明确提供 URL 的表示才应填写。模板：

- `docs/templates/knowledge-manifest.example.json`
- `docs/templates/knowledge-source.example.yaml`

### 稳定 ID

- 推荐格式：小写英文、数字和连字符，例如 `flashattention-2022`。
- 一旦导入不得因题名翻译、文件改名或目录调整而更换。
- 不得使用数组序号、下载时间或临时文件名作为永久 ID。
- 来源内由 `(source_id, source_record_id)` 唯一定位。

### 必要与推荐元数据

| 字段 | 要求 |
| --- | --- |
| `id` | 必填，来源内唯一且永久稳定 |
| `title` | 必填，优先论文正式原文题名 |
| `title_zh` | 推荐，明确是中文译名 |
| `authors` | 推荐使用字符串数组，保持正式顺序 |
| `year` | 推荐使用四位年份 |
| `venue` | 会议、期刊、机构或技术报告系列 |
| `doi` / `arxiv_id` | 有则必须保留，是跨来源合并的重要强标识 |
| `abstract` | 作者原始摘要；模型/系统卡可保存官方介绍，但 provenance 必须明确 `is_paper_abstract: false`，不要把 Agent 推测或导读写成原始摘要 |
| `summary` | 可放中文导读或人工概括 |
| `tags` | 使用稳定、可复用的标签，不堆叠同义词 |
| `source_urls` | 论文页、项目页等可核验来源 |
| `representations` | PDF、代码、导读、HTML、Markdown 的路径或 URL |
| `relations` | 仅指向同一 source 中其他稳定记录 ID；跨来源关系不得写入 manifest |

### 表示规则

- `pdf`：主论文 PDF，角色通常为 `original`。
- `repository`：代码目录或正式仓库 URL。正式 importer 会物化独立 repository 资产，并建立论文到仓库的 `implemented_by` 关系。
- `html`：新生成导读使用 `role: analysis`，历史原始页面使用 `role: original`；新入库不再创建 `guide` 表示，已有 guide 仅保留只读。原始网页与派生解读不可混为一个文件或一种证据。
- `markdown`：个人导读或结构化笔记，不替代原始论文。
- `notebook`：按 Notebook 表示登记，不要转成无法追踪单元格的纯文本。
- 缺失文件应登记为 `missing` 或不填写该可选表示；绝不能伪造一个存在路径。

记录中的路径必须相对于对应 `asset_roots`，统一使用 `/`。绝对路径只能出现在受审查的 `config/sources.yaml` 根配置中，不能进入 manifest 记录或开放导出。

### 关系引用的边界

模板里的 `relations.related_record_ids` 是 generic manifest 的来源内引用：每个值只能是**当前旁车 source 的同一份 manifest 中另一条 `items[].id`**。推荐的“一知识单元一旁车 source”通常保持空数组；只有一个旁车 source 明确包含多条记录时，才在这些记录之间使用它。

不要把现有资产 UUID、DOI/arXiv、外部仓库 URL 或其他 source 的 `source_record_id` 填入 `related_record_ids`。generic adapter 会按当前 source 解析 `source_record_id`；跨来源 ID 可能变成未解析候选，也可能因同名键碰撞而指向错误记录，不能用来表达已确认的跨来源关系。

若两个已导入资产属于不同 source，首次导入完成后用它们的真实 asset UUID 通过正式 API 建立关系，并留下关系证据。只有关系语义已经核对时才使用 `confirmed`；仍需复核的候选应保留 `candidate`。最小示例（将 UUID 换成已核对的真实值）：

```powershell
$payload = @{
  subject_asset_id = "11111111-1111-1111-1111-111111111111"
  object_asset_id = "22222222-2222-2222-2222-222222222222"
  relation_type = "related_to"
  source_key = "manual"
  confirmation_status = "confirmed"
  confidence = 0.8
  attributes = @{}
} | ConvertTo-Json -Depth 4

$relation = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8767/api/v1/relations" `
  -ContentType "application/json" `
  -Body $payload

$evidence = @{
  provider = "manual"
  source_key = "manual-review"
  evidence = @{ note = "核对题名、作者、年份和原文后确认关系。" }
  confidence = 0.8
} | ConvertTo-Json -Depth 4

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8767/api/v1/relations/$($relation.id)/evidence" `
  -ContentType "application/json" `
  -Body $evidence
```

API 会按 asset UUID 校验两个端点并记录审计事件；关系详情和 `/evidence` 端点可用于复核。不要为了表达跨来源关系而修改已冻结的来源 manifest。

## 4. 为新资料创建旁车来源

1. 确认用户给出的 PDF、代码和 HTML 已位于永久目录。
2. 选择新的稳定 source ID，例如 `intake-flashattention-notes-2026`。
3. 创建 `config/intake/<source-id>/manifest.json`；一篇论文 + PDF + HTML + 代码通常只使用一个 `items` 记录和 `generic_manifest` adapter。
4. 在 `config/sources.yaml` 新增 source 条目，`root` 指向该 intake 目录。
5. `asset_roots` 显式指向用户永久资料目录；manifest 内只写相对于该目录的路径。
6. 首次正式导入会自动把 manifest SHA-256 和 source 配置摘要冻结到 PostgreSQL。以后新增另一组资料时创建新 source，不改写旧旁车 manifest 或 source 条目。

冻结前必须确认 ID、领域、`expected_records`、路径和 adapter 映射已经最终确定。冻结后摘要变化会被正式导入和发布报告同时拒绝；如果后来发现历史元数据错误，应创建可追溯的更正来源或使用平台 override/审计流程，不能删除冻结字段或直接改库。

只检查目标来源：

```powershell
./scripts/hub.ps1 preflight
./scripts/hub.ps1 validate -Source <source-id> -Json
./scripts/hub.ps1 ingest -Source <source-id> -DryRun
```

随后刷新全来源 inventory 并执行全来源 dry-run，以发现跨来源 DOI/arXiv 或题名年份候选：

```powershell
./scripts/hub.ps1 inventory -All
./scripts/hub.ps1 ingest -All -DryRun
```

## 5. 新增来源或领域

### 新领域

先在 `config/taxonomy.yaml` 的 `domains` 中增加：

```yaml
- id: new-domain
  label: 新领域中文名
  aliases: [English alias, 中文别名]
```

`id` 必须满足 `^[a-z0-9][a-z0-9-]{1,63}$`，创建后保持稳定。前端不需要新增枚举或按领域写分支。

### 新来源

在 `config/sources.yaml` 中增加唯一 source 条目。普通 JSON 使用：

```yaml
- id: intake-new-paper-2026
  label: 个人新增 · 新论文
  domain: new-domain
  adapter: generic_manifest
  root: config/intake/intake-new-paper-2026
  manifest: manifest.json
  expected_records: 1
  read_only: true
  asset_roots:
    pdf: D:/ResearchAssets/new-paper-2026
    repository: D:/ResearchAssets/new-paper-2026
    html: D:/ResearchAssets/new-paper-2026
    note: D:/ResearchAssets/new-paper-2026
```

然后补齐 `asset_roots` 和 `options`。直接参考 `docs/templates/knowledge-source.example.yaml`，不要整份覆盖现有 `sources.yaml`。旁车 manifest 已明确登记的 HTML 仍会进入后续 HTML 审计；无需为这种单记录 intake 另加递归 `formal_html` 扫描。

旁车 manifest 根路径相对于项目根解析；外部 `asset_roots` 是受审查的稳定绝对目录。不要登记 Codex attachment、浏览器下载缓存、临时工作目录或会被其他工具自动清理的位置。

## 6. 合并决策是硬门禁

正式合并分两类：

- DOI/arXiv 相同：强标识候选，可由 importer 自动合并；
- 只有规范化题名和年份相同：弱标识候选，必须人工批准。

已批准弱合并保存在：

`config/identity-merge-approvals.yaml`

批准项同时锁定：

- `identity_key`；
- 完整的 `source_id + source_record_id` 集合。

如果新来源让已有候选组多出一条记录，原批准自动失效。dry-run 和正式导入都必须失败，直到 Agent：

1. 核对原文题名、作者、年份、摘要、DOI/arXiv 和来源 URL；
2. 明确判断是同一作品，而不是同名论文、版本或勘误；
3. 获得用户确认；
4. 只对这一确切记录集合更新 allowlist；
5. 重新运行全来源 dry-run。

不得通过删除候选、改假题名、虚构 DOI 或修改 importer 来绕过门禁。

## 7. 正式导入

先检查 `work/validation/ingest-dry-run.json`：

- `status` 为 `passed`；
- `errors` 为空；
- `duplicate_source_record_keys` 为空；
- 每个来源 `expected_count_matches` 为 true；
- `unapproved_review_groups` 为空；
- 新增表示路径、URL、类型和角色正确；
- 所有 warning 已解释。

不要只看顶层 `status`：当前 preflight/validate 对 `expected_records` 不匹配可能只给 warning，Agent 必须逐来源确认 `expected_count_matches`/`record_count_matches` 为 true。外部 `asset_roots` 也必须结合 inventory 和 dry-run 的 `asset_path_missing` 等 warning 核对；缺失项只能等于事先明确接受的缺失清单。

正式写库前创建恢复点：

```powershell
./.venv/Scripts/python.exe scripts/create_backup.py `
  --report work/validation/backup-before-ingest.json
```

平台 Restic 快照保护数据库、用户配置与托管资料；外部只读引用原件默认不包含，显式纳入时也须核对报告实际范围。外部来源仍需另行文件级备份；不得把快照存在、存储校验通过和数据库恢复演练通过混为一谈。

正式导入只允许使用全部来源，保证跨来源身份视图完整：

```powershell
./scripts/hub.ps1 ingest -All
```

CLI 会拒绝不带 `DryRun` 的 `ingest -Source`，避免单来源正式导入绕过跨来源合并门禁。

`ingest -All` 在数据库写入前会读取已审计的标识拒绝策略并重跑完整 dry-run；策略不可读时正式导入停止。离线文件 dry-run 仍可查看原始声明，但不能替代正式路径的策略检查。正式 importer 会再次强制每个 source 的 `expected_records`、适配器实际输出的 taxonomy domain、弱合并审批和冻结摘要，因此不能靠跳过手工命令绕过这些检查。成功报告必须为 schema v2，并包含 `validated_dry_run.status: passed`。

检查 `work/validation/ingest-formal.json` 后，在不改变输入的情况下再次执行：

```powershell
./scripts/hub.ps1 ingest -All
```

第二次的以下新增计数必须为 0：

- `source_records_created`
- `source_record_versions_created`
- `paper_assets_created`
- `repository_assets_created`
- `representations_created`
- `relations_created`
- `relation_evidence_created`
- `field_claims_created`

如果不是 0，先解释输入变化或修复幂等问题，不能继续索引并宣称完成。

## 8. 抽取、HTML 与索引

原生桌面监督器统一管理 worker，不再有 `hub start`。登记变化后排队：

```powershell
./.venv/Scripts/python.exe scripts/seed_jobs.py
```

在桌面界面“后台任务”页等待 `pending/running` 清零。失败任务必须查看错误和表示状态；缺失源文件可以是明确 warning，但不能消失。

新增 HTML 时：

```powershell
./.venv/Scripts/python.exe scripts/audit_html.py `
  --report work/validation/html-audit.json `
  --dynamic-samples 12
./.venv/Scripts/python.exe scripts/apply_html_policies.py `
  --report work/validation/html-policy-apply.json
```

构建正式索引：

```powershell
./.venv/Scripts/python.exe scripts/build_indexes.py `
  --version v1 `
  --report work/validation/index-build.json
```

正式完成条件：

- serving scope 选中的关键词索引（默认 `assets/annotations`）计数与报告一致；
- 新增资产存在于 assets 索引；
- 资产可搜索文本仅由标题、中文题名和摘要组成；DOI/arXiv 只用于必要的精确标识匹配，不混入正文、summary 或标签；
- 正文抽取保留在操作主库供按需读证据，不默认维护或查询 documents/code 全文索引；只有明确指定 `--index documents` 或 `--index code` 时才维护这些历史索引；
- 不存在所选范围中的失效服务文档。API 默认 `HUB_SEARCH_CODE_INDEX_ENABLED=false`，代码仓库仍可安全浏览和按行读取。

发现登记 PDF 实际属于另一篇论文时，保留旧文件和冻结声明，另建正确原文的旁车来源。原错误表示通过 `GET/PATCH /api/v1/representations/{id}/serving` 设置独立审计的 `excluded`，提交登记 SHA、当前 ETag、幂等键与原因；恢复同样必须显式审计。错误 DOI/arXiv 身份通过 `GET/PATCH /api/v1/external-identifiers/{id}/identity` 拒绝，先核对预期资产、scheme/scope/canonical 和绑定的来源稳定键。两项控制都不改写原始 source/manifest，也不会被普通重新导入解除；不得用直接改库代替。

## 9. 验证用户真正能用

来源检查：

网关健康只反映数据库与签名配置是否就绪；知识库状态中的根目录信息只表示注册情况，`source_files_verified: false` 明确表示未扫描文件。不要用冻结来源中的全部历史目录是否存在推断当前库可读性。真实表示缺失仍需从 availability、导入检查与实际打开失败报告中处理；保留冻结来源和 manifest，不为消除历史目录告警改写证据。

```powershell
./.venv/Scripts/python.exe scripts/verify_source_integrity.py
```

桌面界面检查：

1. 在桌面“全部资料/搜索”用题名、DOI/arXiv 和作者搜索；只有明确启用代码索引时才测试代码符号搜索。
2. 打开资产详情，核对题名、作者、年份、领域、标签和表示。
3. 分别测试 PDF、代码、导读和原始 HTML；缺失按钮必须明确禁用。
4. 检查代码作为独立 repository 资产，并有正确 `implemented_by` 关系。
5. 读取并核对阅读状态与批注摘要。只有用户本来就要求写入阅读状态或批注时才做写入验证，不得为了 smoke test 污染用户数据。
6. 检查相似论文和图谱邻域能定位到有效锚点。
7. 涉及领域或门户时，从首页确认领域动态出现，并验证：
   - `/api/v1/domains/<slug>/portal` 返回 v2、九章节、`stale: false`；
   - 默认页面存在 `article[data-domain-portal]`，没有 iframe、第二层滚动或横向溢出；
   - 桌面和 390px 窄屏均可阅读，目标页面 axe 无关键违规；
   - “研究单元”能分页到末尾，“最近更新”和“统计”取正式库数据；
   - “原始 HTML”只在表示可用时启用，并经 `127.0.0.1:8766` 网关新窗口打开。

刷新开放目录：

```powershell
./.venv/Scripts/python.exe scripts/export_catalog.py
```

确认 `outputs/open-catalog/manifest.json` 中来源二进制复制数为 0。

需要外部工具时：

```powershell
./.venv/Scripts/python.exe scripts/sync_obsidian.py --preview-only
./.venv/Scripts/python.exe scripts/sync_obsidian.py
./.venv/Scripts/python.exe scripts/export_zotero.py
```

Obsidian 冲突必须进入冲突队列。Zotero 只生成审查式 CSL JSON/BibTeX 包，Agent 不得写 Zotero SQLite；最终导入需要用户界面确认。

## 10. 测试、备份与交付

至少运行：

```powershell
./.venv/Scripts/ruff.exe check apps packages scripts tests
./.venv/Scripts/mypy.exe --strict apps packages scripts
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe scripts/verify_source_integrity.py
pnpm --filter @research-asset-hub/desktop-ui lint
pnpm --filter @research-asset-hub/desktop-ui typecheck
pnpm --filter @research-asset-hub/desktop-ui test -- --run
pnpm --filter @research-asset-hub/desktop-ui build
```

涉及桌面界面或打开链路时，在隔离资料库的原生窗口验收；论文 HTML 的只读浏览器审计继续使用 Playwright/axe，不再运行已退役的产品网站 E2E。涉及索引时重跑相应固定评测。

批量导入、新来源、新领域或准备发布时，创建最终快照并做隔离恢复演练：

```powershell
./.venv/Scripts/python.exe scripts/create_backup.py `
  --report work/validation/backup-final.json
./.venv/Scripts/python.exe scripts/restore_rehearsal.py `
  --snapshot <snapshot-id> `
  --run-id <unique-run-id>
./.venv/Scripts/python.exe scripts/generate_release_report.py `
  --restore-report work/restore-tests/<unique-run-id>/report.json
```

只有 release report 中 `fail=0` 才能交付。`local-copy-only`、已解释的缺失文件或旧 HTML 依赖可以是显式 warning。

## 11. Agent 最终回复模板

最终回复至少包含：

- 新增、更新、合并的论文和仓库数量；
- 表示候选、可用、缺失和失败数量；
- 新增关系及未解析关系；
- dry-run 和第二次导入的幂等结论；
- 抽取任务与索引状态；
- 来源完整性结果；
- 桌面界面中已验证的搜索与打开方式；
- 涉及领域门户时，10/10 或目标领域的九章节、stale、响应式、无 iframe、原始 HTML 门禁和研究单元结果；
- 备份 snapshot ID 和恢复演练状态；
- 用户仍需手工执行的 Zotero、密钥轮换或外部备份步骤；
- 相关报告和文档的绝对可点击路径。

不能只说“已导入”或“测试通过”，也不能隐藏 warning。
