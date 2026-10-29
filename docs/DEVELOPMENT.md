# 原生桌面源码开发与隔离验证

**产品与开发均只有原生桌面入口，不再启动独立网站、Vite dev/preview、Compose 或 WSL。**
React 仍是桌面界面实现，Vite 仅将它构建为静态资源；Tauri 自定义协议加载这些资源。
Rust 壳统一管理便携 PostgreSQL、Meilisearch、Python API/资产网关及 worker。

本说明不代表已运行安装、真实冷启动或 UI 验收。执行 `run` 会启动原生程序、初始化/迁移
所选资料目录并写入日志；不要把正式库作为开发实验目录，不停止未知进程来让出端口。

## 1. 安装开发依赖

Windows PowerShell 7 (`pwsh`)、Python 3.11+、uv、Node.js 24、根 `package.json`
固定的 pnpm、Rust 与 Windows 构建工具。安装包构建另需 Tauri 2 CLI。

在独立 checkout 中执行，先保留已有改动：

```powershell
git status --short
.\scripts\bootstrap.ps1
```

bootstrap 只安装 Python/前端依赖，不构建页面、不启动服务。uv 使用冻结锁文件；没有 uv
时 venv+pip 是非冻结回退，不能据此声称锁文件可复现。便携数据库/搜索工具须由开发者
显式提供，不能从 `Program Files`、历史 stage 或运行进程猜安装目录。

## 2. 唯一开发入口

```powershell
# 仅构建：静态 React + custom-protocol 的 debug Rust 壳，不启动程序。
.\scripts\desktop.ps1 build

# 先检查将使用的命令和绝对路径；plan 不启动程序或创建资料目录。
.\scripts\desktop.ps1 plan `
  -RuntimeRoot 'D:\ResearchLibrary\ResearchAssetHub\runtime' `
  -DataDir 'D:\HubDevData\isolated-library'

# 确认这是隔离开发目录后才执行；每次 run 都先重新构建。
.\scripts\desktop.ps1 run `
  -RuntimeRoot 'D:\ResearchLibrary\ResearchAssetHub\runtime' `
  -DataDir 'D:\HubDevData\isolated-library'
```

`SourceRoot` 默认当前脚本所属 checkout，也可显式提供另一个源码根。入口将解析后的
`--dev-source-root`、`--dev-runtime-root` 和 `--data-dir` 全部传给 Rust，后者不猜测路径。
RuntimeRoot 应含 `postgres/bin/{initdb,pg_ctl,pg_isready,psql}.exe` 与 `meilisearch.exe`；
Python 固定取源码根的 `.venv/Scripts/python.exe`，不借用安装包或系统 Python。
DataDir 必须显式给绝对路径；目录可以不存在，由原生程序按正常首次启动流程初始化。
如需空库配置，先在这个新目录的 `config/` 放入 `newconfig/release-defaults/` 的内容，
否则源码模式按源码配置回退，可能读取现有来源；注册来源始终只读。

等价构建核心是 `pnpm --filter @research-asset-hub/desktop-ui build` 后
`cargo build --manifest-path desktop/src-tauri/Cargo.toml --features custom-protocol`。
开发构建临时合并 `TAURI_CONFIG` 的 `bundle.resources=null`，不要求不存在的安装包
stage/runtime；结束后恢复调用者的配置，生产组装配置不变。run 使用独立默认端口
API 18767、网关 18766、PostgreSQL 15432、Meilisearch 17700，可用 `-ApiPort`、
`-GatewayPort`、`-PostgresPort`、`-MeiliPort` 显式覆盖，四者必须互异。冲突由原生预检
报错，不抢占或 kill。run 清理继承的生产数据库/搜索连接、配置路径、密钥、心跳与
来源根环境变量，以免新 DataDir 仍连接正式库；退出后恢复调用者环境。plan 列出这些
覆盖/清理，但不修改环境。

不运行 `cargo tauri dev`、`pnpm dev` 或 `vite preview`。不带 `custom-protocol` 的直接
Cargo 构建不是支持的桌面开发路径。退出/重试由桌面自身处理，没有 PowerShell stop、
重复进程台账或自动 kill 占用端口的回退。现有原生实例存在时，应正常关闭它再测试，
不要让开发脚本管理正式桌面生命周期。

## 3. 旁车 CLI 与配置边界

```powershell
.\scripts\hub.ps1 preflight -Json
.\scripts\hub.ps1 inventory -All -Json
.\scripts\hub.ps1 validate -All -Json
.\scripts\hub.ps1 ingest -Source <source-id> -DryRun -Json
.\scripts\hub.ps1 status -Json
```

hub.ps1 只转发源码 `.venv` 中的维护 CLI；没有 start/dev/stop。`status` 列出旁车报告
位置与 bootstrap 信息，**不表示原生程序、数据库或 API 已就绪**。正式 ingest 会写库，
须按 [Agent 手册](AGENT_KNOWLEDGE_BASE_PLAYBOOK.md) 完成备份与检查，不因这里有命令即获授权。
`preflight.ps1` 是同一 CLI 的兼容薄入口，不另行发现 Python 或启动服务。

Python 设置读取进程环境的 `HUB_*`，不会自动加载根 `.env`。源码 CLI 的 sources/taxonomy
优先显式配置路径，其次用户库配置，再到源码配置。桌面开发参数明确选择源码/运行时/
数据根；数据库、密钥及两个 loopback HTTP 服务由桌面统一准备。不能只设置
`HUB_DATA_DIR` 就推断任意独立维护命令已经连接了相同数据库。维护正式库前核验实际
配置/连接，禁止修改冻结 manifest 或直接改数据库来迁就错误输入。

## 4. 测试边界

```powershell
uv sync --frozen --extra dev
pnpm install --frozen-lockfile
.\.venv\Scripts\python.exe -m pytest tests/runtime/test_hub_script.py tests/runtime/test_desktop_ui.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check apps packages tests scripts
.\.venv\Scripts\python.exe -m mypy apps packages
pnpm --filter @research-asset-hub/desktop-ui typecheck
pnpm --filter @research-asset-hub/desktop-ui test
pnpm --filter @research-asset-hub/desktop-ui build
cargo test --manifest-path desktop/src-tauri/Cargo.toml
```

单元测试、PowerShell AST/命令契约和静态构建不启动正式应用，不等于原生 UI 验收。
旧 Playwright 网站 runner、自动 preview 服务及独立浏览器产品 fixtures 已退役；组件/
页面继续用现有 Vitest，原生跨进程/窗口/资料操作需在隔离库中实际验收并单独记录结果。
Playwright 与 axe 仍用于原始论文/导读 HTML 的只读审计，不是产品网站部署依赖。
MCP 窄范围测试 `python -m pytest tests/mcp -q` 使用 GET-only fixture；schema 更新后
需重新连接客户端，不能另起重复服务绕过桌面生命周期。
