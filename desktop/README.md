# 研究资产库·桌面版

Tauri 2 壳:双击即用,无浏览器、无 8765 本地网站。数据在 `Documents\ResearchAssetHub\`,卸载/升级均不触碰(可用环境变量 `HUB_DATA_DIR` 覆盖数据目录)。

## 开发

前置:Node.js 20+(启用 corepack 后即得 pnpm,版本由根目录 `package.json` 的 `packageManager` 固定);Rust 工具链 + tauri-cli(`cargo install tauri-cli --locked`,2.x)。

```powershell
corepack enable                                  # 一次性:启用 pnpm(或直接 npm i -g pnpm)
pnpm --filter @research-asset-hub/web dev        # 终端 A:Vite 8765
cd desktop/src-tauri && cargo tauri dev          # 终端 B:壳(连 Vite)
```

开发模式壳使用容器基础设施(docker compose)与项目 .venv;生产模式使用便携运行时。

## 构建安装包

```powershell
# 1. 一次性:按 desktop/scripts/README.md 下载三个运行时并组装
.\desktop\scripts\stage-runtime.ps1 -PostgresZip ... -MeiliExe ... -NodeZip ...
# 2. 打包(自动先构建前端)
cd desktop/src-tauri && cargo tauri build
# 产物: target/release/bundle/nsis/ResearchAssetHub_<version>_x64-setup.exe
```

安装目录 `C:\Program Files\ResearchAssetHub\`(NSIS perMachine);数据目录独立,卸载保数据。

## 验收清单(发版前逐条跑)

- [ ] 干净机(无 Docker/Python/Node)安装成功,桌面/开始菜单有图标
- [ ] 双击 → 加载页显示「预检环境→启动数据库→启动搜索引擎→数据迁移→启动应用服务」→ 自动进入主界面
- [ ] 全程无浏览器窗口;`http://127.0.0.1:8765` 访问失败
- [ ] 点 X → 窗口消失;托盘单击/「打开主界面」秒开(后台保持)
- [ ] 加载页进行中(启动未完成)点托盘「退出」→ 进程数秒内退出、无残留
- [ ] 托盘「退出」→ `tasklist` 无 python/node/meilisearch/postgres 残留
- [ ] 断网可用;搜索/论文/代码/图谱/备份全功能冒烟
- [ ] 重复双击不产生第二个窗口(唤起已有)
- [ ] 端口冲突(如 8767 被占)→ 加载页停在「预检环境」并弹窗说明
- [ ] 卸载 → 重装 → 数据完好;`Documents\ResearchAssetHub` 未被动
- [ ] updates 目录放新版 manifest+安装包 → 托盘「检查更新」→ 确认 → 安装 → 重启后版本号更新、数据无损
- [ ] 更新源无 manifest → 提示「尚未配置更新源」并显示更新目录路径
