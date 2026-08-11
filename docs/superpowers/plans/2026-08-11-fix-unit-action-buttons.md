# 修复研究单元四按钮（PDF/HTML/代码/导读）显示与可用性 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让知识库前端每个论文研究单元卡片的 PDF/HTML/代码 三个按钮与库内真实资料一致（有资料即可用、确实无则灰），删除"导读"按钮，并把"入库后四按钮验收"固化为 `prj.md` 规则，杜绝"代码本地有但按钮灰"复发。

**Architecture:** 三层修复：
1. **数据层**：用官方 `scripts/reconcile_repository_relations.py` 建立 7 条已存在仓库资产的 `implemented_by` 关联（含 FollowYourShape）；用旁车更正来源（frozen manifest 不可改，AGENTS.md 铁律）补 14+2 个仓库的 URL 与仓库资产，再协调出全部 21+2 条关联。
2. **应用层**：后端 `_research_unit_quick_actions` 的 kinds 元组去掉 `guide`，前端 `ResearchUnitActions.tsx` 删掉导读按钮。
3. **规则层**：hub 根新建 `prj.md`，把"验收脚本 check_unit_actions.py 必须全绿"固化为未来入库的硬门禁。

**Tech Stack:** Python（FastAPI/SQLAlchemy/psycopg，hub 用 `.venv`）、React + Vite + Vitest（apps/web）、PowerShell `scripts/hub.ps1`、官方协调脚本 `scripts/reconcile_repository_relations.py`。

## Global Constraints

- **冻结 manifest 只读（AGENTS.md）**：不得修改 `image-editing-2026` / `image-editing-anchors-2026` / `image-editing-2026-pdf-additions` 的 manifest 与 sources.yaml 已登记块；新数据一律走旁车 `config/intake/<source-id>/manifest.json`。
- **不直接改库**：不得直改 PostgreSQL/Meilisearch/Qdrant；一切变更走正式 `hub.ps1 ingest` / API / 官方脚本。
- **Python 解释器**：hub 一律 `./.venv/Scripts/python.exe`（项目根下）；difussion 一律 `C:/Users/ExampleUser/anaconda3/envs/pytorch/python.exe`（完整绝对路径，禁止裸 python）。
- **路径一律正斜杠**（Git Bash 转义纪律）。
- **关系创建唯一正道**：`scripts/reconcile_repository_relations.py`（幂等、审计、evidence），不得手写 INSERT。
- **sources.yaml 变更后必须重启服务**：`./scripts/hub.ps1 stop` + `./scripts/hub.ps1 start`，重启后验证网关根注册与域统计。
- **验收口径（唯一标准）**：每个 `unit_kind=="paper"` 单元的 `pdf`/`html`/`code` 三动作 `status == "available"`（code 允许 `not_provided` 仅当论文在无代码白名单内）；`guide` 动作不得再出现。
- **导读删除边界**：仅删除研究单元卡片按钮 + 后端 kinds；`AssetDetailPage` 的导读面板、`has_guide` 搜索过滤保留（旧域数据仍可访问，不在本次范围）。
- **服务端口**：API 8767 / 网关 8766 / Web 8765；域名 `image-editing` 现有 33 论文 + 7 仓库资产。
- **每次正式导入后必须幂等重跑**（第二次全部新增计数为 0），报告放 `work/validation/`。
- 已知无公开代码的论文（10 篇）白名单按**英文精确标题**维护在 `config/check-unit-actions-allowlist.yaml`。

---

## File Structure

| 文件 | 动作 | 职责 |
|------|------|------|
| `scripts/check_unit_actions.py` | 新建 | 四按钮验收脚本（硬门禁，可重复运行） |
| `config/check-unit-actions-allowlist.yaml` | 新建 | 无代码论文白名单（按 domain 分组的英文标题） |
| `apps/api/routes_v1.py` | 修改 | `_research_unit_quick_actions` kinds 去掉 guide |
| `apps/web/src/components/ResearchUnitActions.tsx` | 修改 | 删导读按钮定义与图标导入 |
| `apps/web/src/lib/format.ts` | 修改 | 删 `guide: "导读"` 标签 |
| `apps/web/src/api/types.ts` | 修改 | `QuickActionKind` 去掉 `"guide"` |
| `tests/api/test_v1.py` | 修改 | guide 动作断言更新（~1050, 1072-1073） |
| `apps/web/src/components/ResearchUnitCard.test.tsx` | 修改 | guide fixture 清理（86/208/324 行附近） |
| `config/intake/image-editing-2026-code-repos/manifest.json` | 新建（生成器产出） | 14 个仓库 URL 补录旁车源 |
| `config/intake/image-editing-anchors-2026-code-urls/manifest.json` | 新建（生成器产出） | 2 个锚点仓库 URL 补录旁车源 |
| `config/sources.yaml` | 修改 | 插入两个旁车 source 块（plugin_discovery 前） |
| `config/identity-merge-approvals.yaml` | 修改 | 追加 16 条 title-year 弱合并审批 |
| `C:/Users/ExampleUser/Desktop/difussion/work/scripts/build_code_repos_source.py` | 新建 | 旁车源生成器（幂等可重跑） |
| `prj.md`（hub 根） | 新建 | 未来入库强制规则（本计划最终交付物之一） |
| `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md` 所引用 `AGENTS.md` | 修改 | 第 9 步补验收脚本引用 |

**关键数据（Task 4 的 14 项 URL 映射，来自 difussion `work/fullmd/*/code_manifest.json`）：**

