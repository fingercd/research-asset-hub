# 研究资产库·桌面版设计规格

日期:2026-08-13
状态:已批准(用户确认"没问题执行详细的计划")

## 目标

把研究资产库交付为 Windows 桌面软件:双击图标即用,不依赖浏览器、不依赖本地网站、不要求用户安装 Docker/Python/Node 等开发环境。界面与当前 React 前端一致。

## 决策记录(与用户确认)

| 决策点 | 结论 |
|--------|------|
| 窗口技术 | Tauri 2(Rust 壳 + 系统 WebView2) |
| 前端载体 | 前端构建产物内嵌壳内,`http://127.0.0.1:8765` 本地网站**不再存在**(桌面模式) |
| 关闭行为 | 点 X 缩到系统托盘,托盘菜单「退出」才彻底停止全部后台 |
| 启动过程 | 双击后立即弹窗口显示「正在启动…」+ 分步进度,后台就绪后自动切换主界面 |
| 交付形态 | NSIS 安装包(开始菜单/桌面图标/控制面板卸载) |
| 更新 | 菜单「检查更新」:读更新源清单 → 下载新安装包 → 运行安装器 → 重启即新版 |
| 数据位置 | 文档目录 `Documents\ResearchAssetHub\`,卸载/升级不丢数据 |
| 约束解除 | 允许小改前端与后端(安全白名单),界面本身不变 |

## 架构

```
双击 Hub.exe
  └─ 壳进程(Rust/Tauri 2, 单实例)
       ├─ 启动编排器(内部状态机)
       │    ├─ 便携 PostgreSQL   (pg_ctl, 数据目录: 文档目录)
       │    ├─ 便携 Meilisearch  (单二进制, 数据目录: 文档目录)
       │    ├─ alembic upgrade head
       │    ├─ api           (便携 Python -m apps.api.main)            127.0.0.1:8767
       │    ├─ worker        (便携 Python -m apps.worker.main)
       │    ├─ asset-gateway (便携 Python -m apps.asset_gateway.main)  127.0.0.1:8766
       │    └─ gitnexus      (便携 Node gitnexus serve)                 127.0.0.1:4747
       ├─ 窗口
       │    ├─ 加载页(壳内资源, 订阅进度事件, 显示启动步骤)
       │    └─ 就绪后切换到内嵌前端 dist(tauri:// 自定义协议)
       └─ 托盘: 显示/隐藏窗口、退出
```

- 前端界面 = 现有 `apps/web` 构建产物,原样打包进壳资源
- 端口全部绑定 127.0.0.1,外部不可达
- 开发模式不受影响:Vite dev(8765)+ 现有 hub.ps1 照旧

## 关键组件

### 1. 壳(desktop/src-tauri,新增目录)

| 文件 | 职责 |
|------|------|
| `lib.rs` | 装配:插件、窗口、状态、命令注册 |
| `orchestrator.rs` | 启动编排状态机:预检 → 基础设施 → 迁移 → 应用进程 → 就绪;进度事件流 |
| `processes.rs` | 子进程 spawn/停止/进程树清理(Windows job object) |
| `runtime.rs` | 便携运行时路径解析(Python/Node/postgres/meilisearch 安装目录布局) |
| `data_dir.rs` | 文档目录数据区初始化与迁移(postgres initdb / meili data) |
| `tray.rs` | 托盘图标与菜单 |
| `updater.rs` | 检查更新:更新源 manifest 读取、版本比较、安装器运行 |
| `loading.html` | 启动加载页(壳内资源,订阅 `startup://progress` 事件) |

### 2. 前端改动(小,界面不变)

- `apps/web/src/api/client.ts`:`API_BASE` 解析优先级
  `window.__HUB_API_BASE__`(壳注入)> `import.meta.env.VITE_API_BASE` > `"/api/v1"`(开发默认)
- 壳在 WebView initialization script 中注入 `window.__HUB_API_BASE__ = "http://127.0.0.1:8767/api/v1"`(端口读壳配置)

### 3. 后端改动(小)

- `apps/api/config.py`:新增 `desktop_origins: list[str]`(默认含 `http://tauri.localhost`、`tauri://localhost`),可用 `HUB_DESKTOP_ORIGINS` 覆盖
- `apps/api/main.py`:
  - CORS `allow_origins` 并入 `desktop_origins`
  - `enforce_browser_write_origin` 白名单并入 `desktop_origins`(CSRF header 机制保持不变,前端已带 `X-Research-Hub-CSRF: 1`)

### 4. 便携运行时(安装目录布局,NSIS 打包)

```
Program Files\ResearchAssetHub\
  hub.exe                  ← Tauri 壳(内含前端 dist)
  runtime\
    python\python.exe      ← 便携 Python 3.12
    venv\                  ← 项目依赖(拷自开发机 .venv,修 pyvenv.cfg)
    node\node.exe          ← 便携 Node
    gitnexus\              ← gitnexus npm 包 + node_modules
    postgres\bin\          ← PostgreSQL 便携版(EDB zip binaries)
    meilisearch.exe        ← Meilisearch 单二进制
```

- 数据目录 `Documents\ResearchAssetHub\`:postgres-data / meili-data / backups / logs / updates(更新源清单+安装包)

## 数据流

1. 双击 → 单实例锁(重复双击唤起已有窗口)
2. 窗口立即显示 loading.html
3. 编排器按状态机推进,每步向 WebView 发 `startup://progress` 事件(步骤名 + 状态)
4. 失败任一步:弹错误对话框(步骤、原因、「查看日志」按钮打开数据目录 logs),窗口停在失败步骤;「重试」可用
5. 全部就绪 → WebView 导航到壳内前端 → 进入登录后的主界面(界面与现状一致)
6. 点 X → 窗口隐藏,后台保持(秒开);托盘「退出」→ 逆序停应用进程 → 停 postgres(pg_ctl stop)→ 退出
7. 更新:菜单「检查更新」→ 读 `Documents\ResearchAssetHub\updates\manifest.json`(或 HUB_UPDATE_URL 配置的远程地址)→ 版本高于当前 → 下载/复制安装包到临时目录 → 运行 NSIS 安装器 → 安装器完成后提示重启

## 错误处理

| 场景 | 行为 |
|------|------|
| 端口被占用(8766/8767/4747/5432/7700) | 弹窗列出占用进程名与端口,给出排查建议 |
| 数据目录损坏/数据库起不来 | 弹窗提示 + 「查看日志」;数据有备份机制兜底 |
| 更新源不可达 | 弹窗「更新源不可用」,软件继续正常使用 |
| 启动中途失败 | 加载页停在失败步骤,显示原因 + 重试按钮 |

## 测试与验收

- Rust 单元测试:状态机转移、端口占用检测、manifest 解析与版本比较
- Python 测试:CORS/写保护白名单含桌面 origin(pytest)
- 前端测试:API_BASE 解析优先级(vitest)
- 手动验收清单:
  - [ ] 双击 → 启动窗口带进度 → 主界面,全程无浏览器、无 8765
  - [ ] 浏览器访问 `http://127.0.0.1:8765` 连不上
  - [ ] X 缩托盘,托盘双击秒开,托盘「退出」后任务管理器无残留进程(postgres/meilisearch 也停)
  - [ ] 卸载 → 重装 → 数据完好
  - [ ] 断网全功能可用
  - [ ] 「检查更新」:更新源放新版安装包 → 升级成功且数据无损
  - [ ] 干净机器(无 Docker/Python/Node)可安装运行

## 范围界定(本期不做)

- 开机自启、静默自动更新、多用户、远程访问
- 开发流程改造(Vite dev 与 hub.ps1 保持现状)
