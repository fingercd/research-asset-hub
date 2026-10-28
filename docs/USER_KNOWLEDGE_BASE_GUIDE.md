# 用户自行增加知识库指南

这份指南说明如何把一篇论文及其 PDF、代码、HTML 导读、Notebook 或 Markdown 笔记作为一个完整知识单元加入 Research Asset Hub。

**日常单篇优先使用桌面“添加资料”**：标题和 PDF 必填，HTML、代码及简介选填；界面确认后可将新材料托管复制到用户数据目录。新领域也只需名称，精选门户内容可后补。

本文后续命令主要说明 **Agent 为已有永久资料创建外部旁车来源** 的方式：引用原文件位置，不搬运或覆盖原件。因此，执行这一路径前要先把资料放在长期稳定、不会自动清理的位置。它不是普通用户上传一篇 PDF 必须执行的命令清单。

当前 PDF 与代码的永久资料根是 `D:/ResearchLibrary`，各领域资料放在其子目录中；`research-asset-hub` 子目录保存应用。以后新增资料继续使用这个资料根，并为新资料建立独立旁车来源。

平台 Restic 备份包含数据库、用户配置和托管资料；**外部只读引用原件默认不包含**，需核对实际备份范围并单独保护。快照存储校验通过不等于数据库恢复演练通过；Git 不能备份个人数据库、未跟踪 PDF 或密钥。

普通的一篇论文使用 `generic_manifest` 旁车，不需要运行 VLN、游戏强化学习、WSVAD 或领域门户的批量快照构建器。完整写库顺序固定为：全量 dry-run 通过、创建备份、第一次全量正式导入、在输入不变时第二次全量正式导入并确认新增计数为 0。

## 1. 最省事的方法：直接告诉 Agent

你可以把下面这段作为以后给 Agent 的请求模板：

```text
请把下面这个知识单元加入 Research Asset Hub，并严格遵守项目根目录
AGENTS.md 和 docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md。

资料永久目录：<绝对路径>
所属领域：<已有领域名，或希望新建的领域>
主论文/报告：<PDF 路径或题名>
代码：<代码目录或 Git 仓库 URL，没有则写无>
导读 HTML：<路径，没有则写无>
原始 HTML：<路径，没有则写无>
Markdown/Notebook：<路径，没有则写无>
DOI/arXiv：<知道就填，不知道可让 Agent 提取>
与已有资料的关系：<知道就填，不知道可留空>

完成 dry-run、备份、正式导入、幂等复跑、抽取、索引、来源完整性和桌面界面打开验证；
不要移动、重命名、覆盖或删除我的原文件。
```

通常只要给出“永久目录、所属领域、哪一个是主 PDF”，其他元数据 Agent 可以从文件、网页和已有 manifest 中提取。以下情况 Agent 会暂停询问：

- 路径属于 Downloads、临时附件或缓存；
- 无法确定是已有论文的新表示，还是一篇新论文；
- 出现可能误合并的同名论文；
- 需要改动你没有明确授权的来源目录。

## 2. 什么是一个知识单元

推荐把属于同一篇论文或报告的内容放在同一个稳定目录。Hub 不要求你在该目录中创建 manifest：

```text
example-paper-2026/
├─ paper.pdf
├─ code/
├─ analysis.html
├─ original.html
└─ note.md
```

- `paper.pdf`：原始论文。
- `code/`：本地代码快照；也可以只提供 GitHub URL。
- `analysis.html`：你或 Agent 生成的导读页面，新资料按 HTML / analysis 登记，不新建已废除的 guide 表示；旧 guide 保留只读。
- `original.html`：需要保留原交互的历史网页。
- `note.md`：可搜索的 Markdown 导读或笔记。

文件名可以不同，重点是旁车 manifest 中的路径必须正确。不要把依赖环境、`.git`、缓存、模型权重、训练数据或无关构建输出当成论文正式表示。

一篇论文 + PDF + HTML + 代码的最短闭环是：

1. 将已有 `paper.pdf`、`code/`、`analysis.html` 或 `original.html` 的永久路径登记清楚；不要为匹配示例而移动已登记文件。
2. 新建 `config/intake/<new-source-id>/manifest.json`，其中只放这一条稳定 `items[].id` 记录。
3. 在 `config/sources.yaml` 新增 `read_only: true`、`adapter: generic_manifest` 的 source，并让 `asset_roots` 指向永久资料目录。
4. 运行全来源检查和 dry-run，核对 PDF、HTML、代码路径及身份合并候选。
5. 备份后执行两次 `ingest -All`；第二次不得新增任何记录、资产、表示、关系或字段声明。
6. 完成抽取、HTML 审计、索引和桌面界面打开验证。

## 3. 先决定加入哪里

### 加入已有领域