| 主源 source_record_id | 代码相对路径 | GitHub URL |
|------|------|------|
| `eccv-arxiv_2605.01480` | `eccv/arxiv_2605.01480` | `https://github.com/leeguandong/AttnRouter` |
| `eccv-arxiv_2605.08250` | `eccv/arxiv_2605.08250` | `https://github.com/ZephinueCode/VAE-LFA` |
| `icml-icml_shan_t_edit_triple_branch_diffusion` | `icml/icml_shan_t_edit_triple_branch_diffusion` | `https://github.com/LinsongShan/T-Edit` |
| `icml-icml_yang_directedit_step_level_accurate_inversion` | `icml/icml_yang_directedit_step_level_accurate_inversion` | `https://github.com/Tr1stesse/DirectEdit` |
| `icml-icml_zaccagnino_shifting_the_breaking_point` | `icml/icml_zaccagnino_shifting_the_breaking_point` | `https://github.com/Blowing-Up-Groundhogs/IDAttn` |
| `icml-icml_tong_entropy_aware_dynamic_kv_cache` | `icml/icml_tong_entropy_aware_dynamic_kv_cache` | `https://github.com/lilitong7/SparseAR-ICML-2026` |
| `icml-arxiv_2505.04320` | `icml/arxiv_2505.04320` | `https://github.com/ZhouZJ-DL/Multi-turn_Consistent_Image_Editing` |
| `acl-arxiv_2602.18022` | `acl/arxiv_2602.18022` | `https://github.com/leeguandong/DCAG` |
| `acl-arxiv_2603.29602` | `acl/arxiv_2603.29602` | `https://github.com/hackermmzz/IMAGAgent` |
| `cvpr-arxiv_2601.19180` | `cvpr/arxiv_2601.19180` | `https://github.com/Tankowa/SNR-Edit` |
| `cvpr-arxiv_2603.00141` | `cvpr/arxiv_2603.00141` | `https://github.com/AMAP-ML/ADE-CoT` |
| `cvpr-cvpr_chen_the_devil_is_in` | `cvpr/cvpr_chen_the_devil_is_in` | `https://github.com/zhuochen02/SynPS` |
| `iclr-arxiv_2504.13109` | `iclr/arxiv_2504.13109` | `https://github.com/DSL-Lab/UniEdit-Flow` |
| `iclr-arxiv_2508.09131` | `iclr/arxiv_2508.09131` | `https://github.com/zxYin/ColorCtrl_Code` |

**Task 5 的 2 项锚点 URL 映射：**

| 主源 source_record_id | 代码相对路径 | GitHub URL |
|------|------|------|
| `spotedit-anchor` | `cvpr/arxiv_2512.22323` | `https://github.com/Biangbiang0321/SpotEdit` |
| `freqedit-anchor` | `icml/arxiv_2512.01755` | `https://github.com/FreqEdit/FreqEdit` |

**无代码白名单（10 篇，英文精确标题，来自主 manifest）：**

```
SteerFlow: Steering Rectified Flows for Faithful Inversion-Based Image Editing
h-Flow: Flexible Flow-based Image Editing via Doob's h-Transform
SpecEdit: Training-Free Acceleration for Diffusion based Image Editing via Semantic Locking
MT-EditFlow: Reinforcement Learning for Multi-Turn Image Editing with Flow Matching
PC-Edit: Prompt-Contrastive Region Discovery and Region-Guided Editing
Unpaired Visual Editing with Self-Consistent Flow Matching
Token Pruning for In-Context Generation in Diffusion Transformers
I2E: From Image Pixels to Actionable Interactive Environments for Text-Guided Image Editing
Edit Where You Mean: Region-Aware Adapter Injection for Mask-Free Local Image Editing
W-EDIT: A Wavelet-Based Frequency-Aware Framework for Text-Driven Image Editing
```

---

### Task 1: 验收脚本 check_unit_actions.py（TDD 红线基线）

**Files:**
- Create: `scripts/check_unit_actions.py`（hub 根）
- Create: `config/check-unit-actions-allowlist.yaml`

**Interfaces:**
- Consumes: 运行中的 API `http://127.0.0.1:8767/api/v1/search`（`group_by=paper` 分页游标 `next_cursor`）
- Produces: `check_unit_actions.py --domain image-editing` 退出码 0/1；后续 Task 2-6 均以它为本轮回归依据

- [ ] **Step 1: 创建无代码白名单**

Create `config/check-unit-actions-allowlist.yaml`:

```yaml
# 验收脚本白名单：这些论文经调研确认无公开代码，允许 code=not_provided。
# 新增论文入库时，确无公开代码才可在此登记（按英文精确标题）。
image-editing:
  no_code_titles:
    - "SteerFlow: Steering Rectified Flows for Faithful Inversion-Based Image Editing"
    - "h-Flow: Flexible Flow-based Image Editing via Doob's h-Transform"
    - "SpecEdit: Training-Free Acceleration for Diffusion based Image Editing via Semantic Locking"
    - "MT-EditFlow: Reinforcement Learning for Multi-Turn Image Editing with Flow Matching"
    - "PC-Edit: Prompt-Contrastive Region Discovery and Region-Guided Editing"
    - "Unpaired Visual Editing with Self-Consistent Flow Matching"
    - "Token Pruning for In-Context Generation in Diffusion Transformers"
    - "I2E: From Image Pixels to Actionable Interactive Environments for Text-Guided Image Editing"
    - "Edit Where You Mean: Region-Aware Adapter Injection for Mask-Free Local Image Editing"
    - "W-EDIT: A Wavelet-Based Frequency-Aware Framework for Text-Driven Image Editing"
```

- [ ] **Step 2: 写验收脚本（先红后绿）**

Create `scripts/check_unit_actions.py`（hub 根，完整代码）：

