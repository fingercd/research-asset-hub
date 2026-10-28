# 资料地图：从领域到原文和代码

[返回首页](../README.md) · [使用手册](USER_MANUAL.md) · [MCP](MCP_USER_GUIDE.md)

## 两个层次

**资料总目录与 Git 仓库不是一回事。** 本维护环境的永久资料根为 `D:/ResearchLibrary`；其中的 `research-asset-hub` 才是应用 Git 仓库。其他领域目录存放 PDF、第三方源码、原网页与导读，不能随软件提交。

```text
核心领域/                         ← 本机资料总目录，不是外层 Git 仓库
├─ VLN/、世界模型/、AI for Math/…  ← 已登记原件，按来源规则只读
├─ 个人新增资料/、共享代码/        ← 永久批次资料，不是可随意删除的缓存
├─ research-asset-hub/            ← 应用代码 + 文档 + 来源清单（Git）
├─ ResearchAssetHub/             ← 历史/本机运行副本，不据目录名判断实际安装
├─ ResearchAssetHubData/         ← 本机数据目录之一，以运行配置为准
└─ ResearchAssetHubBackups/      ← 备份区，不上传 Git
```

当前数据目录可被用户设置或环境变量改变，不能从目录名猜测正在运行哪个库。外层新建的 `README.md` 是**本地导航**，Git 版本的解释与维护约定由本页提供。

## 领域查找表

下表名称与 slug 来自 2026-10-28 的只读 `library_status`；本地目录是定位入口，不保证覆盖该领域所有批次。目录、注册领域与物理文件夹并非一对一；各资产实际路径以 source/representation 为准。

| 领域 | MCP 的 `domain` | 本机原始入口 / 相关资料目录 |
| --- | --- | --- |
| Agent 学习 | `agent` | `agent 学习/` |
| AI / 大模型 Infra | `infra` | `infra/` |
| 世界模型 | `world-model` | `世界模型/` |
| 强化学习 | `reinforcement-learning` | `强化学习/` |
| 大模型微调 | `fine-tuning` | `微调/` |
| 视频生成 | `video-generation` | `生成/`；新增批次另有独立来源 |
| 大语言模型与大厂技术报告 | `llm-tech-reports` | `大厂技术报告/` |
| 视觉语言导航 | `vision-language-navigation` | `VLN/` |
| 游戏强化学习 | `game-rl` | `游戏RL/` |
| 弱监督视频异常检测 | `wsvad` | `VAD/` |
| 图像生成与编辑 | `image-editing` | `图像编辑/` |
| 视频大模型压缩 | `video-llm-compression` | `VLM框架3DV压缩/` |
| 具身智能与导航压缩 | `embodied-ai-compression` | `具身智能压缩/`、`导航压缩/` |
| AI for Math | `ai-for-math` | `AI for Math/` |
| RSI 与自我改进 | `rsi-self-improvement` | `RSI与自我改进/`；门户草稿与正式入库分别管理 |

实时数量请查 `library_status()`；日期化台账和验证限制见[本次报告](reports/2026-10-28-repository-organization.md)。不要把固定 README 数字当成实时完整性证据。

## 推荐的三条使用路线

### 1. 系统了解一个领域

在领域门户先看定位、评测与阅读路线，再进入“研究单元”目录。需要完整列表时用 `search_assets(query="", domain="…", asset_type="paper")`，一直翻页到末尾并核对去重数量。静态原始门户可能保留旧数量，不替代数据库当前目录。

### 2. 查具体问题并核实论文结论

用标题、摘要中的关键词与必要标识找候选，拿到资产 ID 后读取 `original` 页码证据。确认任务、数据集、协议、指标及局限，再引用结论。搜索命中、导读判断或会议字段都不能单独证明论文适用性。

### 3. 从论文走到实现

在详情中查看论文→仓库关系，区分官方/第三方、在线/本地、版本与许可证；通过 `read_code` 读取相关文件和行号。快照没有权重、数据集、依赖环境或子模块，不代表一键复现实验。需要跑实验时另建运行工程，不在只读原件中安装依赖或写结果。

## 如何新增而不破坏旧资料

- **日常单篇**：界面托管上传；标题与 PDF 必填，HTML/代码等可后补。
- **已有永久资料**：新建旁车 source，原文件原位只读，走[完整入库流程](USER_KNOWLEDGE_BASE_GUIDE.md)。
- **错误资料**：保留原始声明与哈希，用独立更正来源和审计控制处理，不覆盖旧 PDF 或改冻住的 manifest。
- **新的论文 HTML**：必须先读资料根 `docs/paper-html-standard.md`；既有生成器或旧页面不是自动合格模板。结构、证据、内容和视觉验收分别记录。

正式身份、表示与关联由[来源契约](architecture/source-boundaries.md)约束。资料库原件、代码快照与数据库需专门备份；Git 只能恢复应用与受版本控制的清单。
