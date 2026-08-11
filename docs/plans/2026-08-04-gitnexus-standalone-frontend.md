# GitNexus 独立前端与图谱解耦实施计划

## 一句话目标与成功标准

把“关系图谱”和“代码图谱”彻底拆成两个独立功能：关系图谱页不再出现任何 GitNexus 入口；代码图谱页升级为站内独立前端，直接内嵌并直达 GitNexus，而不是把用户引到 `http://127.0.0.1:4747/` 根页面新窗口。

成功标准：

1. `/graph` 只做资产关系图谱，页面中没有“打开代码图谱”入口。
2. 左侧导航“关系图谱”与“代码图谱”是两条独立入口，`/code-graph` 路由真实可访问（当前未注册，会 404）。
3. 打开 `/code-graph` 时页面内直接展示 GitNexus 界面，用户无需手动打开 `4747` 根地址。
4. 在代码图谱里点击某个仓库，查看器直达该仓库的 GitNexus 图谱（通过 `?project=<registry name>`），而不是停在 GitNexus 首页。
5. 前端 lint/typecheck/test/build、后端定向测试、e2e 图谱冒烟全部通过。

## 现状核对

- `apps/web/src/App.tsx` 没有注册 `code-graph` 路由，但 `AppShell.tsx` 已有 `/code-graph` 导航项，因此当前点击会落到 404。
- `apps/web/src/pages/GraphPage.tsx` 顶部有“打开代码图谱”按钮，会让用户误以为代码图谱属于关系图谱。
- `apps/web/src/pages/CodeGraphPage.tsx` 目前的仓库按钮只是 `<a href={gitnexusUrl} target="_blank">`，主流程跳到 GitNexus 根页面，没有直达仓库。
- 已验证 GitNexus 具备内嵌与直达能力：
  - 根页面响应没有 `X-Frame-Options` 和 `Content-Security-Policy`，可在 Hub 页面内 iframe。
  - GitNexus Web UI 读取 `?project=<仓库名>` 后自动连接并加载该仓库；仓库名来自 `~/.gitnexus/registry.json` 的 `name` 字段。
  - `GET http://127.0.0.1:4747/api/repos` 返回 `name/path/stats`，并对 `http://127.0.0.1:8765` 回显 `Access-Control-Allow-Origin`，可直接用于状态探测。

## 关键决策

- D1：关系图谱与代码图谱完全独立。删除 `GraphPage` 中的 GitNexus 按钮和 settings 查询，两个页面只通过侧栏导航关联。
- D2：代码图谱页做成站内独立前端：左侧保留按研究方向分组的仓库导航，主区域内嵌 GitNexus Web UI；默认加载 GitNexus，点击仓库后 iframe 切到 `?project=<name>` 直达。
- D3：直达仓库所需的 `name` 由后端 `/api/v1/code-graph` 从 `~/.gitnexus/registry.json` 按本地路径匹配后下发为 `gitnexus_repo_name`，避免前端拼接路径或重复请求 GitNexus。
- D4：保留“新窗口打开 GitNexus”作为兜底按钮，但不再是主入口；若 GitNexus 服务离线，页面给出明确状态并保留 Hub 仓库列表可浏览。

## 任务 1：注册独立路由并移除关系图谱页耦合

**涉及文件或模块：**
- 修改：`apps/web/src/App.tsx`
- 修改：`apps/web/src/pages/GraphPage.tsx`
- 测试：`apps/web/e2e/app.spec.ts`

**接口与依赖：**
- 产出：`/code-graph` 可渲染 `CodeGraphPage`；`/graph` 不再依赖 `/api/v1/settings`。

**实施内容：**
1. 在 `App.tsx` 增加 `CodeGraphPage` 的 lazy import，并在 `AppShell` 子路由中注册 `<Route path="code-graph" element={<CodeGraphPage />} />`，与 `graph` 路由并列。
2. 在 `GraphPage.tsx` 删除 `settings` useQuery、`gitnexusUrl` 常量和“打开代码图谱”链接；同步移除不再使用的 `ExternalLink` import。
3. 保持 `GraphPage` 的邻域/路径、详情抽屉、Ctrl/Cmd 多选、已选对比逻辑不变。

**验收标准：**
- 直接访问 `/code-graph` 能渲染代码图谱页，不再跳 404。
- `/graph` 页面标题和画布不变，页面中不存在“打开代码图谱”文本或链接。

**验证：**
- 运行：`pnpm --filter @research-asset-hub/web typecheck`、`pnpm --filter @research-asset-hub/web lint`。
- e2e：在 `routes` 列表加入 `["/code-graph", "代码图谱"]`，并给图谱测试追加断言 `page.getByText("打开代码图谱")` 不可见。
- 预期：全部通过；人工打开 `/graph` 确认只剩关系图谱功能。

