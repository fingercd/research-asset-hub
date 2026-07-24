# Research Asset Hub

Research Asset Hub 是六个现有论文领域之上的非破坏式本地管理层。它不会移动或改写来源资料；后续通过适配器读取来源清单，将论文、PDF、代码、导读、交互 HTML、阅读状态和批注汇总到统一网站。

当前仓库已完成第一批安全基础与全量 dry-run：

- `apps/api`：FastAPI API；`/api/health` 汇总 PostgreSQL、Meilisearch、Qdrant 和 worker 状态。
- `apps/worker`：后台 worker 占位进程，定期写入本地 heartbeat。
- `apps/asset_gateway`：独立 origin 的只读资产网关占位服务；尚未注册来源时不会暴露文件。
- `apps/web`：React、TypeScript 和 Vite 构建的本地平台壳。
- `packages/domain`：统一数据契约、数据库模型和开放导出器。
- `packages/adapters`：六领域适配器、通用 manifest 适配器和插件注册。
- `packages/ingestion`：稳定指纹、跨领域合并候选和只读试导入。
- `deploy/compose.yaml`：只启动 PostgreSQL、Meilisearch 和 Qdrant，宿主机端口全部绑定到 loopback。

六个来源共 428 条记录已全部通过共享 schema。当前仍停在人工审查点，非 dry-run 写入会被主动拒绝；搜索、详情页、批注、图谱和语义问答尚未实现。

## 先决条件

- Windows PowerShell 5.1 或 PowerShell 7
- Python 3.11+
- Node.js 20+，以及 pnpm 11 或可用的 Corepack
- Docker Desktop（用于 PostgreSQL、Meilisearch、Qdrant）
- 推荐安装 [uv](https://docs.astral.sh/uv/)，未安装时 bootstrap 会退回到标准 `venv + pip`

## 首次初始化

在项目根目录运行：

```powershell
.\scripts\bootstrap.ps1
```

bootstrap 会安装 Python 开发依赖和前端依赖，并在 Docker 可用时校验 Compose 配置。若希望同时启动三个基础设施容器：

```powershell
.\scripts\bootstrap.ps1 -StartInfrastructure
```

本地开发默认值是方便单机启动的开发配置，不应直接用于未来云部署。可通过环境变量覆盖，常用变量见下表。

| 变量 | 默认值 | 用途 |
|---|---|---|
| `HUB_API_HOST` | `127.0.0.1` | API 监听地址 |
| `HUB_API_PORT` | `8767` | API 端口；Vite 将 `/api` 代理至此 |
| `HUB_ASSET_GATEWAY_HOST` | `127.0.0.1` | 资产网关监听地址 |
| `HUB_ASSET_GATEWAY_PORT` | `8766` | 资产网关端口 |
| `HUB_DATABASE_URL` | 本地 Compose 数据库 | API 数据库健康探测 |
| `HUB_MEILISEARCH_URL` | `http://127.0.0.1:7700` | 全文索引健康探测 |
| `HUB_MEILISEARCH_API_KEY` | 本地 Compose 开发密钥 | Meilisearch 认证 |
| `HUB_QDRANT_URL` | `http://127.0.0.1:6333` | 向量索引健康探测 |
| `HUB_WORKER_HEARTBEAT_PATH` | `work/state/worker-heartbeat.json` | worker/API 共用的状态文件 |
| `HUB_WORKER_STALE_AFTER_SECONDS` | `30` | heartbeat 过期阈值 |

## 本地启动

可以使用统一脚本：

```powershell
.\scripts\hub.ps1 start
.\scripts\hub.ps1 status
```

也可以在四个终端中分别启动，便于调试：

```powershell
docker compose -f .\deploy\compose.yaml up -d
uv run rah-api
uv run rah-worker
uv run rah-asset-gateway
pnpm dev:web
```

若 bootstrap 使用了 `venv + pip` 回退路径，把前三条 `uv run ...` 替换为：

```powershell
.\.venv\Scripts\rah-api.exe
.\.venv\Scripts\rah-worker.exe
.\.venv\Scripts\rah-asset-gateway.exe
```

启动后：

- 平台壳：<http://127.0.0.1:8765>
- API 文档：<http://127.0.0.1:8767/docs>
- 聚合健康状态：<http://127.0.0.1:8767/api/health>
- 资产网关健康状态：<http://127.0.0.1:8766/health>

如果基础设施或 worker 尚未启动，API 仍会正常响应；顶层状态为 `degraded`，各组件包含独立错误和延迟信息。所有组件就绪时状态变为 `healthy`。

## 只读检查与试导入

```powershell
.\scripts\preflight.ps1
.\scripts\hub.ps1 inventory -All
.\scripts\hub.ps1 ingest -All -DryRun
```

报告写入 `work/validation/`。最近一次基线为 6 个来源、428 条记录、2,161 个表示候选和 1,753 个关系候选，且没有写入来源目录或数据库。

增加现有领域中的论文后，更新该领域正式 manifest 并重新执行 dry-run。增加新领域时，在 `config/sources.yaml` 注册来源；常见 JSON 结构可使用 `generic_manifest`，特殊结构再注册独立适配器。

## 停止与数据保留

```powershell
.\scripts\hub.ps1 stop
```

或只停止基础设施：

```powershell
docker compose -f .\deploy\compose.yaml down
```

普通 `down` 不会删除命名卷。不要添加 `--volumes`，除非明确要永久删除本地数据库和索引状态。

## 开发检查

```powershell
uv run ruff check apps packages migrations tests
uv run ruff format --check apps packages migrations tests
uv run mypy apps packages
uv run pytest
pnpm lint
pnpm typecheck
pnpm build
docker compose -f .\deploy\compose.yaml config --quiet
```

## 安全边界

- 所有宿主机监听地址默认为 `127.0.0.1`，当前版本不适合直接暴露到互联网。
- 资产网关使用独立端口，并在来源注册功能完成前拒绝文件请求。
- Compose 中的默认口令只用于本机开发。未来上云前必须改用外部密钥、TLS、认证、授权和异盘备份。
- 六个现有领域目录是只读来源；运行骨架本身不会扫描、移动、改名或删除其中任何文件。