复用已有 domain slug，但不要修改任何已登记来源目录或 manifest。为这组新资料创建一个新的旁车 source。

### 建立旁车来源

适合：你交给 Agent 一组位于永久目录的 PDF、代码和 HTML。

原文件放在项目之外的稳定资料区，例如：

```text
D:\ResearchLibrary\个人新增资料\example-paper-2026
```

把 manifest 模板复制到项目的：

```text
research-asset-hub\config\intake\intake-example-paper-2026\manifest.json
```

可以参考：

- `docs/templates/knowledge-manifest.example.json`
- `docs/templates/knowledge-source.example.yaml`

模板只是参考，不要用它覆盖现有 `config/sources.yaml`。旁车 source 首次正式导入时，系统会自动冻结 manifest 和 source 配置摘要；下一组资料使用新的 source ID 和 intake 目录。

### 普通旁车与规范快照不要混用

当前 `config/intake/` 同时保存两类小型旁车：

| 类型 | 用途 | 入口 |
| --- | --- | --- |
| 普通 `generic_manifest` | 用户新增一篇或少量论文、PDF、HTML、代码 | 手工按模板创建新的 `config/intake/<source-id>/manifest.json` |
| `canonical_snapshot` | 项目已定义的批量来源和领域整体 HTML 门户 | 由确定性构建脚本生成，正式导入后冻结 |

当前已验证的批量旁车包括：

- `config/intake/vln-atlas-20260726/manifest.json`：VLN 72 条；
- `config/intake/game-rl-atlas-20260724/manifest.json`：游戏强化学习 72 条；
- `config/intake/wsvad-atlas-20260724/manifest.json`：WSVAD 45 条；
- `config/intake/domain-portals-20260726/manifest.json`：10 个领域的整体 HTML。

构建脚本可以用 `--help` 查看固定参数：

```powershell
./.venv/Scripts/python.exe scripts/build_canonical_snapshot.py --help
./.venv/Scripts/python.exe scripts/build_domain_portal_snapshot.py --help
```

`build_canonical_snapshot.py` 只支持 `vln`、`game_rl`、`wsvad` 三个固定 profile，并且只连接 profile 明确声明的目录，不递归扫描整个来源或 `work/`。`build_domain_portal_snapshot.py` 只生成项目定义的 10 条领域门户记录。它们的输出在 `config/intake/`，原 PDF、代码和 HTML 保持原位、只读。

这四份现有旁车已经过正式导入并冻结；不要原地重建、追加或修改。只有在新 source 首次正式导入之前，才可以为受支持的批量 profile 生成一个新的输出并完整核对。普通单篇论文始终使用前述 `generic_manifest` 流程。

## 4. 填写 manifest

每个 `items` 条目是一篇主论文或报告：

```json
{
  "id": "example-paper-2026",
  "title": "Example Paper Title",
  "title_zh": "示例论文标题",
  "authors": ["First Author", "Second Author"],
  "year": 2026,
  "venue": "Example Venue",
  "doi": null,
  "arxiv_id": null,
  "tags": ["example-topic"],
  "representations": {
    "pdf": {"path": "paper.pdf"},
    "code": {
      "path": "code",
      "url": "https://github.com/example/example-repository"
    },
    "html": {"path": "analysis.html"},
    "original_html": {"path": "original.html"},
    "note": {"path": "note.md"}
  }
}
```

规则：

- `id` 一旦使用不要更换。
- `title` 使用正式原文题名；`title_zh` 是中文译名。
- DOI 和 arXiv 有则一定填写，能减少重复。
- 不存在的可选表示应删除对应 path 或设为 `null`，不要编造路径。
- 路径相对于 source 条目中声明的 `asset_roots`，使用 `/`，不能写盘符或 `..`。
- URL 只能使用 `http://` 或 `https://`。
- `source_record_id` 是独立稳定键；DOI/arXiv 是外部标识，不能互相替代。
- `relations.related_record_ids` 只能填写当前旁车 source 的同一份 manifest 中其他 `items[].id`；不要填写资产 UUID、DOI/arXiv 或其他 source 的记录 ID。推荐一知识单元一旁车 source，因此通常保持空数组。
- 旁车 source 首次导入后不再增加条目；新的知识单元创建新 source。

如果要连接两个已经导入、且来自不同 source 的资产，先完成导入，再用两个真实 asset UUID 调用正式 API；不要把跨来源 ID 塞回 manifest。只有关系语义已经核对时才使用 `confirmed`；仍需复核的候选应保留 `candidate`。最小请求示例（替换 UUID，并先核对关系语义）：

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

关系创建和证据写入都会进入 API 审计；可在 API 文档 `http://127.0.0.1:8767/docs` 查看关系与 evidence 端点。若只是同一新 source 内多条记录之间的关系，才使用 manifest 的 `related_record_ids`。

