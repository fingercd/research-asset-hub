# Research Asset Hub · 研究资产库

在本机管理论文、PDF、代码和阅读记录的科研资料库。以论文为中心组织研究单元，通过领域目录、关键词检索和只读 MCP，把资料交给人或 Agent 阅读，并保留原文页码、代码行号、文件版本和来源关系。

**Windows 桌面版是主要入口，Web 版保留。** 桌面安装包携带 Python、PostgreSQL、Meilisearch、Restic 和 MCP 模块，日常使用无需另装开发环境。新资料默认复制到用户自己的资料库，旧的外部来源继续原位只读。

## 版本与下载

当前源码版本为 **0.2.0-rc.4 候选版**。已安装程序的版本取决于实际安装包，更新源码不会自动升级桌面安装。此前候选包的验证范围见[交付记录](docs/reports/2026-10-05-product-candidate-delivery.md)，后续源码修复见[变更记录](CHANGELOG.md)。候选安装包已在本机生成；GitHub 仓库目前仍为私有，尚未发布公开 Release，因此这里不提供虚构的公开下载入口。

正式分发时将使用[GitHub Releases](https://github.com/fingercd/research-asset-hub/releases)，提供 Windows x64 安装包、SHA-256、变更记录和验证范围。候选包尚未在独立的干净 Windows 虚拟机完成首次安装、升级和卸载验收，也没有代码签名；它是待验收候选，不能据本机测试宣称所有新机器都已通过。

- 系统要求：Windows 10 1903 或更新版本、Windows 11，x64。
- 安装到 Program Files 需要管理员授权；资料库和日常运行使用普通用户目录。
- 默认安装模式会在 WebView2 缺失时联网准备它；已安装后，本地 PDF、代码和阅读记录可离线使用。
- 安装包不含个人论文、代码、密钥、历史数据库或开发机来源路径。源码仓库中的历史 `config/` 是个人维护记录；公共安装使用独立的空库配置。

## 开始建立自己的资料库

1. 打开软件，在“研究领域”中点击“新建领域”，填写名称即可建立空领域。
2. 点击“添加资料”，选择领域、上传 PDF、填写论文标题，再检查入库预览。
3. 作者、年份、会议、摘要、简介、链接和标签可在入库后通过详情页“编辑元数据”补充；HTML 和代码也可省略。DOI/arXiv 在草稿阶段选填，入库后的身份更正需走审计流程，普通元数据编辑不修改这些标识。
4. 确认后，平台保存托管 PDF，并通过后台任务抽取可读取的文本、更新检索状态。

批量上传为每篇 PDF 建立独立草稿，逐条显示结果。预览会给出重复线索；同一 DOI/arXiv 等可靠标识参与身份核对，标题相近不会直接强制合并。草稿可修改、取消，重复提交同一草稿不会新增重复资产。

论文里的 HTML 解读、代码、图和方法卡各有独立的提供/缺失状态。未提供可选材料可以正常入库；已经登记但实际丢失的文件仍会被识别为缺失。

## 核心能力

| 能力 | 使用方式 |
| --- | --- |
| 研究领域 | 创建、修改领域，浏览领域门户、研究单元、最近更新与统计；空领域无需 HTML |
| 研究单元 | PDF、导读、代码与论文关系集中展示；列表在数据库中筛选、排序和分页 |
| 搜索 | 标题、中文题名、摘要、DOI/arXiv 等关键词检索，支持领域、日期、作者、材料类型等筛选 |
| 阅读 | 保存阅读状态和上次 PDF 页码，文本选区高亮，批注回跳原页；锚点绑定 PDF 指纹 |
| 个人组织 | 专题集合、保存筛选、批量修改阅读状态和标签，论文比较与 Markdown 导出 |
| 代码资料 | 关联已有代码资产、本地只读目录或远程链接；GitHub/GitLab 可下载固定 commit 的源码快照 |
| 资料迁移 | 领域 ZIP 包导出、校验预览和导入，含文件、manifest、哈希、关系及可选个人状态 |
| 后台维护 | 任务状态、重试/取消、抽取、来源校验和可重建的关键词索引 |
| 外部工具 | 只读 MCP、Obsidian 同步、Zotero 与开放目录导出 |

源码快照不会运行仓库代码，也不下载训练权重、数据集、依赖环境、LFS 大文件或子模块内容。快照固定到解析出的 commit，保留版本和文件指纹；远程链接本身不代表已有本地源码。只读目录引用要求目录保持可访问。

论文关键词匹配不使用正文、summary 导读和标签冒充标题摘要证据。MCP 通过轻量元数据投影广召回并提供分页，正文与代码按需带页码或行号读取。现阶段不生成 embedding，不启动向量模型或 Qdrant，也不要求全库导出 Markdown。代码全文索引默认关闭。

## Agent 接入：只读 MCP

在“设置 → Agent 接入”复制当前安装和资料库生成的配置，加入支持 stdio MCP 的客户端。配置使用随包 Python 和 `-m apps.mcp_server`，包含当前资料库路径；不要求克隆源码或另装 Python。自定义安装路径会反映在生成的配置中。

“测试连接”检查桥接构造和实际数据库读取；真实协议验收另外使用官方 MCP 客户端执行 `initialize`、`list_tools` 和 `call_tool`。本轮已从打包目录、在源码目录之外完成协议读取，详见[交付记录](docs/reports/2026-10-05-product-candidate-delivery.md)和[MCP 使用指南](docs/MCP_USER_GUIDE.md)。

七个工具均只读：`search_assets`、`get_paper`、`read_evidence`、`read_code`、`get_figure`、`get_methodology`、`library_status`。工具保留原文/analysis 区分、候选状态、页码、行号、来源哈希和缺失错误；不导入资料、写批注或运行源码。

默认连接本机共享后台。`initialize` 和列工具不会启动后台；第一次实际读取才按需唤醒或复用桌面服务。多个聊天使用同一后台，关闭聊天不结束资料库。新候选包的真实冷唤醒与多客户端生命周期仍须在安装验收中补测；已有后台上的协议成功不等于这一项已经完成。

## 数据位置、备份与升级

默认数据目录为 `%USERPROFILE%\Documents\ResearchAssetHub`。桌面“设置 → 资料库位置”可以选择空目录建立新库，或选择已有兼容资料库，确认后切换并重启；原资料库保留。切换不是搬运文件，迁移资料可使用备份恢复或领域包。若启动时显式设置 `HUB_DATA_DIR`，界面会显示该限制并禁用切换。

目录内的 `config/` 保存用户配置和托管来源清单，`library/` 保存托管 PDF、源码快照及领域包资料，`postgres-data/` 保存数据库，`logs/` 保存运行日志。安装资源与用户数据分开，升级保留已有来源映射和配置。已有安装的配置迁移会核对旧注册根目录，失败时中止安装并保留原资源。

点击窗口关闭按钮会收起到托盘；需要停止后台时，从托盘选择“退出”。新版本采用手动下载安装升级；托盘的“检查更新”读取本地更新目录，它不是已经配置好的联网自动更新服务。

备份通过维护脚本或 API 执行，当前设置页没有备份操作按钮。备份包含数据库、用户配置、托管 PDF、源码、批注、阅读记录和开放目录。**外部只读引用原件默认不包含在内**，可以显式选择纳入已登记的外部文件；需要根据报告核对实际范围。Restic 仓库默认在用户 `Documents/ResearchAssetHubBackups`，密码保存在系统凭据库中，迁移到新机器需要另行保存相应恢复凭据。软件不自动删除历史快照。

备份标记为 `verified` 表示该记录的快照存在，并且其仓库的存储完整性检查通过。完整检查读取全仓库数据，会比抽样检查更慢；它不等于 PostgreSQL 恢复演练通过，验证结果会单独标明是否执行了恢复演练。

Git 保存源码和配置，不能恢复论文、数据库或本机密钥。同盘备份也不能替代异机备份。恢复演练应在隔离目录与端口进行，验证文件哈希、数据库、批注、实际文件打开和索引重建。具体维护入口见[使用手册](docs/USER_MANUAL.md)与[发行约束](desktop/scripts/RUNTIME-RELEASE.md)。

## Web 与源码开发

React/TypeScript 前端仍位于 `apps/web`，桌面和 Web 共用它。桌面生产版使用内置静态页面，不依赖 8765 网站服务；开发者可运行 Web 版。默认服务只监听本机；保留 Web 不表示已经开放公网或提供多用户托管。

源码环境需要 Python 3.11+、uv、Node.js、根 `package.json` 固定的 pnpm，以及 Docker Desktop/可用的 WSL2 容器环境。桌面构建另外需要 Rust、Tauri 2 CLI 和 Windows 构建工具。

```powershell
git clone https://github.com/fingercd/research-asset-hub.git
cd research-asset-hub
Copy-Item .env.example .env
.\scripts\bootstrap.ps1 -StartInfrastructure
pnpm --filter @research-asset-hub/web build
.\scripts\hub.ps1 start
.\scripts\hub.ps1 status
```

源码配置中的历史来源不会在新机器自动可用。配置自己的来源后，先按[新增资料指南](docs/USER_KNOWLEDGE_BASE_GUIDE.md)执行只读预检、校验、盘点与导入预演，再备份和正式导入。已登记的冻结清单和原文件不可直接覆盖。

| 本机入口 | 默认地址 |
| --- | --- |
| Web | `http://127.0.0.1:8765` |
| API 文档 | `http://127.0.0.1:8767/docs` |
| API 健康检查 | `http://127.0.0.1:8767/api/health` |
| 资产网关 | `http://127.0.0.1:8766/health` |

热更新使用 `.\scripts\hub.ps1 dev`，停止使用 `.\scripts\hub.ps1 stop`。停止服务保留数据库和搜索卷。

## 构建与验证

发行运行时从 `uv.lock` 构建干净的依赖环境，通过白名单复制运行模块和静态页面。PostgreSQL、Meilisearch、Restic 的输入版本与 SHA-256 必须提供并验证；公共配置从 `newconfig/release-defaults` 复制。完整说明见[桌面构建](desktop/README.md)、[运行时组装](desktop/scripts/README.md)和[发行约束](desktop/scripts/RUNTIME-RELEASE.md)。

PostgreSQL 的发行副本加入 Windows 每进程 UTF-8 manifest，用于中文路径。原厂输入保持不变，转换报告记录每个可执行文件的输入/输出哈希；这也是要求 Windows 10 1903+ 的原因。

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

NSIS 安装包通过 `cargo tauri build --bundles nsis` 构建。单独构建生产可执行文件时必须启用 `--features custom-protocol`。当前 CI 执行 Python 与前端检查；本机原生验收、完整恢复和性能测量另行保存记录，不用单元测试替代安装验收。

## 验证范围与已知限制

0.2.0-rc.1 的实现和实际验证详见[交付记录](docs/reports/2026-10-05-product-candidate-delivery.md)，功能变更见[CHANGELOG](CHANGELOG.md)。原生测试使用随包运行时、中文/空格数据路径和独立端口，现有正式安装与数据库未升级。

个人资料库在本轮修改前有 5,915 篇论文、3,326 个代码仓库和 14 个显示领域。这些资料不随软件分发。既有验收发现的 7 个旧来源目录指纹差异、114 个缺失表示和标准检索 Recall@10=0.7576 仍是历史待办；本轮界面与性能优化没有把它们改写为通过。未提供可选 HTML/代码与已登记文件丢失仍分别报告。

全库科研图批处理继续暂缓。已有图和方法卡按需读取，PDF 图试点改用 PDFium 渲染与 pdfplumber 几何提取；PyMuPDF 已从运行依赖移除。扫描 PDF 的 OCR 依赖外部 OCR 环境，不在本轮承诺无条件自动识别。

## 贡献、许可与文档

项目自身代码使用[MIT 许可证](LICENSE)，论文、代码快照及第三方依赖保留各自许可证。安装包附带第三方 notices；psycopg、PostgreSQL 内的部分库等仍有 LGPL 或其他许可义务，不能把整个运行包描述为“全部 MIT”。公开分发前须按实际二进制补齐对应的源代码提供和再分发义务。

- [贡献说明](CONTRIBUTING.md)与[安全报告](SECURITY.md)
- [用户手册](docs/USER_MANUAL.md)与[MCP 指南](docs/MCP_USER_GUIDE.md)
- [Agent 规范](AGENTS.md)与[维护流程](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)
- [本轮优化计划及竞品调查](docs/plans/2026-10-04-product-optimization-and-public-release.md)
- [上一轮元数据与 MCP 交付](docs/reports/2026-10-04-metadata-runtime-delivery.md)
