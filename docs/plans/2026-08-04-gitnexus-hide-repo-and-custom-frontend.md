# GitNexus 默认隐藏仓库与前端改造实施计划

## 一句话目标与成功标准

让代码图谱页默认不显示 `OpenBMB/BMTools`，点击“打开图谱”时在当前窗口（Hub 当前标签页内）直接打开该仓库图谱，并把 10 个研究方向做成 10 个窗口、每行三个、窗口内仓库列表可滑动，同时评估/落地“改 GitNexus 前端”的方案。

成功标准：

1. 打开 `/code-graph` 时，仓库列表与统计默认不包含 `OpenBMB/BMTools`；页面提供“显示已隐藏仓库”开关，展开后可再次看到它。
2. 点击任一已索引仓库的“打开图谱”，在当前窗口内嵌查看器中加载对应仓库图谱，不跳转到新标签页或离开 Hub。
3. 代码图谱页把 10 个研究方向渲染为 10 个窗口，桌面端每行 3 个（最后一行不足 3 个时靠左对齐），移动端每行 1 个；每个窗口内的仓库列表使用独立滑动区域，默认不把所有仓库平铺展示。
4. 给出“改 GitNexus 前端”的可行方案；在用户确认方案后，至少落地一个可运行的自定义前端（自研轻量前端或 fork 官方前端构建产物）。
5. 前端 lint/typecheck/test/build、后端定向测试、e2e 冒烟全部通过。

## 现状核对

- `/code-graph` 已是站内独立页，右侧 iframe 内嵌 GitNexus，`?project=<registry name>` 可直达仓库；“打开图谱”按钮当前就在 Hub 当前标签页内打开。
- 当前 `/code-graph` 的左侧仓库导航按方向纵向堆叠，10 个方向会全部平铺；参考 `apps/web/src/pages/DomainsPage.tsx` 的 `domain-grid`，该页面已用 `repeat(3, minmax(0, 1fr))` 实现三列卡片布局，移动端降为单列。
- `OpenBMB/BMTools` 已存在：资产 ID `019f93e5-5ab7-70d3-a7de-579fa3ba69f2`，GitNexus registry name `BMTools`，位于 `Agent 学习` 方向，状态为已索引。
- `/api/v1/code-graph` 当前返回全部 390 个仓库、10 个方向，未做隐藏过滤。
- GitNexus npm 包 `web/` 只包含构建产物，没有前端源码；服务端读取 `GITNEXUS_WEB_DIST` 环境变量覆盖静态前端目录（`dist/server/api.js` 的 `resolveWebDistDir`）。
- GitNexus `/api/graph?repo=<name>` 返回 `{nodes, relationships}` JSON，另有 `/api/query`、`/api/search`、`/api/file`、`/api/processes`、`/api/clusters`，CORS 已允许 Hub 的 8765 端口，Hub 自研前端读取这些 API 可行。

## 关键决策

- D1：隐藏规则放在 Hub 侧配置中，不修改 GitNexus 索引。新增 `config/code-graph-hidden.yaml`，按 registry name 和/或资产标题维护隐藏名单，后端 `/api/v1/code-graph` 过滤并返回 `hidden_count`，保证统计与列表一致。
- D2：“当前窗口打开”采用当前页内嵌 iframe 方案（现有主行为），不使用 `target="_blank"` 作为主按钮；“新窗口”仅作为兜底按钮保留。
- D3：代码图谱主布局改为“研究方向窗口矩阵”：桌面端 `repeat(3, minmax(0, 1fr))` 三列，每个窗口头部显示方向名和已索引计数，窗口内仓库列表固定高度并使用纵向滚动；右下角“打开图谱”按钮把仓库送入当前窗口查看器。它替代当前左侧纵向导航，查看器仍保持右侧或下方主区域。
- D4：GitNexus 前端改造采用双轨评估，默认推荐 Hub 自研轻量前端（方案 A），因为它与 Hub 设计体系一致、完全可控、不依赖升级 npm 包时同步维护 fork；若要完整保留搜索、AI 对话、代码浏览等高级功能，再采用 fork 官方 `gitnexus-web` 并用 `GITNEXUS_WEB_DIST` 接入（方案 B）。
- D5：隐藏名单默认只影响 Hub 的 `/code-graph` 页面；GitNexus 官方首页仍会显示全部索引仓库，除非后续选择方案 B 并同步过滤前端列表。

