# Research Asset Hub · 研究资产库

**把论文、实现代码和阅读记录放到同一条可追溯的研究路径上。**

本地优先的科研资料管理工具：按领域找资料，按原文页码读证据，按固定版本查代码。**原生 Windows 桌面是唯一产品入口**；只读 MCP 与维护工具共用资产模型，不再运行独立网站、Vite 开发服务器或云端 Web 模板。

**[使用手册](docs/USER_MANUAL.md) · [资料导航](docs/LIBRARY_MAP.md) · [MCP 接入](docs/MCP_USER_GUIDE.md) · [开发指南](docs/DEVELOPMENT.md) · [全部文档](docs/README.md)**

> **源码与资料分开。** Git 保存应用代码、契约、来源清单和维护文档，不分发个人论文、第三方源码快照、数据库或密钥。克隆仓库不等于下载完整资料库；安装版也不会随源码 push 自动升级。

## 从你要做的事开始

| 你要做什么 | 从这里进入 |
| --- | --- |
| 找论文、读 PDF、看关联实现 | 桌面“研究领域 / 搜索”；[日常阅读工作流](docs/USER_MANUAL.md) |
| 让 Agent 查论文、引用页码和代码行号 | [MCP 使用指南](docs/MCP_USER_GUIDE.md) |
| 新增一篇论文 | 界面“添加资料”：**标题 + PDF 必填**，其他材料选填 |
| 登记一批现有 PDF、代码和导读 | [资料入库指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md)：先预演，再备份和正式导入 |
| 看清领域、文件、论文和代码如何对应 | [资料地图](docs/LIBRARY_MAP.md)与[代码地图](docs/architecture/code-map.md) |
| 开发、测试、排查环境 | [隔离开发指南](docs/DEVELOPMENT.md)与[贡献约定](CONTRIBUTING.md) |
| 找维护脚本或批处理工具 | [脚本索引](scripts/README.md)与[工具箱](tools/README.md) |

## 资料如何组织

```text
研究领域
└─ 研究单元：一篇论文 / 技术报告
   ├─ 身份与来源：稳定记录 ID、DOI / arXiv、版本、文件指纹
   ├─ 原始材料：PDF、原网页、Notebook
   ├─ 关联实现：本地只读代码 / 固定 commit 快照 / 在线链接
   ├─ 派生分析：中文导读、图、方法卡（不冒充原文证据）
   └─ 个人状态：阅读进度、批注、标签、专题集合
```

论文和代码是独立资产，通过 `implemented_by` 等有证据的关系关联。磁盘里有代码目录，不等于论文的“代码”按钮已建立关联；在线 URL、已下载源码、官方实现、第三方复现分别记录。

| 能力 | 实际边界 |
| --- | --- |
| 领域门户与目录 | 总览、时间线、评测与阅读路线；新领域仅名称必填，精选内容可后补 |
| 资料工作台 | 常驻全部资料、阅读中、未读和保存视图；紧凑列表/详细卡片切换，筛选条件可逐项清除 |
| 关键词检索 | 标题、中文题名、摘要与精确标识；桌面和 MCP 均支持会议/期刊、发表年份精确筛选，分页前过滤 |
| PDF 阅读与批注 | 优先打开本地安全会话；版本辨识、页码跳转、缩放/适宽、正确版本续读与文件指纹绑定高亮 |
| 代码阅读 | 原位只读目录、远程链接或固定 commit 快照；不自动运行代码、下载训练数据和权重 |
| 个人组织 | 专题集合、保存筛选、批量状态与标签、论文比较及 Markdown 导出 |
| 迁移与维护 | 领域包、可审计导入、后台抽取、关键词索引、备份与隔离恢复演练 |
| 外部工具 | 只读 MCP、Obsidian 同步、Zotero 导出 |

搜索不把正文、生成导读和标签混入论文标题摘要证据。当前默认**不生成 embedding、不启动向量模型或 Qdrant**；正文和代码按需带页码、行号读取。代码全文索引默认关闭，不影响安全浏览代码。

## 快速开始

### 我只想使用资料库

1. 启动已安装的 Research Asset Hub，进入“研究领域”或“搜索”。
2. 第一次使用时，新建领域，再上传 PDF、填写标题并检查预览。
3. 打开论文详情，分别查看 PDF、HTML 与关联代码的实际状态。
4. 要给 Agent 使用，在“设置 → Agent 接入”复制本机生成的 MCP 配置。

新上传资料可托管复制到用户库；**既有外部来源始终原位只读**。缺少选填 HTML/代码可以正常入库，已经登记但丢失的文件则必须显示为缺失。

当前源码版本为 **0.3.0-rc.1 桌面改革候选版**，版本依据 [Python 配置](pyproject.toml)和 [Node 配置](package.json)。本机安装、源码和候选包验收状态分别判断；功能与实际验证见[产品改革报告](docs/reports/2026-10-28-desktop-product-reform.md)。独立干净 Windows 安装、升级、卸载及签名仍需专项验收，详见[桌面文档](desktop/README.md)与[发行约束](desktop/scripts/RUNTIME-RELEASE.md)。

### 我想查看或修改源码

在**新的 clone / 独立 checkout** 中准备依赖，不要在正式运行目录直接重启服务：

```powershell
git clone https://github.com/fingercd/research-asset-hub.git
cd research-asset-hub
uv sync --frozen --extra dev
pnpm install --frozen-lockfile
.\.venv\Scripts\python.exe scripts/check_repository.py
```

前置：Python 3.11+、uv、Node.js 24、根 `package.json` 固定的 pnpm，原生构建另需 Rust 与 Windows 构建工具。以上不启动数据库或导入资料。

