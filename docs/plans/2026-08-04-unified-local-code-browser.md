# 统一本地代码展示与代码资产补齐计划

## 目标与成功标准

把资产中心的代码入口统一为站内的本地只读代码浏览器，并完成全部已登记代码表示的本地化巡检。

成功标准：

1. 代码入口不再默认跳转到 GitHub/Hugging Face 的原始网页；有本地仓库时在站内展示目录树、文件内容、行号和搜索入口，并保留 VS Code 打开能力。
2. 仅有 Markdown/README、没有已确认源码仓库的资产，不伪装成代码入口，代码按钮和入口按既有状态模型显示为灰色/不可用。
3. 所有可从已登记 `external_url` 推导并下载的代码仓库，下载到对应来源在 `config/sources.yaml` 声明的 `asset_roots.repository`；已存在的仓库不重复下载，无法下载的项目形成可审计报告。
4. 目录遍历、敏感文件、超大文件和二进制文件不会被代码浏览器直接暴露；接口只允许访问已登记代码表示对应的本地仓库。
5. 后端单测、前端单测、类型检查、构建和针对性 API/UI 验证通过；与本次改动无关的既有失败单独记录。

## 技术方案与边界

- 沿用 `SourceRegistry` 的来源根目录解析和 `resolve_editor_path` 的仓库解析，不复制源码到 Web 项目目录，也不修改来源快照或数据库表示元数据。
- 新增只读 API：按 representation 返回本地代码项目摘要、受限目录树和单文件文本。所有路径使用 `SourceRegistry.resolve` 校验，禁止绝对路径、`..`、隐藏/敏感文件；文本读取设置大小上限，二进制文件只返回不可预览状态。
- 前端新增 `CodeBrowser`，作为 `CodeOpenMenu` 的站内浏览器入口和详情页 `CodePanel` 的统一实现。VS Code 继续作为第二入口；当本地仓库不可用时不提供外部浏览器替代按钮。
- 代码仓库下载只处理数据库中 `repository`、`source_code`、`notebook` 三类表示，并以来源配置的 repository 根目录为目的地。没有源码 URL 或非 GitHub/Hugging Face 可推导仓库名的表示不强行猜测。
- 卡片视觉只做与截图直接相关的整理：移除重复/弱信息标签，保留一处清晰的论文类型/来源信息，并把代码状态和操作入口统一到同一视觉层级。

## 实施任务

### 任务 1：代码资产清单与本地化

**涉及文件或模块：**

- 读取：`packages/domain/models.py`、`apps/asset_gateway/registry.py`、`config/sources.yaml`
- 使用：`scripts/ensure_local_repositories.py`
- 输出：`work/validation/code-repository-audit-2026-08-04.json`

**实施内容：**

1. 运行下载脚本的 dry-run，统计所有代码表示的本地命中、待下载、无源码和失败原因。
2. 对可下载且目标根目录有效的项目执行浅克隆；重复仓库按现有目录复用。
3. 对下载后的目标执行 Git 仓库完整性检查（目录、`.git`、工作树状态、远端地址），将每条表示映射到实际本地目录。
4. 对无源码的表示明确标记为 docs-only/no-source，不把 README 当成核心代码。

**验收：**

- 每个代码表示都有明确结果；每个成功结果对应其 `asset_roots.repository` 下的本地目录。
- 三个 VLN 仓库以及其他已有/新增仓库均可被本地解析器发现。

### 任务 2：后端统一代码浏览 API

**涉及文件或模块：**

- 修改：`apps/api/routes_v1.py`
- 新增/复用：`apps/api/code_browser.py`（路径、文件类型、大小和敏感文件规则）
- 修改：`apps/web/src/api/types.ts`、`apps/web/src/api/client.ts`
- 测试：`tests/api/test_code_browser.py`、`apps/web/src/api/client.test.ts`

**实施内容：**

1. 新增代码项目信息、目录树、文件读取接口。
2. 通过 representation + source alias + registered root 解析项目，不接受前端传入绝对文件路径。
3. 目录排序采用目录优先、名称不区分大小写；隐藏文件、凭据、密钥和超过预览上限的文件过滤/拒绝。
4. 文件内容只允许 UTF-8/常见文本扩展名或可判定为文本的文件；返回语言、字节数、行数、截断标记和相对路径。
5. 本地解析失败返回可区分的 404/409 信息，供前端显示灰色状态，而不是回退到外网。

**验收：**

- 正常本地仓库可获取根目录、子目录和文本文件。
- `../`、隐藏文件、敏感文件、目录和超大文件均不能越权读取。
- 远程 URL-only representation 不会被代码浏览 API 当成本地项目。

