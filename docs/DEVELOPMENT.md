# 源码开发与隔离验证

本文面向开发者，不是个人正式资料库的启动指令。产品使用见[用户手册](USER_MANUAL.md)，代码入口见[代码地图](architecture/code-map.md)，来源不变量见[来源边界](architecture/source-boundaries.md)。

**下列启动命令已按当前源码核对，但本次文档整理没有真正启动容器、迁移数据库或运行安装验收。** 命令可创建开发数据和服务，执行前应检查本机端口与现有运行任务。

## 1. 准备环境

- Windows PowerShell；Python 3.11+ 与 uv。
- Node.js 24；pnpm 版本以根 [package.json](../package.json) 的 `packageManager` 为准，当前为 `pnpm@11.9.0`。
- 可从 Windows 调用的 Docker CLI、Compose 与正在运行的 Docker 后端。
- 修改桌面壳另需 Rust、Tauri 2 CLI 和 Windows 构建工具，见[桌面文档](../desktop/README.md)。

在**独立 clone/checkout** 中开发；不要直接使用承载正式 Web 服务的工作树。开始前运行 `git status --short`，保留已有改动。

```powershell
git clone https://github.com/fingercd/research-asset-hub.git
cd research-asset-hub
git status --short
.\scripts\bootstrap.ps1
```

`bootstrap.ps1` 优先用 uv 和冻结锁文件安装依赖；没有 uv 时会降级为 venv+pip，后者不等同于冻结依赖验证。它安装前端依赖，但**不生成 `apps/web/dist`**。`-StartInfrastructure` 要求 Windows 能找到 `docker`；它没有 `hub.ps1` 的 Ubuntu WSL 回退。

## 2. 建立不指向正式资料库的开发环境

仅设置 `HUB_DATA_DIR` 不够：数据库和搜索仍有自己的地址，Compose 命名卷与进程台账也分别隔离。下面使用空来源配置、独立端口、独立 Compose project 和 checkout 内的开发数据目录。

在仓库根目录、同一个 PowerShell 会话执行。端口只是示例；已被占用时请整组调整，不能抢占现有服务。

```powershell
$root = (Get-Location).Path
$devData = Join-Path $root "work\dev-library"
if (Test-Path -LiteralPath $devData) {
    throw "开发数据目录已存在，请检查后选择新的空目录。"
}
New-Item -ItemType Directory -Path (Join-Path $devData "config") | Out-Null
foreach ($name in @("sources.yaml", "taxonomy.yaml", "check-unit-actions-allowlist.yaml")) {
    Copy-Item -LiteralPath (Join-Path $root "newconfig\release-defaults\$name") `
        -Destination (Join-Path $devData "config\$name")
}

$env:HUB_DATA_DIR = $devData
$env:HUB_HOST_WORKSPACE_ROOT = $devData
$env:HUB_SOURCES_CONFIG_PATH = Join-Path $devData "config\sources.yaml"
$env:HUB_TAXONOMY_CONFIG_PATH = Join-Path $devData "config\taxonomy.yaml"
$env:HUB_DOMAIN_PORTAL_CONFIG_PATH = Join-Path $devData "config\domain-portals\v2"
$env:HUB_WORKER_HEARTBEAT_PATH = Join-Path $devData "state\worker-heartbeat.json"

$env:COMPOSE_PROJECT_NAME = "research-asset-hub-dev"
$env:HUB_WEB_PORT = "18765"
$env:HUB_ASSET_GATEWAY_PORT = "18766"
$env:HUB_API_PORT = "18767"
$env:HUB_POSTGRES_PORT = "15432"
$env:HUB_MEILISEARCH_PORT = "17700"
$env:HUB_POSTGRES_PASSWORD = "research_hub_local"
$env:HUB_DATABASE_URL = "postgresql://research_hub:research_hub_local@127.0.0.1:15432/research_asset_hub"
$env:DATABASE_URL = "postgresql+psycopg://research_hub:research_hub_local@127.0.0.1:15432/research_asset_hub"
$env:HUB_MEILISEARCH_URL = "http://127.0.0.1:17700"
$env:HUB_MEILISEARCH_API_KEY = "research-hub-local-meili-key"
$env:HUB_GATEWAY_SECRET = (& .\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32))").Trim()
$env:HUB_GATEWAY_ORIGIN = "http://127.0.0.1:18766"
$env:HUB_WEB_ORIGIN = "http://127.0.0.1:18765"
$env:HUB_API_ORIGIN = "http://127.0.0.1:18767"
$env:VITE_API_BASE = "/api/v1"
$env:HUB_MCP_API_URL = "http://127.0.0.1:18767/api/v1"
```

示例中的本地数据库/搜索密码仅用于这个隔离开发栈，不要复用正式凭据。网关密钥在本会话生成，不应提交或贴进报告。若其他开发环境也使用 `research-asset-hub-dev`，请另选唯一 project 名；仅换端口会继续复用同一 project 的命名卷。

不需要创建空 `portal-index.yaml`：当前门户 schema 要求至少一个门户。没有精选门户索引时，领域路由可返回正常的无精选门户界面；测试内容可以随后通过正式开发流程添加。

### 启动、查看与停止

```powershell
pnpm --filter @research-asset-hub/web build
.\scripts\hub.ps1 start
.\scripts\hub.ps1 status -Json
# 开发网站：http://127.0.0.1:18765
# 开发 API 文档：http://127.0.0.1:18767/docs
# 开发网关健康：http://127.0.0.1:18766/health