## 5. 注册新领域和来源

如果是全新领域，在 `config/taxonomy.yaml` 的 `domains` 下增加：

```yaml
- id: example-research
  label: 示例研究领域
  aliases: [example research]
```

然后把 `docs/templates/knowledge-source.example.yaml` 中 `sources` 下的单个条目复制到 `config/sources.yaml` 的 `sources` 列表，修改：

- `id`：唯一来源名；
- `label`：桌面界面显示名；
- `domain`：taxonomy 中的领域 ID；
- `root`：`config/intake/<source-id>` 旁车目录；
- `asset_roots`：原始资料所在的稳定绝对目录；
- `expected_records`：manifest 中实际条目数。

如果只是为已有领域增加一个新来源，复用已有 `domain`，不需要再建 taxonomy 项。

### 如果增加的是一个全新领域

新领域不必先具备精选门户即可使用。需要补齐**精选九章节门户**时，再建立 `config/domain-portals/v2/<slug>/`，把内容拆成以下八个可审查文件：

- `portal.yaml`：领域说明、内容版本、原始 HTML 选择器、来源 hash/mtime 和章节证据；
- `overview.md`：领域定位与边界；
- `timeline.yaml`：发展阶段；
- `capability-method-map.yaml`：能力、方法和交叉单元；
- `landmarks.yaml`：代表工作；
- `benchmarks.yaml`：任务、数据集、指标和比较注意事项；
- `reading-routes.yaml`：3–5 条推荐阅读路线；
- `open-questions.md`：前沿趋势和开放问题。

同时在 `config/domain-portals/v2/portal-index.yaml` 登记 slug、目录、原始 HTML selector 和允许的虚拟 mount。每个门户必须按同一顺序产生九个页面章节：领域定位、论文/代码/导读目录、时间线、能力与方法、代表工作、评测、阅读路线、前沿、证据。论文数量、阅读状态和完整研究单元目录不写进 YAML，而由 PostgreSQL 动态生成；统计以 `paper_count` 为论文主口径，`repository_count` 与 `asset_count` 只作独立辅助统计，不能相加后称作论文数。

如果只是给已有领域增加论文，不需要改门户文件；导入和索引完成后，统计与目录会自动更新。只有新资料改变了该领域的发展脉络、阅读路线或开放问题时，才更新对应内容文件并提高 `content_version`。更新后运行：

```powershell
./.venv/Scripts/python.exe -m pytest tests/contract/test_domain_portal_v2.py -q
pnpm --filter @research-asset-hub/desktop-ui typecheck
pnpm --filter @research-asset-hub/desktop-ui test
```

在原生窗口验收时确认默认页面没有 iframe；“原始 HTML”只是只读次级入口。

## 6. 检查后再正式导入

在项目目录打开 PowerShell：

```powershell
cd "D:\ResearchLibrary\research-asset-hub"
./scripts/hub.ps1 preflight
./scripts/hub.ps1 validate -All -Json
./scripts/hub.ps1 inventory -All
./scripts/hub.ps1 ingest -All -DryRun
```

`DryRun` 本身只需要 Python、配置和可读来源。需要正式导入时先正常打开桌面程序，让唯一监督器提供数据库与后台任务；只检查 manifest 时不必启动桌面。

日常入口是原生桌面。API 文档与资产网关是维护用的本机服务，不再提供独立网站入口。

网关健康表示数据库与签名配置就绪；知识库状态只汇总表示台账和来源根注册情况，不扫描全部 PDF 或代码。判断某份资料能否阅读，要看其表示状态和实际打开结果；缺失文件仍会返回明确错误。

打开 `work/validation/ingest-dry-run.json`，确认：

- `status` 是 `passed`；
- `errors` 是空数组；
- 实际记录数等于 `expected_records`；
- 没有重复 `source_record_id`；
- PDF、代码和 HTML 的候选数量与预期及明确缺失清单相符；
- `unapproved_review_groups` 为空。

不要只看顶层 `status`。当前 preflight/validate 遇到 `expected_records` 不匹配时可能只显示 warning，所以还要逐来源确认 `expected_count_matches` 或 `record_count_matches` 为 true。外部 `asset_roots` 是否可读则要看 inventory/dry-run 中的 `asset_path_missing` 等 warning；任何缺失都必须是你事先知道并接受的项目。

如果出现未批准的题名年份合并，不要直接正式导入。它表示系统怀疑两个来源是同一篇论文，需要先核对 DOI、作者、年份和摘要。

## 7. 备份、导入和幂等确认

正式写入前：

```powershell
./.venv/Scripts/python.exe scripts/create_backup.py `
  --report work/validation/backup-before-ingest.json
