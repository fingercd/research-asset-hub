# 研究资产库·桌面版

Tauri 2 壳:双击即用,无浏览器、无 8765 本地网站。数据在 `Documents\ResearchAssetHub\`,卸载/升级均不触碰(可用环境变量 `HUB_DATA_DIR` 覆盖数据目录)。

## 开发

前置:Node.js 20+(仅用于构建前端；启用 corepack 后即得 pnpm,版本由根目录 `package.json` 的 `packageManager` 固定);Rust 工具链 + tauri-cli(`cargo install tauri-cli --locked`,2.x)。

```powershell
corepack enable                                  # 一次性:启用 pnpm(或直接 npm i -g pnpm)
pnpm --filter @research-asset-hub/web dev        # 终端 A:Vite 8765
cd desktop/src-tauri && cargo tauri dev          # 终端 B:壳(连 Vite)
```

开发模式壳使用容器基础设施(docker compose)与项目 .venv;生产模式使用便携运行时。

## 日常启动

桌面窗口与后台编排并行创建；单实例只保留最早执行的 Windows 互斥体保护。
后台在端口预检后，同时加载 Python 应用和启动基础设施。API（8767）与资产网关
（8766）在一个 Python 进程中运行，仍使用独立 FastAPI 应用、端口及原有 CORS、
签名与 CSP 策略；后台抽取 worker 保持独立进程，待两个 HTTP 服务就绪后启动。
加载页仍等待数据库迁移与两个服务全部就绪才进入首页。

每次启动直接查询 `alembic_version`，与安装包内迁移文件的全部 head 比对；相同则
跳过 Alembic 加载，有新迁移或无法确认版本时照常执行迁移。首次安装不跳过建库。
检查与迁移在共享的应用进程中完成，成功后才开放两个 HTTP 端口，不再额外启动
迁移专用的 Python 进程。
Python 入口直接通过 Windows 原生 API 预填系统版本缓存，省去 CPython 3.11
间接执行 `cmd /c ver` 时触发本机 CMD AutoRun/Conda 的额外工作。
本机快速版放在可写目录，Python 可以生成并复用与源码时间戳一致的字节码缓存；
源文件改变后仍会自动失效重建。不要直接复用安装包中时间戳不匹配的缓存来评估性能。
`app/config/python-executable.txt` 可显式指定本机已有的 Python 解释器；本机快速版
复用项目 `.venv/Scripts/pythonw.exe`，与打包依赖版本一致，并避免控制台窗口。未配置或路径已失效时使用
自带解释器；这个提示文件只写在本机安装目录，不写入来源配置或可分发安装包。
各阶段耗时记录在数据目录的 `logs/startup-timing.log`，HTTP 服务共用 `logs/api.log`。
`logs/desktop-timing.log` 从进程入口记录窗口创建、服务就绪和前端页面加载，避免漏算
桌面壳初始化时间。

组装脚本会生成 `app/python-startup-<cache_tag>.zip`，其中是标准库与选定纯 Python
依赖的预编译代码；原生扩展及其资源仍从真实目录加载。更新 Python 依赖后须重新生成：

```powershell
.\.venv\Scripts\python.exe scripts\build_startup_archive.py --app-dir <本机快速版目录>\app
```

源码运行时没有该归档，继续使用正常导入；归档名称与解释器的字节码版本对应。
后台 worker 的抽取器与 OCR 工具发现只在第一次抽取任务时初始化。
HTTP 服务就绪后，后台 worker 留出三秒给首屏取数，再加载任务处理模块；进程从创建时
就由桌面壳监督，退出时仍会清理。首次确认业务库存在后记录 `database-initialized`，
后续不再另起 `psql` 重复查库；每次启动的数据库连接与迁移版本检查继续保留。

正常退出后重启与断电后恢复应分别计时。数据库异常关闭后的恢复、首次初始化及新版本
迁移仍须完成，不能通过关闭 fsync 或跳过恢复来满足启动耗时目标。

## 构建安装包

```powershell
# 1. 一次性:按 desktop/scripts/README.md 准备 PostgreSQL 与 Meilisearch 并组装
.\desktop\scripts\stage-runtime.ps1 -PostgresZip ... -MeiliExe ...
# 2. 打包(自动先构建前端)
cd desktop/src-tauri && cargo tauri build
# 产物: target/release/bundle/nsis/ResearchAssetHub_<version>_x64-setup.exe
```

仅更新本机可执行文件时使用 `cargo build --release --features custom-protocol`。
直接 `cargo build --release` 不会启用 Tauri 的生产资源协议，会仍然访问开发服务器，
不能把这种构建用于桌面交付或启动性能验收。

安装目录 `C:\Program Files\ResearchAssetHub\`(NSIS perMachine);数据目录独立,卸载保数据。

## 验收清单(发版前逐条跑)

- [ ] 干净机(无 Docker/Python/Node)安装成功,桌面/开始菜单有图标
- [ ] 双击 → 加载页显示「预检环境→启动数据库→启动搜索引擎→数据迁移→启动应用服务」→ 自动进入主界面
- [ ] 全程无浏览器窗口;`http://127.0.0.1:8765` 访问失败
- [ ] 点 X → 窗口消失;托盘单击/「打开主界面」秒开(后台保持)
- [ ] 加载页进行中(启动未完成)点托盘「退出」→ 进程数秒内退出、无残留
- [ ] 托盘「退出」→ `tasklist` 无 python/meilisearch/postgres 残留
- [ ] 断网可用;搜索/论文/代码/图谱/备份全功能冒烟
- [ ] 重复双击不产生第二个窗口(唤起已有)
- [ ] 端口冲突(如 8767 被占)→ 加载页停在「预检环境」并弹窗说明
- [ ] 卸载 → 重装 → 数据完好;`Documents\ResearchAssetHub` 未被动
- [ ] updates 目录放新版 manifest+安装包 → 托盘「检查更新」→ 确认 → 安装 → 重启后版本号更新、数据无损
- [ ] 更新源无 manifest → 提示「尚未配置更新源」并显示更新目录路径
