# Research Asset Hub

Research Asset Hub 是一个只在本机运行的个人科研数字资产平台。它把当前 10 个研究领域、11 个已登记来源中的论文、代码仓库、导读和历史 HTML 注册到统一数据库、搜索与关系图谱中，同时保持来源目录只读：不移动、不重命名、不覆盖原文件，也不复制原始大文件。

当前已验证登记基线（2026-07-26 正式导入报告）：

- 10 个领域、11 个只读来源、717 条规范化来源记录
- 651 篇论文资产、387 个代码仓库资产，另有 10 个系统隐藏的领域总览集合资产
- 3,136 个表示候选，其中 3,127 个可用、9 个明确标记为缺失
- 本轮新增来源记录：视觉语言导航（VLN）72、游戏强化学习 72、弱监督视频异常检测（WSVAD）45，以及 10 条领域整体 HTML 门户记录
- 10/10 个领域整体 HTML 已验证可在 Hub 统一样式与来源原始样式之间切换
- 未改变输入的第二次正式导入新增计数全部为 0；导入后来源完整性检查为 0 mismatch

抽取、关键词索引和向量索引会随增量资料重建；当前运行状态以 `/api/health` 和 `work/validation/` 中最近一次对应报告为准，不沿用旧基线数字推断。

## 功能

- 跨领域精确、混合与语义搜索；支持题名、DOI、arXiv、作者、代码符号、批注及组合筛选。
- 首页和领域目录从 API 动态显示全部已登记领域；领域详情默认内嵌该领域的整体 HTML，并可切换“统一样式/原始样式”。
- 搜索卡片直接打开 PDF、代码、导读或原始 HTML，并显示阅读状态与约 60 字批语摘要；没有批注时显示“无”。
- 统一资产详情页：元数据、阅读状态、批注、PDF、代码、导读、关系、相似论文和隔离 HTML。
- PostgreSQL 关系图谱，支持邻居、1–3 跳、最短路径、共同邻居和状态过滤。
- 2–4 篇论文并排比较、保存比较视图及 Markdown 导出。
- DeepSeek 资料库问答：本地混合召回、引用锚点验证、prompt-injection 隔离和硬预算控制。
- Obsidian 三方同步、Zotero/Better BibTeX 审查式导入包、开放 JSONL/Markdown/RO-Crate 导出。
- 持久作业队列、lease/heartbeat/retry/dead-letter、事务 Outbox、软删除、审计与版本冲突保护。
- Restic 加密本机备份及独立 Compose 环境恢复演练。

## 文档入口

- [完整使用手册](docs/USER_MANUAL.md)
- [用户自行增加知识库指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md)
- [后续 Agent 知识库增量规范](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)
- [全部文档索引](docs/README.md)

## 本地启动

先决条件：Windows PowerShell 5.1+、Python 3.11+、Node.js 20+、pnpm 11，以及可工作的 Docker Desktop/WSL2 后端。

首次安装依赖：

```powershell
.\scripts\bootstrap.ps1 -StartInfrastructure
pnpm --filter @research-asset-hub/web build
```

启动生产构建：

```powershell
.\scripts\hub.ps1 start
.\scripts\hub.ps1 status
```

打开：

- 网站：<http://127.0.0.1:8765>
- API 文档：<http://127.0.0.1:8767/docs>
- 健康检查：<http://127.0.0.1:8767/api/health>
- 隔离资产网关：<http://127.0.0.1:8766/health>

`start` 只提供 `apps/web/dist` 中的构建产物；需要 Vite 热更新时使用：

```powershell
.\scripts\hub.ps1 dev
```

停止应用与基础设施：

```powershell
.\scripts\hub.ps1 stop
```

普通停止不会删除数据库和索引卷。不要给 Compose 添加 `--volumes`，除非明确要永久删除本地状态。

## 日常更新资料

完整增量导入必须按“只读检查 → 全量 dry-run → 备份 → 正式导入两次”的顺序执行：

```powershell
.\scripts\hub.ps1 preflight
.\scripts\hub.ps1 validate -All -Json
.\scripts\hub.ps1 inventory -All
.\scripts\hub.ps1 ingest -All -DryRun
.\.venv\Scripts\python.exe scripts\create_backup.py `
  --report work\validation\backup-before-ingest.json