### 任务 3：站内统一代码浏览器

**涉及文件或模块：**

- 新增：`apps/web/src/components/CodeBrowser.tsx`
- 修改：`apps/web/src/components/CodeOpenMenu.tsx`
- 修改：`apps/web/src/components/DocumentPanels.tsx`
- 修改：`apps/web/src/components/RepresentationActions.tsx`
- 修改：`apps/web/src/styles.css`
- 测试：`apps/web/src/components/CodeBrowser.test.tsx`、`apps/web/src/components/CodeOpenMenu.test.tsx`

**实施内容：**

1. 以左右分栏展示仓库目录树和代码内容；支持展开目录、选择文件、复制路径/内容、返回仓库根目录。
2. 使用纯文本 + 行号 + 语言标识，避免引入不必要的高亮依赖；Markdown 只在代码文件中作为文本查看，不把说明文档伪装成源码。
3. 从搜索卡片和详情页进入同一浏览器；默认打开本地仓库，VS Code 入口仅在本地路径可用时启用。
4. URL-only/未开源/下载失败代码均显示 disabled/灰色状态和具体原因，不再提供丑陋的外部 GitHub 浏览兜底。
5. 保持窄屏可用：目录树转为顶部可滚动选择区，内容区不溢出页面。

**验收：**

- 截图中的代码弹窗统一为站内浏览器样式；浏览器不会被带到外部代码网页。
- 详情页代码 Tab 与弹窗的文件展示行为一致。
- 无源码资产的代码操作按钮可见但灰色不可点，提示“未开源/未提供源码”。

### 任务 4：卡片信息层级整理

**涉及文件或模块：**

- 修改：`apps/web/src/components/ResearchUnitCard.tsx`、`apps/web/src/components/ResearchUnitActions.tsx`、`apps/web/src/styles.css`
- 测试：现有 ResearchUnitCard/Actions 测试

**实施内容：**

- 合并或隐藏截图中重复的“主论文/匹配来源”弱标签，只保留一个主信息标识。
- 将论文类型/领域标签移到标题上方且使用统一大小，标签、状态和操作按钮遵循同一间距系统。
- 代码状态文案与代码浏览器能力一致：本地可用、待本地化、未提供源码分别可判断。

### 任务 5：验证与交付

**验证命令：**

- `python scripts/ensure_local_repositories.py`（dry-run 审计）
- `python scripts/ensure_local_repositories.py --apply`（补齐本地仓库）
- `pytest -q tests/api/test_code_browser.py tests/api/test_editor_paths.py tests/api/test_v1.py`
- `pnpm --filter @research-asset-hub/web test -- --run`
- `pnpm --filter @research-asset-hub/web typecheck`
- `pnpm --filter @research-asset-hub/web lint`
- `pnpm --filter @research-asset-hub/web build`

**交付物：**

- 站内统一代码浏览器与灰色无源码状态。
- 所有代码仓库本地化审计报告：`work/validation/code-repository-audit-2026-08-04.json`。
- 本实施计划及最终验证结果。

## 风险与处理

- 个别 URL 不是公开 Git 仓库、仓库已删除或网络失败：保留失败原因并显示灰色，不猜测替代仓库。
- 仓库体积过大：默认浅克隆；不下载模型权重、数据集或构建产物，必要时仅在报告中列出限制。
- 既有工作区改动很多：所有新增修改限定在本计划涉及文件，既有删除和修改不回滚。

## 执行结果（2026-08-04）

- 全量审计 421 个代码 representation；首次补齐阶段下载 36 个公开 Git 仓库，已有本地仓库全部复用。
- `SWE-agent/SWE-agent` 和 `mlfoundations/dclm` 因 Windows 路径长度限制无法完整 checkout，已保留 Git 仓库并用 sparse-checkout materialize README、配置、训练、评测和测试等核心目录。
- 最终 dry-run：411 个本地可用、9 个非核心代码源跳过（Hugging Face 模型/数据集或非确认 Git 源）、1 个失效 GitHub 地址待灰显、0 个无来源遗漏。失效地址为 `tiiuae/falcon-llm`，远端返回 Repository not found，未猜测替代仓库。
- 后端定向测试：24 passed；前端类型检查、Lint、28 个单元测试和生产构建均通过。
- Playwright：15 passed；剩余 2 项为既有 `/domains/agents` 测试夹具期待“智能体系统”但当前页面未提供该标题，与本次代码浏览器改动无关。
