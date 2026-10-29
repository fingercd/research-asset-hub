# 研究资产库·桌面版

> 公共发行从空资料库开始，不附带开发者个人论文、第三方代码或历史来源登记。本文保留的日期化本机验收描述属于开发历史，不等于当前公开安装包的验收结果；使用自己的资料完成接入后，工具只读取正式入库且可用的材料。

Tauri 2 桌面版在原生窗口中使用内嵌界面和随包运行时。默认数据目录为 `Documents\ResearchAssetHub\`，可在设置中选择空目录或已有兼容库；切换后原库保留。显式 `HUB_DATA_DIR` 或 `--data-dir <绝对路径>` 固定目录时，界面显示该限制。

当前源码版本为 **0.3.0-rc.1**，公开在线预览包见[版本下载页](https://github.com/fingercd/research-asset-hub-public/releases/tag/v0.3.0-rc.1)。在线包只支持新的空安装目录，配套工具从供应商下载并核验固定版本及 SHA-256；构建说明见[在线预览安装器](scripts/public-online/README.md)。当前没有宣称完成独立干净 Windows 的安装、升级与卸载实机验收，发布说明中列明验证边界。公开源码和安装包从空资料库开始，不包含维护者的个人论文和配置。

## 开发

前置：Node.js 24、根目录 `package.json` 固定的 pnpm、Rust 工具链和 Tauri 2 CLI。构建器使用的 CLI 版本需要与固定的 NSIS 模板匹配，当前模板来自 2.11.4。

```powershell
.\scripts\bootstrap.ps1
.\scripts\desktop.ps1 build
.\scripts\desktop.ps1 plan -RuntimeRoot <便携runtime绝对目录> -DataDir <隔离资料绝对目录>
# 检查 plan 后，才使用相同参数运行；run 会先构建并启动原生桌面。
.\scripts\desktop.ps1 run -RuntimeRoot <便携runtime绝对目录> -DataDir <隔离资料绝对目录>
```

唯一开发路径是静态 React 构建与启用 `custom-protocol` 的 debug 壳，不运行 Vite
网站、Tauri dev server、Compose 或 WSL。脚本显式传入源码根、便携运行时根和资料根；
开发 Python 固定取源码 `.venv/Scripts/python.exe`，数据库/搜索取指定 runtime。
不猜已安装目录，也不在启动时强杀端口占用者。单实例存在时正常关闭原生程序再测试。
详情及测试边界见 [隔离开发指南](../docs/DEVELOPMENT.md)。

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
生产模式仅使用随包 `runtime/python/python.exe`，不再读取本机解释器提示文件。
源码开发模式由显式开发参数选择 checkout 的 `.venv/Scripts/python.exe`；这个选择不
进入可分发安装包，也不改变用户资料配置。
各阶段耗时记录在数据目录的 `logs/startup-timing.log`，HTTP 服务共用 `logs/api.log`。
`logs/desktop-timing.log` 从进程入口记录窗口创建、服务就绪和前端页面加载，避免漏算
桌面壳初始化时间。

运行入口支持可选的 `app/python-startup-<cache_tag>.zip` 导入缓存，公共组装脚本默认不生成它。没有该文件时使用正常 Python 导入。维护既有缓存的本机环境时，依赖变化后可显式重建：

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
# 1. 按 desktop/scripts/README.md 提供已核验的数据库、搜索、备份工具与 notices 输入
#    完整参数包括版本、SHA-256 和构建 profile，不能只提供两个二进制路径。
# 2. 组装成功后打包（自动先构建前端）
cd desktop/src-tauri
cargo tauri build --bundles nsis
# 产物: target/release/bundle/nsis/ResearchAssetHub_<version>_x64-setup.exe
```

仅构建原生可执行文件时也必须启用 `--features custom-protocol`，并先构建静态资源。
不带该 feature 的直接 Cargo 构建不属于支持的开发或交付路径。

发行使用新的暂存目录，不直接打包实际验收运行过的目录。Python 执行会留下字节码缓存，增量复制前端也可能留下旧资源；应从当前白名单源码和完整 `dist` 重新组装。保全旧暂存和构建资源后，打包前核对源码哈希，打包后核对生成的 NSIS 文件清单。Tauri 的配置覆盖会合并资源映射，替换暂存路径时也须确认旧映射已移除。

默认安装目录 `C:\Program Files\ResearchAssetHub\`（NSIS perMachine），日常数据写入用户目录。公共 profile 使用空库配置；已有安装升级会核对来源映射、保留用户配置并将旧 app/runtime 移到同一安装目录下的 `.previous-*`。这些目录不自动删除，详见[发行约束](scripts/RUNTIME-RELEASE.md)。

PostgreSQL 发行副本加入每进程 UTF-8 manifest，输入二进制保持不变，转换前后哈希记录在运行时报告中。候选版要求 Windows 10 1903+ 或 Windows 11 x64。默认 WebView2 缺失时联网准备；离线安装需要按组装文档准备 Microsoft 的离线输入和对应构建配置。

## 验收清单(发版前逐条跑)

- [ ] 干净机(无 Docker/Python/Node)安装成功,桌面/开始菜单有图标
- [ ] 双击 → 加载页显示「预检环境→启动数据库→启动搜索引擎→数据迁移→启动应用服务」→ 自动进入主界面
- [ ] 全程无浏览器窗口、Vite dev/preview 或独立网站监听；原生页面来自静态自定义协议
- [ ] 点 X → 窗口立即消失，后台服务完成清理后退出；再次双击可正常启动
- [ ] 加载页进行中(启动未完成)点托盘「退出」→ 进程数秒内退出、无残留
- [ ] 托盘「退出」→ `tasklist` 无 python/meilisearch/postgres 残留
- [ ] 断网可用;搜索/论文/代码/图谱/备份全功能冒烟
- [ ] 重复双击不产生第二个窗口(唤起已有)
- [ ] 端口冲突(如 8767 被占)→ 加载页停在「预检环境」并弹窗说明
- [ ] 卸载 → 重装 → 数据完好;`Documents\ResearchAssetHub` 未被动
- [ ] updates 目录放新版 manifest+安装包 → 托盘「检查更新」→ 确认 → 安装 → 重启后版本号更新、数据无损
- [ ] 更新源无 manifest → 提示「尚未配置更新源」并显示更新目录路径