.\scripts\hub.ps1 ingest -All
.\scripts\hub.ps1 ingest -All
```

两次正式导入之间不得修改 manifest、来源配置或来源文件；第二次的来源记录、版本、论文、仓库、表示、关系、关系证据和字段声明新增数都应为 0。报告写入 `work/validation/`。

以后由用户或 Agent 增加一篇论文及其 PDF、HTML、代码时，不修改任何已登记来源；在 `config/intake/<source-id>/manifest.json` 创建新的旁车清单，并在 `config/sources.yaml` 注册新的只读来源。普通单知识单元使用 `generic_manifest`；增加新领域时，再在 `config/taxonomy.yaml` 登记稳定领域 slug。完整示例见 [用户自行增加知识库指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md)。

VLN、游戏强化学习、WSVAD 的确定性规范快照以及 10 个领域门户快照也位于 `config/intake/`。对应构建入口是：

```powershell
.\.venv\Scripts\python.exe scripts\build_canonical_snapshot.py --help
.\.venv\Scripts\python.exe scripts\build_domain_portal_snapshot.py --help
```

前者只支持固定的 `vln`、`game_rl`、`wsvad` profile，后者只构建项目登记的 10 个领域门户；两者都不改写来源文件。当前旁车已经过正式导入并冻结，不得为“刷新”而原地重建或改写。新增普通论文也不要使用这两个批量构建器。

重新抽取和构建正式索引：

```powershell
.\.venv\Scripts\python.exe scripts\seed_jobs.py
.\.venv\Scripts\python.exe -m apps.worker.main
.\.venv\Scripts\python.exe scripts\build_indexes.py `
  --version 20260726-domain-expansion `
  --report work\validation\index-build-bge-m3.json
```

只更新 Meilisearch 而保留向量：

```powershell
.\.venv\Scripts\python.exe scripts\build_indexes.py `
  --version 20260726-domain-expansion `
  --keyword-only `
  --report work\validation\index-build-keywords.json
```

## 开放导出与外部工具

生成不包含来源二进制的开放目录：

```powershell
.\.venv\Scripts\python.exe scripts\export_catalog.py
```

输出位于 `outputs/open-catalog/`。每个资产包含稳定 `asset_id`、`asset.json`、`note.md`、表示清单、关系与溯源；原文件只以来源别名、相对路径和 hash 引用。

Obsidian 默认同步到现有 Vault 的 `00_Home/Research Asset Hub`：

```powershell
.\.venv\Scripts\python.exe scripts\sync_obsidian.py --preview-only
.\.venv\Scripts\python.exe scripts\sync_obsidian.py
```

同步采用 base/hub/external 三方比较；同字段冲突进入网站的“同步冲突”页，不会静默覆盖。

生成 Zotero 审查式导入包：

```powershell
.\.venv\Scripts\python.exe scripts\export_zotero.py
```

在 Zotero 中安装 Better BibTeX 后，人工确认导入 `outputs/zotero/research-assets.csl.json` 或 `research-assets.bib`。平台读取 Zotero 本地 API；当前不会直接修改 Zotero SQLite。

## DeepSeek 问答

DeepSeek 密钥只存 Windows Credential Manager，服务名为 `research-asset-hub`，账户名为 `deepseek-api-key`；不要写入 `.env`、源码或命令日志。模型通过官方 `/models` 动态发现，业务代码不固定模型 ID。

默认硬预算：

- 单次任务不超过 ¥5
- 单日不超过 ¥10
- 单月不超过 ¥50
- 预留 20% 安全余量

未知价格、预算超限、模型不可用或引用校验失败时，平台保留本地检索证据并停止生成；不返回未经验证的模型引用。

## 备份与恢复

备份仓库默认为：

`C:\Users\ExampleUser\Documents\ResearchAssetHubBackups`

创建加密快照：

```powershell
.\.venv\Scripts\python.exe scripts\create_backup.py `
  --report work\validation\backup-final.json
```

恢复演练必须使用该次输出的 `snapshot_id`，并使用唯一 `run-id`：

```powershell
.\.venv\Scripts\python.exe scripts\restore_rehearsal.py `
  --snapshot <snapshot_id> `
  --run-id <unique-run-id>
```

演练使用独立 Compose project、卷、数据库和端口，恢复后重建 Meilisearch/Qdrant 并验证固定查询，不覆盖活跃环境。当前仓库仍是同机副本，发布报告会持续标记 `local-copy-only` 风险；重要资料还应增加外置硬盘、NAS 或云端副本。

## 验证与发布报告

```powershell
.\.venv\Scripts\ruff.exe check apps packages scripts tests
.\.venv\Scripts\mypy.exe --strict apps packages scripts
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts\verify_source_integrity.py
pnpm --filter @research-asset-hub/web lint
pnpm --filter @research-asset-hub/web typecheck
pnpm --filter @research-asset-hub/web test -- --run
pnpm --filter @research-asset-hub/web test:e2e
pnpm --filter @research-asset-hub/web build
```

完成最终备份与恢复演练后生成发布报告：

```powershell
.\.venv\Scripts\python.exe scripts\generate_release_report.py `
  --restore-report work\restore-tests\<run-id>\report.json
```

推荐交付入口：

- `outputs/release/release-report.md`
- `outputs/release/release-report.json`
- `outputs/open-catalog/`

## 安全边界

- Web、API、资产网关、PostgreSQL、Meilisearch 和 Qdrant 均只监听 `127.0.0.1`。
- 网关只接受已登记 `representation_id` 的短时签名会话，不接受任意绝对路径。
- 原始 HTML 在独立 origin 中按静态、可信本地交互或需要联网三类 CSP/sandbox 策略打开。
- 所有业务写操作在同一事务写审计和 Outbox；删除默认是软删除，来源文件永不随 API 删除。
- `deploy/cloud/` 只提供未来 Caddy/TLS、OIDC、secret 注入和私有存储接口模板；当前没有公开部署。
