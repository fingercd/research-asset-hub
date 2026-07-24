# Research Asset Hub 文档入口

## 给用户

- [完整使用手册](USER_MANUAL.md)：如何启动网站，以及每个界面、按钮和常用研究工作流的实际效果。
- [自行增加知识库指南](USER_KNOWLEDGE_BASE_GUIDE.md)：如何加入一组 PDF、代码、HTML、Notebook 或 Markdown，并完成导入、抽取、索引与验证。

## 给后续 Agent

- [项目级强制规则](../AGENTS.md)：后续 Agent 进入本项目时应自动读取的不可破坏约定。
- [知识库增量操作手册](AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)：Agent 每次代用户新增知识单元时必须执行的标准流程和完成门槛。

## 架构与契约

- [来源边界](architecture/source-boundaries.md)
- [统一资产 Schema](schemas/README.md)
- [标准知识单元 manifest 示例](templates/knowledge-manifest.example.json)
- [标准来源配置示例](templates/knowledge-source.example.yaml)

网站入口为 <http://127.0.0.1:8765>。项目总览、安装命令和发布验证仍以根目录 [README](../README.md) 为准。