## 任务 1：配置化隐藏仓库与后端过滤

**涉及文件或模块：**
- 新增：`config/code-graph-hidden.yaml`
- 修改：`apps/api/config.py`
- 修改：`apps/api/routes_v1.py`
- 测试：`tests/api/test_v1.py`

**接口与依赖：**
- 产出：`ApiSettings.code_graph_hidden_path`、`/api/v1/code-graph` 新增 `hidden_count`，`CodeGraphRepository` 新增 `hidden: boolean`。
- 数据源：`~/.gitnexus/registry.json`、`config/code-graph-hidden.yaml`。

**实施内容：**
1. 新建 `config/code-graph-hidden.yaml`：
   ```yaml
   schema_version: 1
   hidden_repositories:
     - registry_name: BMTools
       reason: 默认不展示该仓库
   ```
   匹配规则支持 `registry_name`（精确）和 `title`（大小写不敏感子串）两类字段，至少命中一类即隐藏。
2. `ApiSettings` 增加 `code_graph_hidden_path: Path = PROJECT_ROOT / "config" / "code-graph-hidden.yaml"`。
3. `routes_v1.py` 增加 `_load_code_graph_hidden(settings)` 读取配置；`/code-graph` 中给每个 `entry` 设置 `hidden`，过滤后再聚合分组，并返回 `hidden_count`；`total_count/indexed_count` 均只统计未隐藏仓库。
4. 保留隐藏仓库的原始记录，前端“显示已隐藏”开关需要时可从 `domains[].hidden_repositories` 读取。

**验收标准：**
- 默认响应中不存在 `BMTools` 或 `OpenBMB/BMTools`，`hidden_count == 1`。
- 未隐藏仓库的聚合数字与当前一致；隐藏规则配置为空时不改变现有行为。

**验证：**
- 新增测试：写入含 BMTools 的最小 registry 与隐藏配置，断言 `/code-graph` 返回 `hidden_count=1` 且响应不含 `BMTools`；再断言空配置时 `hidden_count=0`。
- 运行：`.\.venv\Scripts\python.exe -m pytest -q tests/api/test_v1.py`。

## 任务 2：前端默认隐藏与“当前窗口打开”

**涉及文件或模块：**
- 修改：`apps/web/src/pages/CodeGraphPage.tsx`
- 修改：`apps/web/src/api/types.ts`
- 修改：`apps/web/src/styles.css`
- 测试：`apps/web/src/pages/CodeGraphPage.test.tsx`

**接口与依赖：**
- 消费：`/api/v1/code-graph` 的 `hidden`、`hidden_count`、`domains[].hidden_repositories`。
- 产出：默认隐藏列表、显示隐藏开关、当前窗口内打开仓库图谱。

**实施内容：**
1. `CodeGraphResponse` 增加 `hidden_count`；`CodeGraphRepository` 增加 `hidden?: boolean`。
2. 页面新增布尔状态 `showHidden`（默认 `false`）：默认过滤掉 `hidden: true` 的仓库；工具栏显示“已隐藏 N 个仓库”chip，点击“显示已隐藏仓库”按钮后 `showHidden=true`，列表恢复显示并带“已隐藏”标记。
3. 所有仓库行与“打开图谱”按钮统一走 `onSelect(repository)`，仅更新 `selectedRepo` 与 iframe `src`，即 Hub 当前标签页内嵌打开；不把主按钮设为 `target="_blank"`。`新窗口` 按钮继续保留为兜底。
4. 当 `selectedRepo` 是隐藏仓库时，查看器头部显示“已隐藏仓库（手动打开）”，正常加载图谱。
5. 样式：增加隐藏开关、隐藏标记、切换后行背景样式；保持移动端布局不变。

**验收标准：**
- 默认页面不显示 `OpenBMB/BMTools`；统计中的仓库数等于未隐藏数量。
- 点击“显示已隐藏仓库”后出现 BMTools，点击其“打开图谱”后 iframe `src` 为 `http://127.0.0.1:4747/?project=BMTools`，且 Hub 地址栏仍停留在 `/code-graph`。
- 新窗口按钮仍可正常打开同一 project URL。

**验证：**
- 更新 `CodeGraphPage.test.tsx`：mock 一个隐藏仓库，断言默认不渲染、打开开关后渲染、点击后 iframe src 含 `project=BMTools`。
- 运行：`pnpm --filter @research-asset-hub/web test -- --run`、`typecheck`、`lint`、`build`。

