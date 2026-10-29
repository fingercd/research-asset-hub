# 文档导航

[项目首页](../README.md) · [资料地图](LIBRARY_MAP.md) · [代码地图](architecture/code-map.md)

文档按“要完成的任务”组织。**指南说明现在怎样使用；计划说明当时想做什么；报告说明当时实际验证了什么。** 三者不要互相替代。

## 阅读与管理资料

| 文档 | 解决的问题 |
| --- | --- |
| [使用手册](USER_MANUAL.md) | 搜索、PDF、代码、批注、领域、集合、设置和故障排查 |
| [资料地图](LIBRARY_MAP.md) | 领域、资料根、论文与代码的对应关系 |
| [MCP 指南](MCP_USER_GUIDE.md) | Agent 接入、目录分页、会议年份筛选、证据引用与限制 |
| [自行增加知识库](USER_KNOWLEDGE_BASE_GUIDE.md) | 界面托管与外部旁车两种入库方式 |
| [配置说明](../config/README.md) | 源码配置、用户配置、发行空配置和冻结清单的区别 |

## 开发与维护

| 文档 | 解决的问题 |
| --- | --- |
| [隔离开发指南](DEVELOPMENT.md) | 环境、独立数据目录/数据库/端口、启动和日志 |
| [贡献约定](../CONTRIBUTING.md) | 测试分层、Git、代码与文档同步、现有改动保护 |
| [代码地图](architecture/code-map.md) | 应用入口、核心模块和数据流 |
| [脚本索引](../scripts/README.md) | 哪个命令只检查、哪个会启动服务或写库 |
| [工具箱](../tools/README.md) | 通用材料处理与历史固定批次工具的边界 |
| [桌面开发](../desktop/README.md) | Tauri 壳、退出、构建和原生验收 |
| [运行时组装](../desktop/scripts/README.md) · [发行约束](../desktop/scripts/RUNTIME-RELEASE.md) | 白名单打包、第三方依赖、安装/升级/许可 |
| [安全报告](../SECURITY.md) | 敏感信息、私下报告与发布边界 |

## 资料契约与 Agent 必读

- [项目强制规则](../AGENTS.md)：来源只读、永久身份、可见失败与既有改动保护。
- [知识库增量操作手册](AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)：预检、dry-run、备份、幂等导入、抽取、索引、实际打开与交付。
- [来源边界](architecture/source-boundaries.md)：冻结、原位引用、纠错与受控路径。
- [资料入库补充规则](../prj.md)：材料选填、代码关联和历史入库教训。
- [Schema 索引](schemas/README.md)：资产、目录、来源快照与领域门户契约。
- [manifest 示例](templates/knowledge-manifest.example.json) · [source 示例](templates/knowledge-source.example.yaml)：仅用于新来源，不覆盖已有清单。

## 公共源码边界与历史设计

| 入口 | 怎样使用 |
| --- | --- |
| [公共源码分发说明](PUBLIC_SOURCE_DISTRIBUTION.md) | 应用功能保留范围、资料与历史隔离、测试条件 |
| [变更记录](../CHANGELOG.md) | 定位源码行为变化；不是当前安装包的验收结论 |
| [桌面资料工作台设计](design/desktop-library-workbench.md) | 检索、阅读、保存视图和错误恢复的设计 |
| [界面设计系统](design/research-workbench-visual-system.md) | UI 视觉约定 |
| [历史计划](plans/) · [早期设计](superpowers/) | 保留日期和未完成项，不据此自动重跑旧批次 |

个人资料库的来源台账、冻结快照、原件、历史运行报告和日志不随公共源码分发。当前代码测试与公开安装包验收应分别报告，不用个人机器上的历史结果替代干净环境验证。

产品与开发均使用内嵌静态前端的原生桌面，不再提供独立网站端口。API 与隔离文件网关仍是桌面/MCP 所需的本机内部服务，不能与已经退役的网站入口混淆。