# 用完在同一会话停止；普通 stop 不删除命名卷。
.\scripts\hub.ps1 stop
```

`start` 会启动 Compose、执行 Alembic 迁移并启动应用，不是只读命令。需要热更新时，在停止当前开发链之后使用 `hub.ps1 dev`，仍保留上面的环境变量；`dev` 替换的是前端托管方式，不会让 Python API 自动热重载。

`hub.ps1` 的进程台账固定在**当前 checkout** 的 `work/state/processes.json`，不会按 `HUB_DATA_DIR` 或端口区分。它遇到已有部分/过期进程组会尝试停止并重建，所以不能在同一 checkout 用第二组变量管理另一套服务。日志在 `work/logs/`；不要把环境变量、密码和正式资料正文复制到诊断报告。

## 3. 配置到底从哪里来

Python [ApiSettings](../apps/api/config.py) 读取进程环境的 `HUB_*`，**没有自动读取根 `.env`**。Docker Compose 与 Vite 各有自己的环境读取行为，不能因此推断 API/worker 已加载相同文件。

| 设置 | 当前选择顺序 / 边界 |
| --- | --- |
| 资料目录 | 显式 `HUB_DATA_DIR`；否则桌面保存的 `%LOCALAPPDATA%/ResearchAssetHub/data-directory.txt`；再回退 `Documents/ResearchAssetHub` |
| API 来源/分类/门户 | 显式配置路径；否则数据目录存在 `config/sources.yaml` 时改用用户配置；再回退源码 `config/` |
| CLI 来源/分类 | 显式 `HUB_SOURCES_CONFIG_PATH` / `HUB_TAXONOMY_CONFIG_PATH`；否则优先用户库配置，再回退源码配置 |
| PostgreSQL / 搜索 | 分别由连接 URL、端口与密钥决定；不因更换资料目录自动切库 |
| 维护脚本 | 不保证全部复用 ApiSettings；先检查脚本参数，例如来源完整性脚本默认读取源码 `config/sources.yaml` |

因此在源码目录改 `config/` 不证明正在运行的用户库使用了它。维护当前用户库前，应核对运行参数、配置路径和数据根，而不是直接导入或重建索引。已登记来源与冻结 manifest 只读，新增材料流程见[Agent 手册](AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)。

## 4. 验证命令与范围

下列命令是开发检查，不等于独立机器安装、真实服务冷启动、全库索引一致性或恢复演练通过。测试会生成缓存/构建产物；涉及外部服务的测试应使用隔离配置，不指向个人正式库。

```powershell
uv sync --frozen --extra dev
pnpm install --frozen-lockfile
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check apps packages tests scripts
.\.venv\Scripts\python.exe -m mypy apps packages
pnpm --filter @research-asset-hub/web typecheck
pnpm --filter @research-asset-hub/web test
pnpm --filter @research-asset-hub/web build
cargo test --manifest-path desktop/src-tauri/Cargo.toml
```

MCP 窄范围测试用 `python -m pytest tests/mcp -q`，通过临时 GET-only fixture 验证协议与边界，不需要启动正式 Hub。更多说明见[MCP 指南](MCP_USER_GUIDE.md)。

常见检查点：

- `start` 提示缺少静态页面：先构建 `apps/web/dist`，bootstrap 不负责这一步。
- API/网关无法就绪：先看当前开发 checkout 的日志、独立端口与数据库 URL，不停止不明占用者。
- 维护脚本选择了错误来源：显式传配置路径；不要改冻结资料来迁就错误配置。
- `hub.ps1 status` 代表实际进程/HTTP 状态；`python -m packages.cli.main status` 当前只列 bootstrap/报告信息，两者不能互换。
- 源码 MCP 指定上述非默认 `HUB_MCP_API_URL` 后由开发者管理 API，不会自动唤醒正式桌面。修改 schema 后需重新连接 MCP；修改 API 后需正常重启对应开发进程，不另起重复 API。