**下一步按[开发指南](docs/DEVELOPMENT.md)执行 `pnpm desktop build`，并显式指定便携运行时与独立数据目录进行原生验证。** 静态前端嵌入 Tauri，不监听 8765、4173 或 1430。生产缺运行时直接报告错误，不回退到系统 Python 或容器；开发使用同一套原生监督器。`hub.ps1` 仅做资料维护，不再提供 start/dev/stop。

`apps/web` 是历史沿用的桌面前端源码目录名，不代表仍有 Web 产品。`.env.example` 是变量参考，不会自动加载。

## Agent：先定位，再读证据

7 个只读 MCP 工具：`library_status`、`search_assets`、`get_paper`、`read_evidence`、`read_code`、`get_figure`、`get_methodology`。不导入、不改批注、不执行论文代码。

```text
1. library_status()                              → 查看领域与台账范围
2. search_assets(query="", asset_type="paper",
                 venue="CVPR", year=2026)         → 按库内字段浏览目录
3. 按 next_offset 翻页，直到 has_more=false        → 去重数量与 total 核对
4. 用返回的资产 ID 读原文 / 代码                  → 保留页码、行号、来源和版本
```

会议筛选是登记字段匹配，不等于官方录用核验；目录总数不等于关键词命中数。原文是 `original`，导读是 `analysis`，候选和待审状态不可省略。协议与故障排查见[MCP 指南](docs/MCP_USER_GUIDE.md)。

默认 MCP 在实际读取时按需唤醒或复用共享桌面后台；初始化和列工具不启动后台。多个聊天复用同一后台，关闭聊天不会关闭资料库。缓存旧工具 schema 的客户端需要重新连接。

## 代码与文档放在哪里

| 目录 | 职责 | 配套说明 |
| --- | --- | --- |
| [`apps/`](apps/) | 桌面前端、API、MCP、worker、资产网关 | [代码地图](docs/architecture/code-map.md) |
| [`packages/`](packages/) | 领域模型、导入、检索、证据、备份 | [来源边界](docs/architecture/source-boundaries.md) |
| [`desktop/`](desktop/) | Tauri 桌面壳、运行时与安装包 | [桌面构建](desktop/README.md) |
| [`config/`](config/) | 来源配置、冻结旁车、领域门户 | [配置说明](config/README.md) |
| [`newconfig/release-defaults/`](newconfig/release-defaults/) | 不含个人资料的空库发行配置 | [运行时组装](desktop/scripts/README.md) |
| [`scripts/`](scripts/) | 生命周期、维护、核验与历史批次脚本 | [脚本索引](scripts/README.md) |
| [`tools/`](tools/) | 元数据、代码关联、证据和批次处理 | [工具箱](tools/README.md) |
| [`tests/`](tests/) · [`migrations/`](migrations/) | 自动化回归与数据库迁移 | [测试与贡献](CONTRIBUTING.md) |
| [`docs/`](docs/) | 使用、开发、规范、计划和验收记录 | [文档导航](docs/README.md) |

`work/` 是本机日志、缓存和验证区，不进入 Git；原始资料与托管库在仓库之外。不要为“整齐”移动已登记资料，路径和指纹本身是证据。

## 数据安全、备份与版本

- **操作主库是 PostgreSQL。** 关键词索引与抽取缓存可重建，不能替代主库或原文件。
- 默认数据目录为 `%USERPROFILE%\Documents\ResearchAssetHub`，可在设置中选择资料库；配置优先级见[开发指南](docs/DEVELOPMENT.md)。切换不是搬运，原库保留。
- 备份包含数据库、用户配置与托管资料；外部只读原件默认不在其中，需核对报告范围并另做备份。`verified` 不等于数据库恢复演练通过。
- **Git 不是资料备份。** 不提交 `.env`、密钥、数据库、论文原件、权重和运行时；不删除或重写冻结 manifest 来消除错误。
- 当前源码点击窗口 **X 会隐藏窗口、清理后台并退出**，不再表示收起到托盘；已安装旧版本可能不同。见[修复记录](docs/reports/2026-10-26-desktop-close-fix.md)。

Restic 仓库默认位于资料库同级 `ResearchAssetHubBackups`；同盘快照不能代替异机备份。恢复在独立目录与端口演练，详见[使用手册](docs/USER_MANUAL.md)。

## 验证与已知限制

```powershell
.\.venv\Scripts\python.exe scripts/check_repository.py
.\.venv\Scripts\python.exe -m ruff check apps packages scripts tests
.\.venv\Scripts\python.exe -m pytest -q
pnpm --filter @research-asset-hub/web typecheck
pnpm --filter @research-asset-hub/web test
pnpm --filter @research-asset-hub/web build
```

文档检查发现断链和明显不应跟踪的文件，不是密钥扫描、论文质量验收或全库完整性证明。历史缺失与未完成安装验收均保留，不用“测试通过”掩盖。产品行为、同类项目参考与真实体验见[设计取舍](docs/design/desktop-library-workbench.md)和[改革报告](docs/reports/2026-10-28-desktop-product-reform.md)；此前的[仓库整理报告](docs/reports/2026-10-28-repository-organization.md)保留原验收范围。

扫描 PDF 的 OCR 依赖外部环境；全库科研图批处理未作为默认能力开启。资料数量和覆盖率是日期化观察，不是软件固有能力，也不代表官方会议全集。

## 贡献与许可

改动前看[贡献约定](CONTRIBUTING.md)；资料维护另读 [AGENTS.md](AGENTS.md)、[增量操作手册](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)和[来源边界](docs/architecture/source-boundaries.md)。安全问题按[安全报告流程](SECURITY.md)处理。

本项目代码采用 [MIT](LICENSE)。论文、第三方源码和依赖保留各自许可，整个桌面运行包不能笼统称为“全部 MIT”。沿革见 [CHANGELOG](CHANGELOG.md)。