```python
# -*- coding: utf-8 -*-
"""四按钮验收脚本：断言每个论文研究单元的 PDF/HTML/代码 按钮状态与库内资料一致。

用法:
  ./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing

口径:
  - 每个 unit_kind=="paper" 单元必须满足:
      pdf  status == "available"
      html status == "available"
      code status == "available"（或 == "not_provided" 且论文标题在白名单内）
  - guide 动作不得再出现（2026-08-11 已废除导读按钮）。
退出码 0 = 全部符合;1 = 存在不匹配。可重复运行，是未来入库的硬门禁。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

DEFAULT_ALLOWLIST = Path(__file__).resolve().parents[1] / "config" / "check-unit-actions-allowlist.yaml"


def fetch_units(api_base: str, domain: str) -> list[dict]:
    units: list[dict] = []
    cursor: str | None = None
    while True:
        params = {"q": "", "mode": "keyword", "group_by": "paper"}
        if domain:
            params["domain"] = domain
        if cursor:
            params["cursor"] = cursor
        url = f"{api_base}/api/v1/search?{urllib.parse.urlencode(params)}"
        with urllib.request.urlopen(url, timeout=120) as response:
            data = json.load(response)
        units.extend(data.get("items", []))
        cursor = data.get("next_cursor")
        if not cursor:
            return units


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8767")
    parser.add_argument("--domain", default="image-editing")
    parser.add_argument("--allowlist", default=str(DEFAULT_ALLOWLIST))
    args = parser.parse_args()

    allowlist = yaml.safe_load(Path(args.allowlist).read_text(encoding="utf-8")) or {}
    no_code_titles = set((allowlist.get(args.domain) or {}).get("no_code_titles", []))

    units = fetch_units(args.api, args.domain)
    papers = [unit for unit in units if unit.get("unit_kind") == "paper"]
    failures: list[str] = []
    guide_found = False
    print(f"论文单元 {len(papers)} 个（domain={args.domain}）")

    for unit in sorted(papers, key=lambda item: (item.get("primary_asset") or {}).get("title", "")):
        primary = unit.get("primary_asset") or {}
        title = primary.get("title") or "?"
        actions = {action["kind"]: action for action in unit.get("quick_actions", [])}
        row = [f"[{primary.get('id', '?')[:8]}]"]
        for kind in ("pdf", "html"):
            status = (actions.get(kind) or {}).get("status", "missing")
            ok = status == "available"
            row.append(f"{kind}={status}{'✓' if ok else '✗'}")
            if not ok:
                failures.append(f"{title}: {kind} 期望 available，实际 {status}")
        code_status = (actions.get("code") or {}).get("status", "not_provided")
        code_ok = code_status == "available" or (
            code_status == "not_provided" and title in no_code_titles
        )
        row.append(f"code={code_status}{'✓' if code_ok else '✗'}")
        if not code_ok:
            failures.append(f"{title}: code 期望 available（或属于无代码白名单），实际 {code_status}")
        if "guide" in actions:
            guide_found = True
            failures.append(f"{title}: 仍存在 guide 动作（已废除，应删除）")
        print("  " + " | ".join(row) + " | " + title[:60])

    if failures:
        print(f"\n❌ {len(failures)} 处不匹配：")
        for failure in failures:
            print("  - " + failure)
        if guide_found:
            print("  - （guide 动作残留提示：后端 kinds 元组未移除 guide）")
        return 1
    print("\n✅ 全部论文单元按钮状态符合预期")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: 运行脚本，确认当前失败（TDD 红线基线）**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing
```
Expected: 退出码 1。输出应包含：
- 每个论文单元 `code=not_provided ✗`（21 篇有代码论文全部灰）或仅 7 篇通过（若有先导数据）；
- 每个单元 `guide=not_provided ✗`（guide 动作残留，Task 2 修复）；
- 汇总 `❌ N 处不匹配`。

把完整输出保存为 `work/validation/check-unit-actions-baseline.txt`（证明基线存在）。

- [ ] **Step 4: 提交**

```bash
git add scripts/check_unit_actions.py config/check-unit-actions-allowlist.yaml
git commit -m "test: 新增四按钮验收脚本 check_unit_actions（红线基线，当前预期失败）"
```

---

### Task 2: 删除导读按钮（后端 + 前端 + 测试）

**Files:**
- Modify: `apps/api/routes_v1.py`（`_research_unit_quick_actions` kinds 元组，约 648 行；`missing_reasons` 字典约 551 行）
- Modify: `tests/api/test_v1.py`（约 1050、1072-1073 行）
- Modify: `apps/web/src/components/ResearchUnitActions.tsx`（actionDefinitions 与 lucide 导入）
- Modify: `apps/web/src/lib/format.ts`（`guide: "导读"`）
- Modify: `apps/web/src/api/types.ts`（`QuickActionKind`）
- Modify: `apps/web/src/components/ResearchUnitCard.test.tsx`（guide fixture）

**Interfaces:**
- Consumes: 无（独立变更）
- Produces: 后端 quick_actions 只剩 `pdf/html/code` 三类；前端卡片只剩三个按钮；Task 6 的验收脚本不再报 guide 残留

- [ ] **Step 1: 改后端 kinds 元组**

In `apps/api/routes_v1.py`，`_research_unit_quick_actions` 内（当前 `for kind in ("pdf", "html", "guide"):`）：

```python
-    for kind in ("pdf", "html", "guide"):
+    for kind in ("pdf", "html"):
```

In 同文件 `_quick_action` 的 `missing_reasons` 字典（当前含 `"guide": "来源未提供导读。",` 一行），删除该行：

```python
         missing_reasons = {
             "pdf": "来源未提供 PDF。",
             "html": "来源未提供 HTML。",
-            "guide": "来源未提供导读。",
             "code": (
```

- [ ] **Step 2: 更新后端测试**

In `tests/api/test_v1.py`：
- 约 1050 行：`assert list(actions) == ["pdf", "html", "guide", "code"]` → `assert list(actions) == ["pdf", "html", "code"]`
- 约 1072-1073 行：删除两行 guide 断言
  ```python
  -    assert actions["guide"]["status"] == "not_provided"
  -    assert actions["guide"]["representation_id"] is None
  ```
- 搜索该文件其余 `actions["guide"]` / `"guide"` 相关 quick-action 断言（`has_guide` 过滤测试保留不动——过滤参数仍存在），仅删除对 quick_actions 中 guide 动作的断言。

- [ ] **Step 3: 改前端按钮定义**

In `apps/web/src/components/ResearchUnitActions.tsx`：

```diff
 import { useMutation } from "@tanstack/react-query";
 import {
-  BookOpenText,
   ChevronDown,
   Code2,
   ExternalLink,
   FileText,
 } from "lucide-react";
```

```diff
 const actionDefinitions: Array<{
   kind: QuickActionKind;
   label: string;
   icon: typeof FileText;
 }> = [
   { kind: "pdf", label: "PDF", icon: FileText },
   { kind: "html", label: "HTML", icon: ExternalLink },
-  { kind: "guide", label: "导读", icon: BookOpenText },
   { kind: "code", label: "代码", icon: Code2 },
 ];
```

- [ ] **Step 4: 清理类型与标签**

In `apps/web/src/api/types.ts`：
```diff
-export type QuickActionKind = "pdf" | "html" | "guide" | "code";
+export type QuickActionKind = "pdf" | "html" | "code";
```

In `apps/web/src/lib/format.ts` 约 61 行：
```diff
-    guide: "导读",
```

- [ ] **Step 5: 清理前端测试 fixtures**

In `apps/web/src/components/ResearchUnitCard.test.tsx`，删除 3 处 `kind: "guide"` 的 fixture（约 86、208、324 行），并把断言按钮数量的用例从 4 调整为 3（如有）。同步检查 `apps/web/src/components/ResearchUnitActions.test.tsx`（若存在）同样清理。