## 任务 2：后端 `/api/v1/code-graph` 下发 GitNexus 仓库名

**涉及文件或模块：**
- 修改：`apps/api/routes_v1.py`
- 测试：`tests/api/test_code_browser.py` 或 `tests/api/test_v1.py`

**接口与依赖：**
- 产出：`CodeGraphRepository` 增加可选字段 `gitnexus_repo_name: string | null`；`gitnexus_indexed` 语义不变。
- 数据源：`~/.gitnexus/registry.json` 的 `path` 与 `name`。

**实施内容：**
1. 将 `_gitnexus_registry_paths()` 扩展为 `_gitnexus_registry_index(registry_path: Path | None = None) -> dict[str, dict[str, Any]]`，键为 `Path(item["path"]).resolve().casefold()`，值为 `{"name": item["name"], "indexed_at": item["indexedAt"]}`；缺省路径仍为 `Path.home() / ".gitnexus" / "registry.json"`。
2. `/code-graph` 循环中，对每个 `entry` 找到第一个命中 registry 的 `paths` 项，写入 `entry["gitnexus_repo_name"] = name or None`。
3. 保持聚合、排序、`total_count/indexed_count/domains` 结构不变，未索引仓库的 `gitnexus_repo_name` 为 `null`。

**验收标准：**
- 对已索引仓库，`/api/v1/code-graph` 返回的 `gitnexus_repo_name` 与 `registry.json` 的 `name` 完全一致。
- 未索引或 registry 缺失时不报错，字段为 `null`。

**验证：**
- 新增测试：用临时目录写入最小 `registry.json`，通过参数注入调用 `_gitnexus_registry_index`，断言路径归一化与 name 提取；再用 monkeypatch 的 `Path.home` 走 `code_graph` 路由断言响应字段。
- 运行：`.\.venv\Scripts\python.exe -m pytest -q tests/api/test_v1.py tests/api/test_code_browser.py`。
- 手工：`.\.venv\Scripts\python.exe -m apps.api.main` 后请求 `/api/v1/code-graph`，抽查一个已索引仓库的 `gitnexus_repo_name`。

## 任务 3：代码图谱独立前端（内嵌并直达 GitNexus）

**涉及文件或模块：**
- 修改：`apps/web/src/pages/CodeGraphPage.tsx`
- 修改：`apps/web/src/api/types.ts`
- 修改：`apps/web/src/styles.css`
- 测试：`apps/web/src/pages/CodeGraphPage.test.tsx`

**接口与依赖：**
- 消费：`api.codeGraph()`、`api.settings()`、后端新增的 `gitnexus_repo_name`。
- 产出：页面级 GitNexus 查看器；仓库选择状态；服务在线/离线状态。

**实施内容：**
1. 类型：`CodeGraphRepository` 增加 `gitnexus_repo_name?: string | null`。
2. 页面状态：
   - `selectedRepo: { name: string; title: string } | null`，默认 `null` 时 iframe 加载 `${gitnexusUrl}` 根界面。
   - `viewerSrc` 计算：选中仓库时 `${gitnexusUrl}/?project=${encodeURIComponent(name)}`。
   - `viewerNonce` 用于“重新加载”按钮强制刷新 iframe。
3. 布局（保留现有研究方向分组）：
   - 顶部 `PageHeader` + 汇总数字 + 工具栏：“GitNexus 状态”chip、`重新加载图谱`、`新窗口打开`。
   - `.code-graph-workspace` 两栏：左栏为研究方向下的仓库导航（仓库行变为 `button` 选择器，附“已索引/未索引”状态），右栏为 `.gitnexus-viewer`，内含 `<iframe title="GitNexus 代码图谱" src={viewerSrc}>`，高度约占视口剩余空间。
   - 移动端单列：仓库列表在上，查看器在下。
4. 仓库行动作：
   - `打开图谱`（默认主按钮）：设置 `selectedRepo`，让 iframe 直达该项目。
   - `新窗口`：`<a href={viewerSrc} target="_blank">` 兜底。
   - `资产详情`：保留原 `/assets/:id` 链接。
5. GitNexus 状态：
   - 用 `fetch(`${gitnexusUrl}/api/repos`, { signal })` + 3 秒超时探测；成功显示“服务在线”，失败显示“服务未启动”，并提示可先运行 `.\scripts\hub.ps1 start`。
   - iframe `onLoad` 后清除临时“正在加载”遮罩；`onError` 或 8 秒无 `onLoad` 显示离线遮罩但保留仓库列表。

