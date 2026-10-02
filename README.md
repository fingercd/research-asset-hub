# Research Asset Hub · 研究资产库

一个在本机运行的个人科研资料管理平台，用统一入口管理论文、代码仓库、PDF、导读、笔记和历史 HTML。它记录资料之间的关系、阅读状态与批注，并提供搜索和结构化领域门户。

**来源文件保持原位、只读。** 平台通过来源清单、相对路径和指纹引用资料，不搬动、不覆盖原始论文和代码。Git 仓库保存应用源码、配置、迁移、测试与文档；实际资料、数据库、密钥、依赖和安装包不随仓库上传。

## 当前状态

本仓库保存个人工作环境的持续开发版本。2026-10-02 完成一次累计改动归档和桌面启动优化收尾；用户已认可当前启动速度，不再以“稳定 10 秒内”为验收条件。启动耗时受机器负载、缓存、数据库恢复和首次初始化影响，不作固定秒数承诺。

现有 `config/sources.yaml` 与旁车清单面向原工作环境，包含本机来源目录和历史记录。**克隆代码不会自动得到原知识库数据，也不代表这些路径在新机器上可用。** 新环境应先配置自己的来源并执行只读校验；不要直接批量导入历史清单。

本次验证范围和已知限制见 [2026-10-02 收尾记录](docs/reports/2026-10-02-startup-and-git-handoff.md)。历史报告中的资产数量只代表当时的快照，当前数量以应用页面和 API 为准。

## 能做什么

- **查找资料**：题名、DOI、arXiv、作者、关键词与组合筛选；从结果进入论文及其 PDF、导读和代码。
- **浏览领域**：由结构化内容生成领域门户，涵盖定位、时间线、方法、代表工作、评测、阅读路线、目录、前沿与证据。
- **管理阅读**：阅读状态、批注、资产详情、论文与代码关系、2–4 篇论文比较及 Markdown 导出。
- **安全打开资料**：受控的代码目录浏览；原始 HTML 通过独立资产网关和对应策略打开。
- **增量维护**：只读检查、导入预演、身份合并审核、来源完整性检查、后台抽取与索引重建。
- **连接外部工具**：Obsidian 同步、Zotero 导出、开放目录导出，以及面向知识加工的 MCP 接口。各工具的配置与限制见对应文档。

检索只使用现有 Meilisearch 关键词索引。中文问题通过科研术语、别名和查询条件处理，不生成 embedding、不建立向量库，也不启动向量模型或 Qdrant。代码仓库可以浏览，代码符号索引默认关闭。

## 系统组成

| 组件 | 职责 |
| --- | --- |
| React + TypeScript | 首页、领域门户、搜索、资产详情与阅读交互 |
| Tauri 2 + Rust | Windows 桌面窗口、单实例、托盘、后台服务编排与退出清理 |
| FastAPI | 业务 API，默认 `127.0.0.1:8767` |
| 独立资产网关 | 受控文件访问与 HTML 隔离，默认 `127.0.0.1:8766` |
| PostgreSQL | 主数据、阅读状态、审计、作业和来源记录 |
| Meilisearch | 可重建的关键词搜索索引 |
| Python worker | 后台抽取与作业处理 |

桌面模式将 API 与网关放在同一个 Python 进程中复用依赖加载，但保留各自的应用、端口和访问策略。worker 是独立进程。数据库迁移成功后才开放服务端口。

## 日常使用：Windows 桌面版

已安装的桌面版直接通过快捷方式启动，无需另开终端或启动浏览器。生产安装包携带运行时；桌面界面不依赖 `8765` 网站服务。

- 默认应用数据目录：`%USERPROFILE%\Documents\ResearchAssetHub`，可通过 `HUB_DATA_DIR` 指定。
- 点击窗口右上角关闭按钮会收起到托盘；需要停止后台服务时，从托盘选择“退出”。
- 日志在数据目录的 `logs/`：`api.log` 为 HTTP 服务日志，`startup-timing.log` 和 `desktop-timing.log` 记录启动阶段。
- 安装目录与数据目录分开。升级或替换应用文件不等同于迁移或备份数据库。

本机另有“研究资产库（快速启动）”快捷方式，指向独立的快速版目录；它是本机交付入口，不包含在 Git 仓库中。桌面构建、运行时组装和故障检查见 [desktop/README.md](desktop/README.md)。

## 从源码启动

以下命令从仓库根目录执行，示例使用 Windows PowerShell。

### 1. 准备开发环境

需要 Python 3.11+、Node.js、项目指定的 pnpm 11，以及 Docker Desktop/可用的 WSL2 容器环境。建议安装 uv，以便按 `uv.lock` 固定 Python 依赖；pnpm 版本以 `package.json` 的 `packageManager` 为准。

```powershell
git clone https://github.com/fingercd/research-asset-hub.git
cd research-asset-hub
Copy-Item .env.example .env
.\scripts\bootstrap.ps1 -StartInfrastructure
```

