# 脚本索引：先看副作用，再运行

[首页](../README.md) · [工具箱](../tools/README.md) · [完整入库手册](../docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)

下列路径均相对应用仓库根。**“检查”可能写本机报告，但不代表会写数据库；“维护”可能改变库、索引或外部笔记。** 不要批量执行本目录全部脚本，也不要将日期化修复脚本作为日常清理命令。

## 1. 开发与只读检查

| 入口 | 作用 | 边界 |
| --- | --- | --- |
| [check_repository.py](check_repository.py) | Git 跟踪文件卫生、Markdown 本地链接 | 离线只读；不扫描原件、不联网、不验证内容级密钥或锚点 |
| [hub.ps1](hub.ps1) `status -Json` | CLI bootstrap 与报告位置 | 不代表原生服务就绪；实际状态在桌面查看 |
| [hub.ps1](hub.ps1) `preflight / validate / inventory` | 配置、来源和资料盘点 | 读来源、写 `work/validation/` 报告；不正式导入 |
| [hub.ps1](hub.ps1) `ingest -Source <alias> -DryRun` | 目标来源预演 | 不写正式库；正式导入仍需全来源门禁 |
| [verify_source_integrity.py](verify_source_integrity.py) | 核对已登记来源完整性 | 必须确认 `--sources-config` 对准目标库，默认是源码配置 |
| [check_unit_actions.py](check_unit_actions.py) | PDF / HTML / 代码动作可用性门禁 | 只检查声明材料；按钮绿不等于科研质量验收 |
| [verify_knowledge_mcp.py](verify_knowledge_mcp.py) | 协议和知识读取验证 | 对实际连接负责；不是全库覆盖证明 |

```powershell
# 新文档暂存后可直接检查；--json 便于保存机器可读结果。
.\.venv\Scripts\python.exe scripts/check_repository.py
.\.venv\Scripts\python.exe scripts/check_repository.py --json

# 只检查正在编辑的 Markdown，允许显式列出的草稿互相引用。
.\.venv\Scripts\python.exe scripts/check_repository.py --files README.md docs/README.md
```

文档检查默认对 `docs/plans/`、`docs/reports/`、`docs/superpowers/` 的缺失或未入 Git 目标发 warning，保留历史真实性；`--strict-history` 可升级为错误。存在但只在本机 `work/` 的文件不能算 fresh clone 可用。

## 2. 启动与依赖：会改变运行环境

| 入口 | 作用 | 运行前确认 |
| --- | --- | --- |
| [bootstrap.ps1](bootstrap.ps1) | 安装开发依赖，不启动服务 | frozen lockfile；不覆盖正式环境 |
| [desktop.ps1](desktop.ps1) `build / plan / run` | 内嵌静态界面构建、只读计划与显式原生开发 | plan 不启动；run 明确 runtime/data-dir，冲突端口不自动接管 |
| [run_knowledge_mcp.py](run_knowledge_mcp.py) | 源码 MCP 辅助入口 | 通常优先 `python -m apps.mcp_server` 或安装版生成配置 |
| [build_startup_archive.py](build_startup_archive.py) | 显式生成本机运行时导入缓存 | 不是通用安装步骤；公共打包默认不生成 |

## 3. 资料维护：需要目标、备份与验收

| 入口 | 主要副作用 | 正确使用方式 |
| --- | --- | --- |
| [hub.ps1](hub.ps1) `ingest -All` | 写 PostgreSQL、冻结新来源、建立关系 | 全量 dry-run → 备份 → 导入 → 不改输入再导入验证幂等 |
| [seed_jobs.py](seed_jobs.py) | 向库中排队抽取任务 | 优先按 source/类型限定，不为普通查询全库排队 |
| [build_indexes.py](build_indexes.py) | 写关键词引擎与索引台账 | 默认资产/批注范围；不自动建立向量或代码全文索引 |
| [reconcile_repository_relations.py](reconcile_repository_relations.py) | `--apply` 建立论文与代码关联 | 先预览，按真实证据决定 confirmed / candidate |
| [create_backup.py](create_backup.py) | 建立 Restic 快照与记录 | 核对托管与外部原件的实际范围；不等于异机备份 |
| [restore_rehearsal.py](restore_rehearsal.py) | 恢复到隔离环境 | 独立目录、端口和明确快照；不得覆盖正式库 |
| [export_catalog.py](export_catalog.py) · [export_zotero.py](export_zotero.py) | 生成导出文件 | 检查私有状态与输出范围；Zotero 导入仍需用户确认 |
| [sync_obsidian.py](sync_obsidian.py) | 可写外部笔记与同步状态 | 先 `--preview-only`；冲突需审查，不覆盖已有笔记 |
| [audit_html.py](audit_html.py) · [apply_html_policies.py](apply_html_policies.py) | 审计报告 / 应用 HTML 策略 | 两者副作用不同；静态审计不是内容与视觉验收 |
| [generate_release_report.py](generate_release_report.py) | 汇总来源、恢复与发行门禁 | 必须读失败项，不以退出码替代完整验收 |

具体参数以各脚本 `--help` 和[操作手册](../docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)为准。这里不提供脱离目标库的“一键维护全部”命令。

## 4. 历史批次与高风险修复

这些代码保留用于审计和复现旧处理逻辑，**不是新的通用默认流程**：

- `build_vln_*`、`audit_vln_*`、`verify_vln_*`、`build_compression_intake_sidecars.py`：固定资料批次与 VLN 导读/代码检查。
- [build_canonical_snapshot.py](build_canonical_snapshot.py)、[build_domain_portal_snapshot.py](build_domain_portal_snapshot.py)：受限定 profile 的旁车构建；现有冻结输出不可重跑覆盖。
- [delete_vln_uav_assets.py](delete_vln_uav_assets.py)、[delete_keyword_documents_for_assets.py](delete_keyword_documents_for_assets.py)：会删除特定业务或索引记录，不能作为“清理仓库”动作。
- [cancel_superseded_index_jobs.py](cancel_superseded_index_jobs.py)、[supersede_extraction_generation.py](supersede_extraction_generation.py)、[retry_failed_extractions.py](retry_failed_extractions.py)：改变任务或抽取状态，先核实当前活跃任务。
- [seed_recovery_fixture.py](seed_recovery_fixture.py)：恢复演练 fixture，不注入正式资料库。
- [enrich_citations.py](enrich_citations.py)、[enrich_paper_abstracts.py](enrich_paper_abstracts.py)：元数据补全，不是离线只读盘点。

没有删除上述工具，因为日期化记录和测试可能仍引用它们；也没有把历史输入当成可任意重建的缓存。增加新脚本时在本页标明用途、输入、输出和副作用，测试放 `tests/scripts/` 或对应模块。