**验收标准：**
- `/code-graph` 打开后页面内直接出现 GitNexus iframe，不再要求用户先打开 `4747` 根地址。
- 点击某个已索引仓库，iframe `src` 变为 `${gitnexusUrl}/?project=<urlencoded name>`。
- 未选择仓库时 iframe 为 GitNexus 根界面；切换仓库时 iframe 随之更新。
- GitNexus 服务停止时页面显示离线状态，仓库列表和资产详情仍可用。

**验证：**
- 更新 `CodeGraphPage.test.tsx`：mock `codeGraph` 两个仓库（一个带 `gitnexus_repo_name`、一个未索引）、mock `settings`；断言默认 iframe `src`、点击仓库后 `src` 含 `?project=...`、离线状态文案。
- 运行：`pnpm --filter @research-asset-hub/web test -- --run`、`typecheck`、`lint`、`build`。
- 手工：`.\scripts\hub.ps1 start` 后打开 `http://127.0.0.1:8765/code-graph`，点击不同方向下的仓库并确认查看器直达。

## 任务 4：e2e 与文档更新

**涉及文件或模块：**
- 修改：`apps/web/e2e/app.spec.ts`
- 修改：`docs/USER_MANUAL.md`
- 修改：`README.md`
- 修改：`AGENTS.md`

**实施内容：**
1. e2e：
   - `routes` 数组加入 `["/code-graph", "代码图谱"]`。
   - 新增“代码图谱内嵌 GitNexus 并直达仓库”用例：mock `/api/v1/code-graph`、`/api/v1/settings`，断言 iframe 存在且点击仓库后 `src` 含 `?project=`。
   - 在 Cytoscape 图谱用例中追加 `await expect(page.getByText("打开代码图谱")).toHaveCount(0)`。
2. 用户手册“代码图谱（GitNexus）”一节改写：说明代码图谱是独立页面、页内直接内嵌 GitNexus、点击仓库直达对应项目、`?project=` 参数、离线兜底与新窗口入口；删除“图谱页顶部打开代码图谱”的说法。
3. README 特性列表把“独立新窗口打开”改为“站内独立页面内嵌打开并直达仓库”。
4. AGENTS.md 的 GitNexus 例外段落补充：代码图谱页通过 `?project=<registry name>` 直达仓库，`name` 仅来自 `registry.json`，不得写入源文件。

**验收标准：**
- 文档描述与实际 UI 一致；e2e 覆盖路由可达、iframe 直达、关系图谱不再含 GitNexus 入口。

**验证：**
- 运行：`pnpm --filter @research-asset-hub/web test:e2e -- --grep "代码图谱|graph"`；文档由人工核对。

## 任务 5：验证与交付

**验证命令：**
- `.\.venv\Scripts\python.exe -m pytest -q tests/api/test_v1.py tests/api/test_code_browser.py tests/runtime/test_hub_script.py`
- `pnpm --filter @research-asset-hub/web test -- --run`
- `pnpm --filter @research-asset-hub/web typecheck`
- `pnpm --filter @research-asset-hub/web lint`
- `pnpm --filter @research-asset-hub/web build`
- `pnpm --filter @research-asset-hub/web test:e2e -- --grep "代码图谱|graph"`
- `.\scripts\hub.ps1 status`

**预期结果：**
- 后端定向测试、前端全部单测、lint/typecheck/build、e2e 图谱相关用例通过。
- `hub.ps1 status` 显示 `gitnexus-serve` 与 Hub 服务 ready。
- 验证后启动 Hub，打开 `/code-graph`：页面内直接展示 GitNexus，点击仓库可直达对应图谱；打开 `/graph` 确认没有 GitNexus 入口。

## 待确认事项

- 内嵌方案采用 iframe + `?project=` 直达，底层 URL 仍是 `http://127.0.0.1:4747`，只是用户不再手动打开该地址。若希望浏览器地址栏完全不出现 4747，需要 Hub 做 `/gitnexus/*` 同源反向代理，涉及静态资源与 SSE 转发，建议作为后续独立任务，不在本计划默认范围内。
- 本计划不重写 GitNexus 的图形渲染，仍复用其官方 Web UI。若希望 Hub 自研一套读取 `/api/graph` 的图形前端，工作量会显著增加，属于另一种方案。

## 风险与回退

- GitNexus 未来版本若改变 `?project=` 参数或添加 `X-Frame-Options`，内嵌直达会失效：降级为“新窗口打开”按钮，并把该行为记录为待跟进项；前端对 iframe `onError`/超时已有离线兜底。
- `project` 参数必须与 `registry.json` 的 `name` 完全一致：由后端读取并下发，前端不自行猜测名称。
- `/code-graph` 数据仍来自 Hub 数据库与 `registry.json`；若 GitNexus 服务离线，仓库列表、索引状态和资产详情仍可正常展示。