编辑本地 `.env`，按示例配置网关签名密钥等变量。不要把实际密钥提交到 Git。来源目录在 `config/sources.yaml` 中配置；在导入数据前阅读 [新增资料指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md)，并执行 `preflight`、`validate` 与 `inventory`。

### 2. 构建并启动 Web 版本

```powershell
pnpm --filter @research-asset-hub/web build
.\scripts\hub.ps1 start
.\scripts\hub.ps1 status
```

| 入口 | 默认地址 |
| --- | --- |
| Web 界面 | http://127.0.0.1:8765 |
| API 文档 | http://127.0.0.1:8767/docs |
| API 健康检查 | http://127.0.0.1:8767/api/health |
| 网关健康检查 | http://127.0.0.1:8766/health |

前端热更新使用 `.\scripts\hub.ps1 dev`。停止使用 `.\scripts\hub.ps1 stop`；普通停止保留数据库和索引卷。

### 3. 开发或构建桌面版

另需 Rust 工具链、Tauri 2 CLI 与 Windows 桌面构建依赖。生产包还需要组装 Python、PostgreSQL 和 Meilisearch 运行时，具体步骤见 [运行时组装](desktop/scripts/README.md)。

```powershell
# 终端 A：开发前端
pnpm --filter @research-asset-hub/web dev

# 终端 B：开发桌面壳
cd desktop/src-tauri
cargo tauri dev
```

生产安装包使用 `cargo tauri build`。仅重建生产可执行文件时必须启用资源协议：

```powershell
cargo build --release --features custom-protocol --manifest-path desktop/src-tauri/Cargo.toml
```

直接运行不带 `custom-protocol` 的 `cargo build --release` 会保留开发服务器访问行为，不能作为独立桌面包交付。依赖变化后必须重新组装运行时和 Python 启动归档。

## 增加资料与维护数据

完整操作见 [用户指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md) 和 [Agent 操作规范](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)。基本顺序为：

1. 确认资料在稳定目录，建立新来源或旁车清单，保持已有来源不变。
2. 执行只读预检、校验、盘点与导入预演。
3. 创建可恢复备份，再执行正式导入；用相同输入再次导入验证幂等性。
4. 完成抽取、索引和来源完整性检查，在页面中验证搜索、PDF、代码和导读。

已正式登记的来源清单与原文件属于冻结证据，不能直接重写来“修复”历史数据。来源更正、退休和合并须经过已有审核流程。禁止直接改数据库来绕过导入和迁移。

## 仓库结构

```text
apps/             API、资产网关、worker、MCP 服务与 React 前端
packages/         领域模型、导入、检索、知识加工、导出与备份逻辑
config/           来源与领域配置、旁车清单和策略
migrations/       PostgreSQL 的 Alembic 迁移
desktop/          Tauri 桌面壳、打包与运行时组装脚本
scripts/          启动、维护、校验与处理入口
tools/            特定资料加工工具及其说明
tests/            Python 测试；前端测试位于 apps/web
work/             本机运行、缓存和验证产物，不提交 Git
docs/             用户、开发、架构和工作记录
```

## 验证

```powershell
# Python 测试：部分集成测试需要对应环境
.\.venv\Scripts\python.exe -m pytest -q

# 前端类型、单元测试与构建
pnpm --filter @research-asset-hub/web typecheck
pnpm --filter @research-asset-hub/web test
pnpm --filter @research-asset-hub/web build

# 桌面 Rust 测试
cargo test --manifest-path desktop/src-tauri/Cargo.toml
```

这些是验证入口，不代表当前所有测试都已通过。每次交付应记录实际执行范围和失败项；本次结果见 [收尾记录](docs/reports/2026-10-02-startup-and-git-handoff.md)。涉及真实来源的完整验收还需要页面抽样、来源完整性和备份恢复演练。

## 备份与安全边界

GitHub 私有仓库用于代码与配置版本恢复，**不能恢复实际数据库、阅读批注、原始论文或本机密钥**。数据备份使用现有脚本与独立备份介质：

```powershell
.\.venv\Scripts\python.exe scripts\create_backup.py --report work\validation\backup.json
.\.venv\Scripts\python.exe scripts\restore_rehearsal.py --snapshot <snapshot_id> --run-id <unique-run-id>
```

恢复演练使用隔离环境，实际配置和前置依赖见 [用户手册](docs/USER_MANUAL.md)。同盘或同机副本不能替代异机备份。

服务默认在本机监听。网关只对已登记资料签发短时访问会话，不接受任意绝对路径。原始 HTML 使用独立 origin 与对应 CSP/sandbox 策略。`deploy/cloud/` 是部署模板，不代表已经公开部署或完成公网安全验收。

## 文档入口

- [完整使用手册](docs/USER_MANUAL.md)
- [新增资料与来源管理](docs/USER_KNOWLEDGE_BASE_GUIDE.md)
- [Agent 工作规范](AGENTS.md) / [详细维护流程](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)
- [来源边界](docs/architecture/source-boundaries.md)
- [桌面版开发与打包](desktop/README.md)
- [MCP 使用说明](docs/MCP_USER_GUIDE.md)
- [全部文档索引](docs/README.md)