- [ ] **Step 6: 跑后端测试**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe -m pytest tests/api/test_v1.py -q -x
```
Expected: 全部 PASS（环境变量按 playbook 第 12 节配置；若仅缺 `DATABASE_URL` 等，按 `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md` 的测试节补齐后重跑）。

- [ ] **Step 7: 跑前端测试 + 构建**

Run（hub 根）:
```bash
cd apps/web && pnpm test
```
Expected: 全部 PASS（vitest run）。再跑 `pnpm build`，Expected: `tsc -b && vite build` 无类型错误。

- [ ] **Step 8: 重启服务并回归验收**

Run:
```bash
./scripts/hub.ps1 stop
./scripts/hub.ps1 start
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing
```
Expected: 退出码 1（guide 残留项消失，不再出现 `guide=not_provided ✗`；代码项仍红——那是 Task 3-5 的事）。确认 `curl http://127.0.0.1:8767/api/v1/health` 正常。

- [ ] **Step 9: 提交**

```bash
git add apps/api/routes_v1.py tests/api/test_v1.py apps/web/src
git commit -m "feat: 移除研究单元卡片的导读按钮（后端 kinds + 前端 actionDefinitions + 测试）"
```

---

### Task 3: 关联 7 个已有仓库资产（reconcile）

**Files:**
- Run: `scripts/reconcile_repository_relations.py`（hub 根，只读预览 + 正式 apply）
- Record: `work/validation/reconcile-7.json`（输出留存）

**Interfaces:**
- Consumes: 库内已存在的 7 个 repository 资产及其 REPOSITORY 角色 SourceRecordAsset 链接（已确认存在）
- Produces: 7 条 `paper --implemented_by--> repository` confirmed 关系（含 FollowYourShape）；对应论文单元的 code 动作变 available

- [ ] **Step 1: 只读预览**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-2026 > work/validation/reconcile-7-preview.json
```
Expected: `"bindings_found": 7`、`"relations_created": 7`、所有 action outcome 为 `would_create`、`"missing_or_invalid_endpoints": 0`。若 `missing_or_invalid_endpoints > 0`，先按报告逐条排查再继续。

- [ ] **Step 2: 正式 apply（confirmed）**

Run:
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-2026 --confirmation-status confirmed --apply > work/validation/reconcile-7-applied.json
```
Expected: `"status": "applied"`、`"relations_created": 7`。

- [ ] **Step 3: 幂等复核（重跑 apply 必须全 0）**

Run:
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-2026 --confirmation-status confirmed --apply
```
Expected: `"relations_created": 0`、`"relations_updated": 0`、`"relations_reused": 7`。

- [ ] **Step 4: API 抽查 FollowYourShape 单元**

Run:
```bash
curl -s "http://127.0.0.1:8767/api/v1/search?q=Follow-Your-Shape&mode=keyword&group_by=paper" > work/validation/fys-unit.json
```
用 `./.venv/Scripts/python.exe` 检查 `items[0].quick_actions`：
```python
import json
d = json.load(open("work/validation/fys-unit.json", encoding="utf-8"))
u = d["items"][0]
assert u["unit_kind"] == "paper"
actions = {a["kind"]: a["status"] for a in u["quick_actions"]}
print(actions)  # 期望 {"pdf": "available", "html": "available", "code": "available"}
assert actions["code"] == "available", actions
assert len(u.get("related_repositories", [])) == 1, u.get("related_repositories")
```
Expected: 打印 `{'pdf': 'available', 'html': 'available', 'code': 'available'}`，无 AssertionError。

- [ ] **Step 5: 验收脚本回归**

Run:
```bash
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing
```
Expected: 退出码 1，但 7 篇（VeloEdit、CacheEdit、HierEdit、FlowDC、RegionE、FlowAlign、FollowYourShape）code 项转绿。

- [ ] **Step 6: 提交（无代码变更，提交验证产物）**

```bash
git add work/validation/reconcile-7-preview.json work/validation/reconcile-7-applied.json work/validation/fys-unit.json
git commit -m "chore: image-editing 7 条 paper→repository implemented_by 关系（reconcile confirmed，幂等复核通过）"
```

---

### Task 4: 旁车更正来源补齐 14 个仓库 URL

**Files:**
- Create: `C:/Users/ExampleUser/Desktop/difussion/work/scripts/build_code_repos_source.py`（生成器）
- Create（生成器产出）: `config/intake/image-editing-2026-code-repos/manifest.json`
- Modify（生成器产出）: `config/sources.yaml`（插入 source 块）
- Modify（生成器产出）: `config/identity-merge-approvals.yaml`（追加 14 条审批）
- Record: `work/validation/code-repos-*.json`

**Interfaces:**
- Consumes: `config/intake/image-editing-2026/manifest.json`（标题/年份/作者/venue）、difussion `work/fullmd/{acl,cvpr,eccv,iclr,icml}/code_manifest.json`（URL）
- Produces: 14 个新 repository 资产 + 14 条 REPOSITORY 角色 SourceRecordAsset 链接；reconcile 后 21 条关联；对应论文 code 动作 available

- [ ] **Step 1: 写生成器（幂等可重跑）**

Create `C:/Users/ExampleUser/Desktop/difussion/work/scripts/build_code_repos_source.py`（完整代码，模式同 `work/scripts/setup_pdf_additions_source.py`）：

```python
# -*- coding: utf-8 -*-
"""为 14 个「本地有代码但 manifest 缺 URL」的论文建立旁车更正来源。

产出（幂等可重跑，重复运行不产生重复）：
  1) hub config/intake/image-editing-2026-code-repos/manifest.json（14 项）
  2) hub config/sources.yaml 插入 image-editing-2026-code-repos 块
  3) hub config/identity-merge-approvals.yaml 追加 14 条 title-year 弱合并审批
"""
import json
import os
import re
import unicodedata
from pathlib import Path

import yaml

HUB = Path("D:/ResearchLibrary/research-asset-hub")
DIFFUSION = Path("C:/Users/ExampleUser/Desktop/difussion")
MAIN_MANIFEST = HUB / "config/intake/image-editing-2026/manifest.json"
SOURCES = HUB / "config/sources.yaml"
INTAKE_DIR = HUB / "config/intake/image-editing-2026-code-repos"
APPROVALS = HUB / "config/identity-merge-approvals.yaml"

