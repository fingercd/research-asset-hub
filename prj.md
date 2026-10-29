# prj.md — 本知识库资料入库的强制规则（Agent 必读）

范围：任何向 research-asset-hub 增加论文/代码/PDF/HTML/导读 资料的工作。
与 `AGENTS.md` 冲突时以更严格者为准；详细命令见 `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md`。

## 铁律

1. **论文标题和 PDF 必填，其余材料选填**（2026-10-04 用户确认）：简介、作者、年份、会议、HTML、代码、笔记均可后补。未提供的可选材料不是入库失败；已登记材料缺失或损坏必须显式报告。
   前端研究单元卡片三按钮（PDF/HTML/代码）必须"有资料即可点"——按钮状态 = 库内数据，
   不是 difussion 工作目录。代码克隆到了 `outputs/code/` 但没在 hub 建成仓库资产 + 关联，
   按钮照样灰。
2. **入库五步，每步验证**（顺序执行，禁止跳步）：
   a. **素材落地**（difussion 根）：
      - PDF → `outputs/pdfs/<conf>/<paper_id>.pdf`（`%PDF` 魔数 + sha256）
      - 解读 HTML → `outputs/papers_html/<conf>/<paper_id>/index.html`（过 `work/scripts/check_html.py`）
      - 代码 → `outputs/code/<conf>/<paper_id>/`（`git clone`，记录 GitHub URL）
   b. **登记**（hub）：旁车 `config/intake/<source-id>/manifest.json` + `config/sources.yaml` 块
      + `config/identity-merge-approvals.yaml` 审批；manifest 里 `representations.code.url`
      **必须填真实 URL**——url 为空就不会创建仓库资产，代码按钮必灰（本次 14 篇就是此病根）。
   c. **导入**：`./scripts/hub.ps1 ingest -All`（先 `-DryRun` 看合并命中），**第二遍幂等重跑全 0** 才能继续。
   d. **关系**：`./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source <alias> --confirmation-status confirmed --apply`
      （先不加 `--apply` 预览 `bindings_found`）。论文↔仓库不关联 = 代码按钮灰。
   e. **验收**：`./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain <domain>`
      **必须退出码 0**。任何 `✗` 不得以"本地有代码"为由通过。
3. **代码按钮灰 = 三种原因，按序排查**：
   ① 仓库资产不存在 → 查 manifest `representations.code.url` 是否为空；
   ② `implemented_by` 关系缺失 → 跑 reconcile（预览看 bindings）；
   ③ 文件不存在 → 查 `outputs/code/<conf>/<id>/` 目录与网关根注册。
4. **导读（guide）已废除**（2026-08-11）：新数据不得创建 `role: guide` 表示，不得在 manifest 配 guide；
   前端按钮已删除。旧的 guide 数据保留在库中（只读），仅无入口。
5. **代码状态独立记录**：未添加、仅在线、已公开并保存、已宣布未公开、第三方复现分别显示；人工入库无需按论文题名增加无代码白名单。历史已审计的限制仍保留，不自动解除。
6. **收尾**：等待抽取任务收敛 → 更新 survey（如有）→ 修复报告放 `work/validation/`，
   必须包含：新增/合并/幂等/完整性/验收脚本输出/遗留问题。

## 本次修复的教训（2026-08-11，避免重演）

- 21 篇有代码论文曾全部显示灰色"代码"按钮：7 篇缺 `implemented_by` 关系，14 篇缺 URL 未建仓库资产。
- FollowYourShape 卡片"没有 PDF/HTML 按钮"：那是孤立的仓库单元卡片（repositoryOnly 只显示代码按钮），
  关联建立后仓库并入论文单元，按钮齐全。
- 验收脚本在修复前运行过一次（红线基线），修复后必须全绿——先红后绿是标准流程。