```

正式导入：

```powershell
./scripts/hub.ps1 ingest -All
```

检查 `work/validation/ingest-formal.json` 后，在 manifest 和来源文件完全未变化的前提下，再执行一次同样命令：

```powershell
./scripts/hub.ps1 ingest -All
```

第二次新增的论文、仓库、表示、关系和来源版本都应为 0。这一步用于确认没有重复导入；两次之间必须保持 manifest、source 配置和来源文件不变。

注意：新旁车一旦完成第一次正式导入，修改 manifest 或它在 `config/sources.yaml` 中的配置会被硬门禁拒绝，不会再创建“预期的新版本”。因此首次导入前要把 ID、领域、条目数、路径和映射检查完毕；以后新增资料应建立新 source。正式命令本身也会先自动重跑全来源 dry-run，并再次检查预期记录数、领域、弱合并和冻结摘要。

## 8. 按需读正文并维护标题摘要搜索

原生桌面启动后，监督器会在后台运行 worker。创建增量任务：

```powershell
./.venv/Scripts/python.exe scripts/seed_jobs.py
```

在桌面系统状态中打开“后台任务”，等“等待中”和“运行中”任务归零。失败任务会明确显示；不要因为其他资产成功就忽略失败项。

构建正式索引：

```powershell
./.venv/Scripts/python.exe scripts/build_indexes.py `
  --version v1 `
  --skip-code `
  --report work/validation/index-build.json
```

当前默认只维护资产和批注索引。论文匹配使用标题、中文题名和摘要，保留 DOI/arXiv 精确查询；正文、中文导读和标签不会因为出现某个词就让论文命中。MCP 返回候选总数与下一页位置，广召回结果仍需回到原文判断是否满足条件。

正文与代码已有内容可以按需读取并附页码或行号，不需要先把全库复制成 Markdown。`--index assets` 可单独刷新标题摘要投影；默认 `--skip-code` 保留代码浏览，但不建立代码全文索引。只有明确要求相关全文维护时才使用 `--index documents` 或 `--index code`。

有 HTML 时再执行：

```powershell
./.venv/Scripts/python.exe scripts/audit_html.py `
  --report work/validation/html-audit.json `
  --dynamic-samples 12
./.venv/Scripts/python.exe scripts/apply_html_policies.py `
  --report work/validation/html-policy-apply.json
```

HTML 会按静态、可信本地交互或需要联网三种策略隔离打开。缺少本地脚本、CSS 或图片时，报告会明确列出。

## 9. 最后检查

运行来源完整性：

```powershell
./.venv/Scripts/python.exe scripts/verify_source_integrity.py
```

然后在桌面界面执行：

1. 用完整题名或 DOI/arXiv 搜索。
2. 打开资产详情。
3. 测试 PDF、代码、导读和原始 HTML。
4. 检查领域、作者、年份和标签。
5. 确认代码仓库关系正确。
6. 只读核对阅读状态与批注；仅当用户本来要求写入时才验证写入，不能为了 smoke test 污染用户状态。

如果本次新增或调整了领域，还要进入对应领域详情，确认原生 v2 总览九章节齐全、无 iframe、桌面和窄屏无横向溢出；再测试“原始 HTML”是否只在来源表示可用时启用并在新窗口打开。任何门户更新都不应造成来源文件 hash 或 mtime 变化。

刷新开放目录：

```powershell
./.venv/Scripts/python.exe scripts/export_catalog.py
```

如使用 Obsidian：

```powershell
./.venv/Scripts/python.exe scripts/sync_obsidian.py --preview-only
./.venv/Scripts/python.exe scripts/sync_obsidian.py
```

如使用 Zotero：

```powershell
./.venv/Scripts/python.exe scripts/export_zotero.py
```

Zotero 包仍需你在 Zotero/Better BibTeX 界面中确认导入。

## 10. 常见错误

| 现象 | 原因与处理 |
| --- | --- |
| dry-run 报记录数不一致 | 更新 `expected_records`，并确认 manifest 没有漏项 |
| `duplicate_source_record_id` | 同一来源内存在重复 ID；为不同论文使用不同稳定 ID |
| `path_outside_root` | manifest 使用绝对路径或 `..`；改为相对于已声明 asset root 的路径 |
| `asset_path_missing` | 路径拼写错误、文件尚未完成写入或盘符未挂载 |
| 未批准弱合并 | 先核对是否同一论文，再由 Agent/用户显式更新审批清单 |
| 搜索不到新内容 | 等抽取任务完成，并重建 Meilisearch 索引 |
| PDF/HTML 无法打开 | 检查表示 availability、网关健康和 HTML 审计报告 |
| Zotero 没有新条目 | CLI 只生成导入包，还需要在 Zotero 界面确认 |

如果你只想日常阅读、搜索和批注，不需要执行这些命令；直接使用桌面界面即可。桌面界面操作详见 [完整使用手册](USER_MANUAL.md)。