## 任务 3：研究方向三列窗口与仓库横向/纵向滚动

**涉及文件或模块：**
- 修改：`apps/web/src/pages/CodeGraphPage.tsx`
- 修改：`apps/web/src/styles.css`
- 测试：`apps/web/src/pages/CodeGraphPage.test.tsx`

**接口与依赖：**
- 消费：`/api/v1/code-graph` 的 `domains[]`；`CodeGraphDomain` 已有 `repositories`。
- 产出：`code-graph-window-grid` 三列布局与每个方向的独立滚动仓库列表。

**实施内容：**
1. 保留 `CodeGraphPage` 顶部汇总、工具栏、查看器；把左侧 `.code-graph-domains` 替换为 `.code-graph-window-grid`，按 `domains` 渲染 10 个窗口，窗口结构复用领域卡的视觉层级（方向图标、方向名、`已索引 x/y`、仓库列表、查看领域/打开仓库操作）。
2. 窗口内仓库列表使用 `.code-graph-window-scroll`：固定 `max-height`（桌面约 320px、移动约 240px），`overflow-y: auto`；仓库行只显示标题与状态 chip，把“打开图谱/新窗口/资产详情”收进行尾紧凑按钮区，避免单个方向仓库过多时把页面无限撑高。
3. 每个窗口保持独立滚动，不做全页长列表；默认按 `hidden` 过滤，打开“显示已隐藏仓库”后窗口内追加隐藏仓库并带标记。
4. 查看器放窗口矩阵下方（若仍保留左右分栏，则在窄屏自动换到下方），点击任意窗口中的“打开图谱”后 `selectedRepo` 更新，查看器在当前窗口加载对应仓库；同时把对应仓库滚动到查看器上方提示。
5. CSS 响应式：桌面 `repeat(3, minmax(0, 1fr))`，`max-width: 1100px` 降为 `repeat(2, ...)`，`max-width: 640px` 降为单列；每行不足 3 个时自然靠左。

**验收标准：**
- 打开 `/code-graph` 后可见 10 个方向窗口，桌面一行最多 3 个，窗口之间有明确间距与边框。
- 任意窗口内仓库列表超过可视高度时出现纵向滚动条，页面主体不因仓库数量无限变长。
- 点击某窗口内仓库的“打开图谱”后，Hub 仍在 `/code-graph`，查看器加载 `?project=<name>`。

**验证：**
- 更新 `CodeGraphPage.test.tsx`：mock 3 个方向、每个方向 3 个仓库，断言窗口数量、三列 class、滚动容器存在、点击后 iframe src 正确。
- 运行：`pnpm --filter @research-asset-hub/web test -- --run`、`typecheck`、`lint`、`build`；再用 Playwright 截 1440px 与 390px 两档人工检查三列/单列与滚动。

## 任务 4：GitNexus 前端改造方案评估与落地

**涉及文件或模块：**
- 方案 A（推荐）：新增 `apps/web/src/components/CodeGraphViewer.tsx`、`apps/web/src/api/client.ts` 增加 GitNexus API 请求，修改 `CodeGraphPage.tsx` 将 iframe 替换为自研查看器（可先以 feature flag 灰度）。
- 方案 B：`scripts/hub.ps1` 为 `gitnexus-serve` 增加 `GITNEXUS_WEB_DIST` 环境变量；`docs/` 记录 fork 来源与构建命令。

**接口与依赖：**
- GitNexus API：`/api/repos`、`/api/repo?repo=<name>`、`/api/graph?repo=<name>`、`/api/query`、`/api/search`、`/api/processes`、`/api/clusters`。
- CORS：GitNexus 已对 `http://127.0.0.1:8765` 开放，前端可直接 fetch。

**实施内容（方案 A 最小闭环）：**
1. `api/client.ts` 新增 `gitnexusRepos`、`gitnexusRepoInfo`、`gitnexusGraph(repo)`、`gitnexusQuery(repo, query)`，基地址复用 `gitnexus_url`。
2. `CodeGraphViewer` 用 Cytoscape 渲染 `/api/graph` 返回的 nodes/relationships，节点颜色按 label/type 区分，支持缩放、拖拽、点击节点显示属性面板。
3. 工具栏提供搜索框，调用 `/api/search` 或 `/api/query` 定位节点；详情面板展示文件路径、符号类型、关联边。
4. `CodeGraphPage` 通过 feature flag（如 `config` 返回的 `code_graph_viewer: "gitnexus" | "self"`）切换 iframe 与自研查看器；默认先保持 iframe，确认方案后再切默认。
5. 若选择方案 B：`hub.ps1` 在启动 `gitnexus-serve` 前设置 `GITNEXUS_WEB_DIST=$projectRoot/work/gitnexus-web/dist`，并记录 fork 的构建、升级与许可说明。

