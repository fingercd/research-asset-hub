# 代码地图与数据流

本文按当前源码描述入口、职责和边界，不把历史交付报告当成当前运行证明。隔离运行命令见[开发指南](../DEVELOPMENT.md)，来源规则见[来源边界](source-boundaries.md)。

## 1. 一个原生监督器，静态界面与只读 MCP

### Windows 桌面与显式开发

[`scripts/desktop.ps1`](../../scripts/desktop.ps1) 只构建静态前端和原生程序；`plan` 列出明确参数，`run` 需要显式 runtime 与 data-dir。没有 Vite devUrl、独立网站托管、Compose 或云端 Web fallback。

```text
原生监督器（生产或显式开发 profile）
  ├─ 自管 PostgreSQL / Meilisearch
  ├─ apps.desktop_services
  │    ├─ 数据库迁移
  │    ├─ API（仅本机）
  │    └─ 安全资产网关（独立 Origin / CSP）
  ├─ apps.worker_start
  └─ Tauri 内嵌静态前端（不监听网站端口）
```

[`run_startup`](../../desktop/src-tauri/src/lib.rs) 与 [`Orchestrator`](../../desktop/src-tauri/src/orchestrator.rs) 是唯一生命周期实现。生产只使用随包运行时；缺失报错，不猜源码路径或系统 Python。开发明确 `--dev-source-root`、`--dev-runtime-root` 和 `--data-dir`，使用同一个监督器。静态前端仅接受原生注入的 API base，不再回退到页面相对路径。

[`apps/desktop_services.py`](../../apps/desktop_services.py) 在一个 Python 进程内承载两个独立 FastAPI 应用，仍保持 API 和 gateway 的端口、安全会话、CORS 与 CSP 边界。窗口 X 正常退出本程序拥有的后台；冲突端口不认领、不强杀。源码更新不等于已安装程序更新，原生验收必须核对实际执行文件。

### 只读 MCP

[`apps.mcp_server.__main__`](../../apps/mcp_server/__main__.py) 启动 stdio FastMCP，实际工具在 [`server.py::create_server`](../../apps/mcp_server/server.py)。[`HubClient`](../../apps/mcp_server/client.py) 仅用 HTTP GET 访问固定本机路由，不直接查询数据库或执行来源代码。

构造桥接、`initialize` 和列工具不启动后台；默认 API 的首次实际读取连接失败时，[`SharedBackend`](../../apps/mcp_server/backend.py) 通过锁、就绪探针及桌面单实例检查，按需唤醒或复用已有桌面监督器。自定义 API 地址由调用者管理。工具内容只读不等于完全不产生运行活动：冷唤醒可创建协调锁并启动已有后台，worker 可继续处理先前已排队的任务。

## 2. 数据流与事实来源

```text
外部只读来源 + 新旁车 manifest ─→ adapters ─→ dry-run / formal import ─┐
                                                                  ├─→ PostgreSQL
网页上传 PDF ─→ intake 草稿 / 预览 ─→ 托管文件 + 不可变来源清单 ───────┘
                                                                      │
                                                       outbox / jobs  ↓
                                                worker → 抽取与索引任务
                                                   ├─→ PostgreSQL chunks / 状态
                                                   └─→ Meilisearch 可重建索引

前端 / MCP → API → 元数据、关系、阅读记录和已抽取证据
前端文件打开 → 受控打开动作 → gateway → 已登记文件只读响应
```

- **PostgreSQL 是操作主库**：资产、表示、来源证据、关系、个人状态、任务和审计均以它为准。
- **Meilisearch 是可重建投影**：不替代原始证据与数据库。当前不生成 embedding、不启动 Qdrant；代码全文索引默认不参与查询。
- **来源原件保持只读**：用户上传的新托管资料与原位引用是两条受控路径，不能据前者推断允许覆盖旧来源。
- **元数据搜索不等于原文结论**：MCP 论文检索匹配标题、中文题名、摘要和必要标识；具体科研判断要读原文页码。网站搜索及其其他资产分组有自己的服务入口，不要假定所有搜索都走同一实现。
- **台账不等于覆盖率**：状态计数、抽取成功、索引 READY、文件真实存在和科学内容正确分别验证；健康接口不扫描全库来源文件。

## 3. 按任务找代码