# 14 项：主源 source_record_id -> (代码相对路径, GitHub URL)
URL_MAP = {
    "eccv-arxiv_2605.01480": ("eccv/arxiv_2605.01480", "https://github.com/leeguandong/AttnRouter"),
    "eccv-arxiv_2605.08250": ("eccv/arxiv_2605.08250", "https://github.com/ZephinueCode/VAE-LFA"),
    "icml-icml_shan_t_edit_triple_branch_diffusion": ("icml/icml_shan_t_edit_triple_branch_diffusion", "https://github.com/LinsongShan/T-Edit"),
    "icml-icml_yang_directedit_step_level_accurate_inversion": ("icml/icml_yang_directedit_step_level_accurate_inversion", "https://github.com/Tr1stesse/DirectEdit"),
    "icml-icml_zaccagnino_shifting_the_breaking_point": ("icml/icml_zaccagnino_shifting_the_breaking_point", "https://github.com/Blowing-Up-Groundhogs/IDAttn"),
    "icml-icml_tong_entropy_aware_dynamic_kv_cache": ("icml/icml_tong_entropy_aware_dynamic_kv_cache", "https://github.com/lilitong7/SparseAR-ICML-2026"),
    "icml-arxiv_2505.04320": ("icml/arxiv_2505.04320", "https://github.com/ZhouZJ-DL/Multi-turn_Consistent_Image_Editing"),
    "acl-arxiv_2602.18022": ("acl/arxiv_2602.18022", "https://github.com/leeguandong/DCAG"),
    "acl-arxiv_2603.29602": ("acl/arxiv_2603.29602", "https://github.com/hackermmzz/IMAGAgent"),
    "cvpr-arxiv_2601.19180": ("cvpr/arxiv_2601.19180", "https://github.com/Tankowa/SNR-Edit"),
    "cvpr-arxiv_2603.00141": ("cvpr/arxiv_2603.00141", "https://github.com/AMAP-ML/ADE-CoT"),
    "cvpr-cvpr_chen_the_devil_is_in": ("cvpr/cvpr_chen_the_devil_is_in", "https://github.com/zhuochen02/SynPS"),
    "iclr-arxiv_2504.13109": ("iclr/arxiv_2504.13109", "https://github.com/DSL-Lab/UniEdit-Flow"),
    "iclr-arxiv_2508.09131": ("iclr/arxiv_2508.09131", "https://github.com/zxYin/ColorCtrl_Code"),
}

_PUNCTUATION = re.compile(r"[^\w\s]")


def normalize_title(value: str) -> str:
    """与 hub packages/ingestion/reconciliation.py 的 normalize_title 完全一致。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = _PUNCTUATION.sub(" ", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def identity_key(title: str, year) -> str:
    return f"title-year:{normalize_title(title)}:{year}"


def main() -> None:
    main_items = json.load(open(MAIN_MANIFEST, encoding="utf-8"))["items"]
    by_id = {item["id"]: item for item in main_items}
    missing = [rid for rid in URL_MAP if rid not in by_id]
    assert not missing, f"主 manifest 缺少条目: {missing}"

    items = []
    approvals = []
    for record_id, (path, url) in URL_MAP.items():
        src = by_id[record_id]
        items.append({
            "id": record_id,
            "title": src["title"],
            "title_zh": src.get("title_zh"),
            "authors": src.get("authors", []),
            "year": src.get("year"),
            "venue": src.get("venue"),
            "arxiv_id": src.get("arxiv_id"),
            "representations": {
                "code": {"path": path, "url": url},
            },
        })
        approvals.append({
            "identity_key": identity_key(src["title"], src.get("year")),
            "records": [
                {"source_id": "image-editing-2026", "source_record_id": record_id},
                {"source_id": "image-editing-2026-code-repos", "source_record_id": record_id},
            ],
            "rationale": f"代码仓库 URL 补录，与主记录同论文合并到同一资产（{url}）。",
        })

    # 1) 写 intake manifest（幂等：内容相同即跳过）
    INTAKE_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = INTAKE_DIR / "manifest.json"
    payload = {"schema": "intake/generic-manifest/v1", "items": items}
    if manifest_path.exists() and json.load(open(manifest_path, encoding="utf-8")) == payload:
        print("manifest 已存在且一致，跳过")
    else:
        json.dump(payload, open(manifest_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print("manifest 写入:", manifest_path, len(items), "items")

    # 2) sources.yaml 插入（幂等）
    text = SOURCES.read_text(encoding="utf-8")
    block = """  - id: image-editing-2026-code-repos
    label: 图像编辑 · 2026 五大会调研 代码仓库 URL 补录
    domain: image-editing
    adapter: generic_manifest
    root: config/intake/image-editing-2026-code-repos
    manifest: manifest.json
    expected_records: 14
    read_only: true
    asset_roots:
      repository: C:/Users/ExampleUser/Desktop/difussion/outputs/code
    options:
      record_selector: items
      field_mapping:
        source_record_id: id
        title: title
        title_zh: title_zh
        authors: authors
        year: year
        venue: venue
        arxiv: arxiv_id
      representations:
        - type: repository
          role: official
          root_key: repository
          path_field: representations.code.path
          url_field: representations.code.url
          network_required: true