**验收标准：**
- 方案 A：自研查看器能按仓库渲染图谱、搜索节点、展示节点详情，统计与列表行为与 iframe 版一致。
- 方案 B：`gitnexus serve` 实际加载自定义 `web/dist`，页面出现 Hub 定制标识，原有图谱功能不退化。
- 两种方案都不修改 `.gitnexus/` 之外的源文件。

**验证：**
- 方案 A：`pnpm --filter @research-asset-hub/web test -- --run`、`typecheck`、`lint`、`build`；手工打开 `/code-graph` 对比。
- 方案 B：`.\scripts\hub.ps1 start` 后访问 `http://127.0.0.1:4747/`，确认静态资源来自自定义目录；再跑 `.\scripts\hub.ps1 status`。

## 任务 5：e2e、文档与交付

**涉及文件或模块：**
- 修改：`apps/web/e2e/app.spec.ts`
- 修改：`docs/USER_MANUAL.md`
- 修改：`README.md`
- 修改：`AGENTS.md`

**实施内容：**
1. e2e：`/api/v1/code-graph` mock 增加 `hidden` 与 `hidden_count`；新增用例验证默认不显示 BMTools、显示开关、点击后 iframe 直达、Hub 地址栏仍为 `/code-graph`。
2. 用户手册补充：隐藏规则配置位置、显示已隐藏仓库、当前窗口打开语义、自研前端/自定义前端说明。
3. 用户手册补充“代码图谱研究方向窗口”：10 个方向三列窗口、窗口内滚动仓库列表、点击打开当前窗口查看器。
4. README 功能列表更新为“站内独立页面内嵌并直达仓库，支持隐藏名单与三列方向窗口”。
5. AGENTS.md 明确：隐藏名单只影响 Hub 页面展示，不修改 GitNexus registry 与 `.gitnexus/`。

**验收标准：**
- e2e 覆盖默认隐藏、显示隐藏、当前窗口直达、三列窗口与滚动容器；文档与最终行为一致。

**验证：**
- 运行：`pnpm --filter @research-asset-hub/web test:e2e`。
- 手工：`.\scripts\hub.ps1 start` 后打开 `/code-graph`。

## 验证命令汇总

- `.\.venv\Scripts\python.exe -m pytest -q tests/api/test_v1.py tests/api/test_code_browser.py tests/runtime/test_hub_script.py`
- `pnpm --filter @research-asset-hub/web test -- --run`
- `pnpm --filter @research-asset-hub/web typecheck`
- `pnpm --filter @research-asset-hub/web lint`
- `pnpm --filter @research-asset-hub/web build`
- `pnpm --filter @research-asset-hub/web test:e2e`
- `.\scripts\hub.ps1 start` / `status`

## 待确认事项

- “默认不要显示 BMTools”的作用范围：本计划默认只隐藏 Hub 代码图谱页；若要求 GitNexus 官方首页也不显示，必须完成任务 4 的自定义前端或自研前端。
- “当前窗口打开”默认按 Hub 当前标签页内嵌 iframe 理解；若你指的是点击后整个 Hub 页面跳转到 `http://127.0.0.1:4747`，需要把主按钮改为 `window.location.href` 并放弃站内内嵌，这会影响已完成的独立前端设计。
- 前端改造选方案 A（Hub 自研轻量前端）还是方案 B（fork 官方前端）：方案 A 交互完全可控但会缺失官方 AI 对话与高级代码浏览；方案 B 保功能但需要维护 fork 和升级。建议先完成任务 1–3，再按你的选择执行任务 4。

## 风险与回退

- GitNexus 升级到新版本时，方案 B 的 fork 需要重新合入；方案 A 不依赖其前端，风险更小。
- 隐藏规则按 title 子串匹配可能误伤同名仓库；默认配置只精确匹配 `registry_name: BMTools`，扩展名单需人工确认。
- 自研前端直接 fetch GitNexus API 时若未来版本改动响应结构，需要同步适配；任务 4 应先以 feature flag 灰度，保留 iframe 作为回退。