| 要做什么 | 首先阅读 | 职责与边界 |
| --- | --- | --- |
| 调整前端请求 | [桌面 API client](../../apps/web/src/api/client.ts) | 唯一桌面注入 API base，未初始化时明确报错；请求/超时/错误转换 |
| 调整 API 组装或浏览器写保护 | [API main](../../apps/api/main.py) | 数据库管理器、CORS、Origin/CSRF、健康检查；不是公网多用户认证系统 |
| 找业务端点 | [routes_v1](../../apps/api/routes_v1.py)、[routes 目录](../../apps/api/routes/) | 共享查询/序列化辅助与拆分路由并存，不能假定已经完全分层 |
| 查配置优先级 | [ApiSettings](../../apps/api/config.py)、[CLI config](../../packages/cli/config.py)、[managed sources](../../packages/managed_sources/service.py) | 进程环境、保存的资料目录、用户配置与源码配置；维护脚本可能有独立默认值 |
| 新增来源解析 | [adapters](../../packages/adapters/)、[CLI](../../packages/cli/main.py) | 产生标准化记录；不要按领域名在核心导入中散布特殊分支 |
| 查正式导入/身份/冻结 | [formal.py](../../packages/ingestion/formal.py)、[dry_run.py](../../packages/ingestion/dry_run.py) | 冻结摘要、强/弱身份候选、幂等资产/表示/关系、outbox |
| 查网页托管上传 | [intake routes](../../apps/api/routes/intake.py)、[intake service](../../packages/intake/service.py) | 草稿、预览、PDF 校验、托管副本和正式提交 |
| 查任务执行 | [worker main](../../apps/worker/main.py)、[handlers](../../apps/worker/handlers.py) | outbox、任务租约与心跳、抽取/索引/快照/导出分派；抽取器延迟初始化 |
| 查正文抽取 | [extraction](../../packages/extraction/) | PDF/HTML/代码等内容抽取；OCR 与文件可用性仍需单独核实 |
| 查索引投影 | [index builder](../../packages/indexing/builder.py)、[search runtime](../../apps/api/search_runtime.py) | 默认 assets/annotations 维护、精确范围刷新和网站检索运行时 |
| 查 Agent 证据查询 | [knowledge routes](../../apps/api/routes/knowledge.py)、[KnowledgeService](../../packages/knowledge/service.py)、[status](../../packages/knowledge/status.py) | 原文/analysis 区分、分页、领域/会议/年份筛选、页码及来源状态 |
| 查安全打开/代码浏览 | [gateway](../../apps/asset_gateway/main.py)、[registry](../../apps/asset_gateway/registry.py)、[code browser](../../apps/api/code_browser.py) | 来源归属、路径限制、签名会话、CSP 和只读文件内容 |
| 查领域总览 | [domain routes](../../apps/api/routes/domains.py)、[portal loader](../../packages/domain_portals/loader.py)、[portal schemas](../../packages/domain_portals/schemas.py) | 配置内容与动态数据库目录合成；无精选门户与坏配置分别处理 |
| 查源码快照/领域包 | [repository snapshots](../../packages/repository_snapshots/)、[workflows](../../packages/workflows/) | 固定版本源码与资料迁移；不执行下载的仓库代码 |
| 查备份与恢复 | [backup](../../packages/backup/)、[backup API](../../apps/api/routes/backups.py) | Restic、范围报告和隔离恢复演练；快照存在不等于恢复已验证 |
| 查数据结构/迁移 | [domain models](../../packages/domain/models.py)、[domain package](../../packages/domain/)、[migrations](../../migrations/) | SQLAlchemy 模型、约束与 Alembic；不直接改正式数据库绕过迁移 |

维护命令与副作用应同时阅读对应脚本参数，不把 `--help`、dry-run、正式导入、排队任务或索引刷新混为一类操作。知识库新增的完整门禁见[Agent 手册](../AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)，不由本文简化。

## 4. 验证入口

- Python：[`tests/`](../../tests/)，MCP 窄范围：[`tests/mcp/`](../../tests/mcp/)。
- 前端：[`apps/web/package.json`](../../apps/web/package.json) 的 typecheck/test/build；加载页与组件用 JSDOM；交付交互在真实原生窗口验证。
- 桌面：[`desktop/src-tauri`](../../desktop/src-tauri/) 的 Rust 测试，以及独立的原生安装/生命周期验收。
- CI：[`checks.yml`](../../.github/workflows/checks.yml) 运行离线仓库检查、Python Ruff/pytest 与前端 typecheck/test/build；Markdown/docs 的 push 也会触发检查。它不替代原生安装、真实冷唤醒或恢复演练；仍应核实目标提交对应的实际运行结果。

当前源码版本由版本文件和构建配置确定；历史报告只证明报告明确记录的版本、机器和流程。更改实现后应重新运行与改动相称的验证，并单列未完成项。