"""
    if "image-editing-2026-code-repos" not in text:
        anchor = "plugin_discovery:"
        assert anchor in text, "找不到 plugin_discovery 锚点"
        text = text.replace(anchor, block + anchor, 1)
        SOURCES.write_text(text, encoding="utf-8")
        print("sources.yaml: 已插入 image-editing-2026-code-repos")
    else:
        print("sources.yaml: 已存在，跳过")

    # 3) merge approvals 追加（幂等）
    approvals_doc = yaml.safe_load(APPROVALS.read_text(encoding="utf-8"))
    groups = approvals_doc["approved_weak_merge_groups"]
    existing = {g["identity_key"] for g in groups}
    added = 0
    for group in approvals:
        if group["identity_key"] not in existing:
            groups.append(group)
            added += 1
    if added:
        approvals_doc["approved_weak_merge_groups"] = groups
        yaml.safe_dump(approvals_doc, open(APPROVALS, "w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
        print("approvals: 追加", added, "条")
    else:
        print("approvals: 已存在，跳过")

    # 4) 校验 YAML/JSON 可解析
    yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    yaml.safe_load(APPROVALS.read_text(encoding="utf-8"))
    json.load(open(manifest_path, encoding="utf-8"))
    print("DONE：14 项、sources.yaml、approvals 全部就绪")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 本地校验 14 个代码目录存在（缺目录先补克隆）**

Run（difussion 根）:
```bash
C:/Users/ExampleUser/anaconda3/envs/pytorch/python.exe work/scripts/build_code_repos_source.py
```
Expected: 输出 `manifest 写入 ... 14 items`、`sources.yaml: 已插入 ...`、`approvals: 追加 14 条`、`DONE`。第二次重跑全部输出"跳过"（幂等）。

Run（difussion 根）:
```bash
for d in eccv/arxiv_2605.01480 eccv/arxiv_2605.08250 icml/icml_shan_t_edit_triple_branch_diffusion icml/icml_yang_directedit_step_level_accurate_inversion icml/icml_zaccagnino_shifting_the_breaking_point icml/icml_tong_entropy_aware_dynamic_kv_cache icml/arxiv_2505.04320 acl/arxiv_2602.18022 acl/arxiv_2603.29602 cvpr/arxiv_2601.19180 cvpr/arxiv_2603.00141 cvpr/cvpr_chen_the_devil_is_in iclr/arxiv_2504.13109 iclr/arxiv_2508.09131; do test -d "outputs/code/$d" && echo "OK $d" || echo "MISSING $d"; done
```
Expected: 14 行全部 `OK`（Git Bash 语法）。任何 `MISSING` → 先按 `work/fullmd/<conf>/code_manifest.json` 的 URL `git clone` 到 `outputs/code/<conf>/<id>/` 再继续。

- [ ] **Step 3: hub 侧 dry-run 导入（AGENTS.md 硬门禁）**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe -m packages.cli.main validate
./scripts/hub.ps1 preflight
./scripts/hub.ps1 inventory
./scripts/hub.ps1 ingest -All -DryRun > work/validation/code-repos-dryrun.json
```
Expected（`work/validation/code-repos-dryrun.json`）:
- `source_records_created: 14`（弱合并后不新增论文资产）
- `repository_assets_created: 14`
- `paper_assets_created: 0`（14 条 title-year 审批全部命中弱合并；若不为 0，检查 approvals 的 identity_key 是否与 reconcile 的 normalize_title 一致）
- `representations_created: 14`

- [ ] **Step 4: 备份 + 正式导入**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe scripts/create_backup.py --note "before image-editing-2026-code-repos" > work/validation/code-repos-backup.json
./scripts/hub.ps1 ingest -All > work/validation/code-repos-ingest.json
```
Expected: 新增计数与 dry-run 一致；`paper_assets_created == 0`。

- [ ] **Step 5: 幂等重跑（全 0 才可继续）**

Run:
```bash
./scripts/hub.ps1 ingest -All > work/validation/code-repos-ingest-2nd.json
```
Expected: `source_records_created`、`repository_assets_created`、`representations_created`、`relations_created` 全部为 0（按 playbook 第 7 节清单逐项核对）。若不为 0，解释输入变化或修复幂等问题后再继续。

- [ ] **Step 6: 排队抽取 + 重启服务（sources.yaml 变更）**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe scripts/seed_jobs.py
./scripts/hub.ps1 stop
./scripts/hub.ps1 start
```
然后在网站"后台任务"页等待 `pending/running` 清零（playbook 第 8 节）。重启后验证：
```bash
curl -s http://127.0.0.1:8767/api/v1/domains/image-editing | grep -o '"repository_count":[0-9]*'
```
Expected: `"repository_count":21`（7 + 14）。

- [ ] **Step 7: 协调 21 条关联**

Run（hub 根）:
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-2026 --confirmation-status confirmed > work/validation/reconcile-21-preview.json
```
Expected: `"bindings_found": 21`、`"relations_created": 14`（7 条复用）、`"missing_or_invalid_endpoints": 0`。确认后：
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-2026 --confirmation-status confirmed --apply > work/validation/reconcile-21-applied.json
```
Expected: `"relations_created": 14`、`"status": "applied"`。

- [ ] **Step 8: 验收脚本回归**

Run:
```bash
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing
```
Expected: 退出码 1（21 篇 code 转绿；剩余 2 篇锚点在 Task 5）。

- [ ] **Step 9: 提交（difussion + hub）**

```bash
# difussion 根
git add work/scripts/build_code_repos_source.py
git commit -m "feat: 旁车更正来源生成器（14 个仓库 URL 补录）"
# hub 根
git add config/intake/image-editing-2026-code-repos config/sources.yaml config/identity-merge-approvals.yaml work/validation/code-repos-*.json work/validation/reconcile-21-*.json
git commit -m "feat: image-editing-2026-code-repos 旁车来源（14 仓库 URL）导入并协调 21 条 implemented_by"
```

---

### Task 5: 锚点 2 仓库补 URL（SpotEdit/FreqEdit）

**Files:**
- Create: `config/intake/image-editing-anchors-2026-code-urls/manifest.json`
- Modify: `config/sources.yaml`（插入 source 块）
- Modify: `config/identity-merge-approvals.yaml`（追加 2 条审批）
- Record: `work/validation/anchors-code-*.json`

**Interfaces:**
- Consumes: `config/intake/image-editing-anchors-2026/manifest.json`（spotedit-anchor/freqedit-anchor 的标题/年份）
- Produces: 2 个新 repository 资产；reconcile 后 2 条关联；锚点论文 code 动作 available

- [ ] **Step 1: 手工写旁车源（2 项，先读锚点主 manifest 取标题/年份/作者）**

Run（hub 根，打印锚点主 manifest 内容）:
```bash
C:/Users/ExampleUser/anaconda3/envs/pytorch/python.exe -c "
import json
m = json.load(open('config/intake/image-editing-anchors-2026/manifest.json', encoding='utf-8'))
for it in m['items']:
    print(json.dumps(it, ensure_ascii=False, indent=1))
"
```
按输出创建 `config/intake/image-editing-anchors-2026-code-urls/manifest.json`（标题/年份/作者/venue 从主 manifest 逐字段复制）：

```json
{
  "schema": "intake/generic-manifest/v1",
  "items": [
    {
      "id": "spotedit-anchor",
      "title": "<从主 manifest 复制 spotedit-anchor 的 title>",
      "title_zh": "<从主 manifest 复制>",
      "authors": "<从主 manifest 复制>",
      "year": "<从主 manifest 复制>",
      "venue": "<从主 manifest 复制>",
      "representations": {
        "code": {
          "path": "cvpr/arxiv_2512.22323",
          "url": "https://github.com/Biangbiang0321/SpotEdit"
        }
      }
    },
    {
      "id": "freqedit-anchor",
      "title": "<从主 manifest 复制 freqedit-anchor 的 title>",
      "title_zh": "<从主 manifest 复制>",
      "authors": "<从主 manifest 复制>",
      "year": "<从主 manifest 复制>",
      "venue": "<从主 manifest 复制>",
      "representations": {
        "code": {
          "path": "icml/arxiv_2512.01755",
          "url": "https://github.com/FreqEdit/FreqEdit"
        }
      }
    }
  ]
}
```

- [ ] **Step 2: 插入 sources.yaml 块 + 追加 2 条 merge approvals**

In `config/sources.yaml`，`plugin_discovery:` 之前插入（幂等检查：已含 `image-editing-anchors-2026-code-urls` 则跳过）：

```yaml
  - id: image-editing-anchors-2026-code-urls
    label: 图像编辑 · 锚点论文 代码仓库 URL 补录
    domain: image-editing
    adapter: generic_manifest
    root: config/intake/image-editing-anchors-2026-code-urls
    manifest: manifest.json
    expected_records: 2
    read_only: true
    asset_roots:
      repository: C:/Users/ExampleUser/Desktop/difussion/outputs/code
    options:
      record_selector: items
      field_mapping:
        source_record_id: id
        title: title
        title_zh: title_zh
        authors: authors
        year: year
        venue: venue
      representations:
        - type: repository
          role: official
          root_key: repository
          path_field: representations.code.path
          url_field: representations.code.url
          network_required: true

```

In `config/identity-merge-approvals.yaml` 的 `approved_weak_merge_groups` 追加 2 条（`<title>`/`<year>` 用 Step 1 读到的真实值，identity_key 用 Task 4 生成器同款 normalize）：

```yaml
    - identity_key: "title-year:<spotedit 规范化标题>:<year>"
      records:
        - source_id: image-editing-anchors-2026
          source_record_id: spotedit-anchor
        - source_id: image-editing-anchors-2026-code-urls
          source_record_id: spotedit-anchor
      rationale: "SpotEdit 代码仓库 URL 补录，与主记录同论文合并到同一资产。"
    - identity_key: "title-year:<freqedit 规范化标题>:<year>"
      records:
        - source_id: image-editing-anchors-2026
          source_record_id: freqedit-anchor
        - source_id: image-editing-anchors-2026-code-urls
          source_record_id: freqedit-anchor
      rationale: "FreqEdit 代码仓库 URL 补录，与主记录同论文合并到同一资产。"
```

校验可解析：
```bash
C:/Users/ExampleUser/anaconda3/envs/pytorch/python.exe -c "import yaml; yaml.safe_load(open('config/sources.yaml', encoding='utf-8')); yaml.safe_load(open('config/identity-merge-approvals.yaml', encoding='utf-8')); print('YAML OK')"
```

- [ ] **Step 3: 确认两个锚点代码目录存在**

Run（difussion 根）:
```bash
test -d "outputs/code/cvpr/arxiv_2512.22323" && echo "OK SpotEdit" || echo "MISSING SpotEdit"
test -d "outputs/code/icml/arxiv_2512.01755" && echo "OK FreqEdit" || echo "MISSING FreqEdit"
```
Expected: 两行 OK。

- [ ] **Step 4: dry-run + 备份 + 正式导入 + 幂等**

Run（hub 根）:
```bash
./scripts/hub.ps1 ingest -All -DryRun > work/validation/anchors-code-dryrun.json
./.venv/Scripts/python.exe scripts/create_backup.py --note "before image-editing-anchors-2026-code-urls" > work/validation/anchors-code-backup.json
./scripts/hub.ps1 ingest -All > work/validation/anchors-code-ingest.json
./scripts/hub.ps1 ingest -All > work/validation/anchors-code-ingest-2nd.json
```
Expected: dry-run 与正式导入 `paper_assets_created: 0`、`repository_assets_created: 2`、`representations_created: 2`；第二遍全部 0。

- [ ] **Step 5: 重启 + 协调 2 条关联**

Run:
```bash
./scripts/hub.ps1 stop
./scripts/hub.ps1 start
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-anchors-2026 --confirmation-status confirmed > work/validation/reconcile-anchors-preview.json
```
Expected: `"bindings_found": 2`、`"relations_created": 2`、`"missing_or_invalid_endpoints": 0`。确认后：
```bash
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py --source image-editing-anchors-2026 --confirmation-status confirmed --apply > work/validation/reconcile-anchors-applied.json
```
Expected: `"relations_created": 2`、`"status": "applied"`。

- [ ] **Step 6: 验收脚本回归**

Run:
```bash
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing
```
Expected: 退出码 1，但仅剩 PDF/HTML 项红（若有），code 项 23/23 全绿。

- [ ] **Step 7: 提交**

```bash
git add config/intake/image-editing-anchors-2026-code-urls config/sources.yaml config/identity-merge-approvals.yaml work/validation/anchors-code-*.json work/validation/reconcile-anchors-*.json
git commit -m "feat: 锚点 SpotEdit/FreqEdit 代码仓库 URL 补录并协调 implemented_by"
```

---

### Task 6: 全量验收 + 打开链路 + 报告

**Files:**
- Run: `scripts/check_unit_actions.py`、API 抽查、`scripts/verify_source_integrity.py`
- Create: `work/validation/2026-08-11-unit-action-buttons.md`（迁移/修复报告）

**Interfaces:**
- Consumes: Task 1-5 全部产出
- Produces: 全绿验收 + 报告；Task 7 的 prj.md 引用此报告口径

- [ ] **Step 1: 验收脚本全绿**

Run:
```bash
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain image-editing > work/validation/check-unit-actions-final.txt
```
Expected: 退出码 0，输出 `✅ 全部论文单元按钮状态符合预期`（33 个论文单元：31 + 2 锚点，code 23 可用 + 10 白名单）。若 PDF/HTML 有红项：对缺失 PDF 的论文按既有下载流水线补下（`work/scripts/pull_pdf_chunks.py` / 已存在 PDF 则核对路径），对缺失 HTML 的重新生成解读页（`work/scripts/check_html.py` 门禁），然后重跑本脚本直至全绿。

- [ ] **Step 2: 打开链路实测（open-representation）**

对 FollowYourShape 单元依次点击三按钮等价调用，验证网关真实可打开：
```bash
# 1) 拿三个 representation id（上一步 fys-unit.json 中）
# 2) POST open-actions（前端 api.openRepresentation 等价）
curl -s -X POST http://127.0.0.1:8767/api/v1/representations/019fec66-ca5e-769a-92a9-254f4d5ad0ff/open-actions -H "Content-Type: application/json" -d '{}'
```
Expected: 返回 `open_url` 指向网关 PDF 路径（`/api/v1/.../file` 或 gateway URL）。随后用 `curl -s -o /dev/null -w "%{http_code}" "<open_url>"` 验证：
- PDF → 200 且 Content-Type 为 `application/pdf`
- HTML（`019fec66-cac0-7879-b57d-43492a0cc29c`）→ 200 且 `text/html`
- 代码（对 mayuelala/FollowYourShape 仓库资产 019fec66-ca66-76fd-a8d9-b230fce9e155 的 code 表示 019fec66-cab…）→ 200（目录/文件接口）
若 403（signed gateway session）：按 hub 文档用网关签名会话方式打开（403 是正常签名机制，非错误——用浏览器会话点击或带签名的请求验证）。

- [ ] **Step 3: 来源完整性校验**

Run:
```bash
./.venv/Scripts/python.exe scripts/verify_source_integrity.py --report work/validation/integrity-after.json
```
Expected: 与本次相关的 3 个 image-editing 来源 0 个 PDF/代码 mismatch。既有 96 处 repository tree_digest mismatch（GitNexus 索引副作用）与 9 处 fine_tuning missing 为历史遗留，报告内注明与本次无关。

- [ ] **Step 4: 写修复报告**

Create `work/validation/2026-08-11-unit-action-buttons.md`，内容：
- 背景与症状（FollowYourShape 卡片无 PDF/HTML 按钮 = 仓库孤儿单元；代码按钮灰 = implemented_by 缺失 / manifest URL 缺失）
- 修复清单：删导读按钮（前后端）、7 条关联、14+2 仓库 URL 旁车源、21+2 条 implemented_by
- 验收证据：check_unit_actions 输出、幂等重跑计数、integrity 摘要、打开链路结果
- 遗留：96 处 repository tree_digest + fine_tuning 9 missing（与本次无关，建议单独审计）

- [ ] **Step 5: 提交**

```bash
git add work/validation/2026-08-11-unit-action-buttons.md work/validation/check-unit-actions-final.txt work/validation/integrity-after.json
git commit -m "docs: 2026-08-11 研究单元按钮修复报告（四按钮验收全绿）"
```

---

### Task 7: prj.md 未来入库规则

**Files:**
- Create: `prj.md`（hub 根）
- Modify: `AGENTS.md`（第 9 步补验收脚本引用）

**Interfaces:**
- Consumes: Task 6 的验收口径与报告
- Produces: 未来所有入库 Agent 必读的强制规则文件

- [ ] **Step 1: 写 prj.md**

Create `prj.md`（hub 根，完整内容）：

```markdown
# prj.md — 本知识库资料入库的强制规则（Agent 必读）

范围：任何向 research-asset-hub 增加论文/代码/PDF/HTML/导读 资料的工作。
与 `AGENTS.md` 冲突时以更严格者为准；详细命令见 `docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md`。

## 铁律

1. **四件套缺一不可**：每篇论文必须同时具备 PDF、解读 HTML、代码（可获取时）。
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
5. **无代码白名单**：确无公开代码的论文，把**英文精确标题**登记到
   `config/check-unit-actions-allowlist.yaml` 对应 domain 下，禁止静默缺失。
6. **收尾**：等待抽取任务收敛 → 更新 survey（如有）→ 修复报告放 `work/validation/`，
   必须包含：新增/合并/幂等/完整性/验收脚本输出/遗留问题。

## 本次修复的教训（2026-08-11，避免重演）

- 21 篇有代码论文曾全部显示灰色"代码"按钮：7 篇缺 `implemented_by` 关系，14 篇缺 URL 未建仓库资产。
- FollowYourShape 卡片"没有 PDF/HTML 按钮"：那是孤立的仓库单元卡片（repositoryOnly 只显示代码按钮），
  关联建立后仓库并入论文单元，按钮齐全。
- 验收脚本在修复前运行过一次（红线基线），修复后必须全绿——先红后绿是标准流程。
```

- [ ] **Step 2: AGENTS.md 第 9 步补验收脚本引用**

In `AGENTS.md`，把第 9 步：

```markdown
9. 在网站中验证：可以搜索到、详情元数据正确、PDF/代码/导读/HTML 可以安全打开、关系和阅读状态正常。
```

改为：

```markdown
9. 在网站中验证：可以搜索到、详情元数据正确、PDF/代码/导读/HTML 可以安全打开、关系和阅读状态正常；
   研究单元卡片四按钮（PDF/HTML/代码）必须跑 `./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain <domain>` 全绿（红线基线 + 修复后全绿），
   论文↔代码关联必须用 `scripts/reconcile_repository_relations.py --confirmation-status confirmed --apply` 建立。
   新入库规则详见 `prj.md`。
```

- [ ] **Step 3: 自审 prj.md 可执行性**

逐条核对：Agent 只读 `prj.md` + `AGENTS.md` + playbook 能否完成一次完整入库而不会重犯"代码灰"？若某步命令不精确（缺 flag/路径），当场补全。

- [ ] **Step 4: 提交**

```bash
git add prj.md AGENTS.md
git commit -m "docs: 新增 prj.md 未来入库强制规则（四按钮验收 + reconcile 必跑）"
```

---

## 收尾（所有任务完成后）

1. 运行全量验收脚本最终确认退出码 0；
2. 用 `git log --oneline` 核对两仓库提交序列完整；
3. 汇总交付报告（症状 → 根因 → 修复 → 验收证据 → 遗留）。

## Self-Review（写计划时已自查）

- **规格覆盖**：①"修改当前 pdf 情况" → Task 3（FollowYourShape 关联）+ Task 6（打开链路）；②"四按钮 + 导读删除 + 代码灰色" → Task 2（删导读）+ Task 3/4/5（代码按钮恢复）+ Task 1（验收口径）；③"prj.md 未来规则" → Task 7。无缺口。
- **占位符扫描**：无 TBD；所有新脚本给出完整代码；唯一 `<...>` 占位是 Task 5 需从锚点主 manifest 复制的字段值（执行时读取，非推断）。
- **类型一致性**：`check_unit_actions.py --domain` 与 `reconcile --source` 的 alias 均为 `image-editing-2026` 系；`normalize_title` 与 hub `reconciliation.py` 实现逐字符一致；旁车源命名 `image-editing-2026-code-repos` / `image-editing-anchors-2026-code-urls` 全计划一致。
