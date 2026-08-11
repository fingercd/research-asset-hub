# GitNexus 式图谱体验与代码图谱接入计划

## 目标与成功标准

在不破坏现有论文资产图谱的前提下，把 Hub 图谱升级为“点节点看详情、Ctrl/Cmd 多选并排对比”，并让 GitNexus 作为本地代码图谱随 Hub 启停、独立新窗口打开。

成功标准：

1. 图谱节点单击后在图谱内展示详情抽屉，不再直接跳页。
2. Ctrl/Cmd+点击可多选节点（最多 4 篇），并一键跳转现有 `/compare` 对比页。
3. `hub.ps1 start` 自动启动 `gitnexus serve`（`127.0.0.1:4747`），`status` 能反映其健康，`stop` 能停止。
4. 已登记仓库可按唯一本地路径生成 `.gitnexus/` 索引，dry-run 与失败报告可审计。
5. 前端 lint/typecheck/test/build、后端定向测试、e2e 图谱冒烟全部通过。

## 技术方案与边界

- 图谱继续使用 Cytoscape.js；GitNexus Web UI 不 iframe，通过 Hub 图谱页新窗口打开。
- GitNexus 只写 `.gitnexus/` 索引，统一使用 `--index-only`，不写 AGENTS.md、不覆盖源文件内容；这是对“来源目录严格只读”的唯一例外。
- 索引脚本复用 `scripts/ensure_local_repositories.py` 的数据库查询和 `resolve_editor_path` 模式，按去重后的本地路径逐个索引，失败不中断。
- 多选复用现有 `CompareContext`（上限 4 篇）和 `/compare` 页；加入对比时优先用已加载的完整资产详情，未加载时用图谱节点构造占位，后端按 id 返回完整比较数据。
- GitNexus 进程纳入 Hub 进程状态文件；健康探测用根页面 2xx。

## 实施任务

### 任务 1：GitNexus 仓库索引脚本

- 新增 `scripts/build_gitnexus_index.py`。
- 从数据库查询代码表示并解析本地路径，按唯一目录去重；`--source`/`--limit` 控制范围。
- `--dry-run` 只输出计划；`--apply` 执行 `gitnexus analyze --index-only <path>`，失败不中断。
- 输出 `work/gitnexus/report.json`：路径、来源、表示数、状态、耗时、错误。

### 任务 2：GitNexus 服务随 Hub 启停

- `apps/api/config.py` 新增 `gitnexus_url`（默认 `http://127.0.0.1:4747`）。
- `/api/v1/settings` 在 `app` 下返回 `gitnexus_url`。
- `scripts/hub.ps1` 增加 `gitnexus-serve` 进程、端口、环境变量、健康探测和停止管理。

### 任务 3：图谱详情抽屉与多选对比

- 新增 `GraphNodeDrawer`，普通点击节点打开抽屉，展示完整资产详情、批注和一跳关系。
- Ctrl/Cmd 点击切换 `CompareContext` 选择，画布高亮已选节点；超过 4 篇给出提示。
- 图谱页顶部新增“打开代码图谱”按钮，读取 settings 返回的 GitNexus URL。

### 任务 4：文档与规则更新

- `docs/USER_MANUAL.md` 增加代码图谱章节并更新图谱交互说明。
- `README.md` 增加 GitNexus 先决条件、URL 和功能说明。
- `AGENTS.md` 明确 `.gitnexus/` 是来源只读的唯一例外。

### 任务 5：验证与交付

- `.\.venv\Scripts\python.exe scripts\build_gitnexus_index.py --dry-run`
- `.\.venv\Scripts\python.exe -m pytest -q tests/api/test_v1.py tests/api/test_code_browser.py tests/runtime/test_hub_script.py`
- `pnpm --filter @research-asset-hub/web test -- --run`
- `pnpm --filter @research-asset-hub/web typecheck`
- `pnpm --filter @research-asset-hub/web lint`
- `pnpm --filter @research-asset-hub/web build`
- `pnpm --filter @research-asset-hub/web test:e2e -- --grep "graph"`
- `.\scripts\hub.ps1 start` / `status` / `stop`

## 风险与处理

- 全部仓库索引耗时长：脚本串行执行、可 `--limit`、失败不中断，报告可续跑。
- Windows 长路径可能导致个别仓库 analyze 失败：保留错误原因，不阻塞其他仓库。
- 图谱多选使用占位 Asset 字段可能不全：优先在抽屉中加载完整详情再入对比，后端对比结果按 id 回填完整资产。
