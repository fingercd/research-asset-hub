# 三个 VLN 模型及其论文接入 Research Asset Hub 实施计划

> 日期：2026-08-03
> 项目：Research Asset Hub
> 范围：新增 AwareVLN、StreamVLN、InternVLA-N1 三个 VLN 知识单元（论文 + 官方代码仓库），复用已有 `vision-language-navigation` 领域
> 状态：待执行。本文件只描述方案；正式写库前必须逐项执行并保留报告。

## 1. 目标与成功标准

一句话目标：在不修改任何已登记来源、不移动原文件的前提下，把三个 VLN 模型以“论文资产 + 官方仓库资产”的旁车来源正式登记，并完成抽取、索引、来源完整性和网站打开验证。

成功标准：

1. 新增 3 个独立 `generic_manifest` 旁车 source，各含 1 条稳定 `source_record_id`，共 3 条新来源记录。
2. 每个知识单元都有：正式题名、作者、年份、arXiv/URL 强标识、论文表示和官方仓库表示；不存在的本地文件不得伪造。
3. 全量 dry-run `status=passed`、无重复来源键、`expected_count_matches` 全部为 true、无未审批弱合并。
4. 备份后第一次 `ingest -All` 创建 3 条记录；第二次 `ingest -All` 在输入不变时所有新增计数均为 0。
5. 抽取任务收敛；正式索引使用 `BAAI/bge-m3`、1024 维、`degraded=false`，新资产可被搜索到。
6. 来源完整性检查通过；开放目录中来源二进制复制数为 0。
7. 完成报告列出新增/表示/关系/索引/备份/幂等结论和用户仍需手工完成的步骤。

## 2. 已核实的材料与关键决策

### 2.1 三个模型证据

| 模型 | 主论文 | 官方代码 | 本地材料 |
|---|---|---|---|
| AwareVLN | arXiv `2605.22816`，CVPR 2026，项目页 `https://gwxuan.github.io/AwareVLN/` | `https://github.com/gwxuan/AwareVLN` | 无本地 PDF；checkpoint 在 `https://huggingface.co/gwx22/AwareVLN-ck`，不作为正式表示 |
| StreamVLN | arXiv `2507.05240`，ICRA 2026 | `https://github.com/OpenRobotLab/StreamVLN`（当前地址 `InternRobotics/StreamVLN`） | `C:\Users\ExampleUser\Desktop\VideoLLM压缩裁剪\05_StreamVLN_ICRA2026.pdf` |
| InternVLA-N1 | 技术报告 `InternVLA_N1.pdf`（`https://internrobotics.github.io/internvla-n1.github.io/static/pdfs/InternVLA_N1.pdf`）；关联 DualVLN arXiv `2512.08186` | `https://github.com/InternRobotics/InternNav` | 无本地 PDF；服务器存在评测用 `InternNav`/`InternVLA-candidate-20260801` 工作副本，不作为资产根 |

约束说明：

- 平台资产类型没有 `model`；模型统一表示为 `paper` 资产 + `repository` 资产，checkpoint/权重只出现在 `source_urls` 或属性里，不登记为表示（`docs/architecture/source-boundaries.md` 排除模型权重）。
- 服务器 `/users/fotile/VLN/src/*` 是远程工作副本，Hub 的 `asset_roots` 只解析本机路径，因此代码一律登记官方 GitHub URL；如需本地代码快照，先放到本机稳定目录再登记。
- 现有 VLN atlas 已包含 `ground-slow-move-fast-dual-system-foundation-2025`（DualVLN / Ground Slow Move Fast，arXiv `2512.08186`），与 InternVLA-N1 是同一模型家族，但 InternVLA-N1 技术报告本身尚未登记。不得改写已冻结的 `vln-atlas-20260726` 旁车。

### 2.2 关键决策

1. 使用三个独立旁车 source，一知识单元一旁车；不复用或追加已冻结的 VLN atlas。
2. 普通旁车一律走 `generic_manifest`，不调用 `build_canonical_snapshot.py`。
3. 默认表示策略：
   - StreamVLN：引用桌面现有 PDF（不复制、不移动）；
   - AwareVLN：URL-only PDF；
   - InternVLA-N1：URL-only 技术报告 PDF；
   - 三者代码均为 URL-only 官方仓库。
   URL 表示符合手册“只有 URL、没有本地文件，可以登记 URL 表示”，会在打开链路中明确联网依赖。
4. InternVLA-N1 技术报告与已登记 Ground Slow Move Fast 若被识别为题名年份弱候选，必须核对后显式处置；确认为不同文档时保留两条记录，正式导入后通过 API 建立 `related_to` 关系。
5. 默认不更新 VLN 领域门户内容；仅当用户确认三模型改变了发展脉络/阅读路线时，才在 `config/domain-portals/v2/vision-language-navigation/` 更新并提高 `content_version`。
6. 中文导读 `note.md` 为推荐表示，便于 RAG 检索；若用户不需要可删除该表示，不影响其他任务。

## 3. 目标源与文件清单

| source_id | source_record_id | 主题名 | 表示 |
|---|---|---|---|
| `intake-awarevln-2026` | `awarevln-2026` | AwareVLN: Reasoning with Self-awareness for Vision-Language Navigation | PDF URL + 代码 URL + 可选 note |
| `intake-streamvln-2026` | `streamvln-2026` | StreamVLN: Streaming Vision-and-Language Navigation via SlowFast Context Modeling | 本地 PDF + 代码 URL + 可选 note |
| `intake-internvla-n1-2026` | `internvla-n1-2025` | InternVLA-N1: An Open Dual-System Navigation Foundation Model with Learned Latent Plans | PDF URL + 代码 URL + 可选 note |

计划新建/修改文件：

- 创建：`config/intake/intake-awarevln-2026/manifest.json`
- 创建：`config/intake/intake-streamvln-2026/manifest.json`
- 创建：`config/intake/intake-internvla-n1-2026/manifest.json`
- 修改：`config/sources.yaml`（仅追加 3 条 source，不整份覆盖）
- 可选创建：`D:/ResearchLibrary/个人新增资料/2026-08-03-vln-models/<model>/note.md`（项目外永久目录）
- 报告：`work/validation/` 下 dry-run、ingest、index、integrity、final report

## 4. 任务清单

### 任务 1：冻结前材料与身份核对

**涉及文件或模块：**
- 只读检查：`config/intake/vln-atlas-20260726/manifest.json`、VLN 领域已登记资产 API
- 外部证据：arXiv 页面、项目页、GitHub 仓库、本地 StreamVLN PDF

**接口与依赖：**
- 产出：每个知识单元的最终 `source_record_id`、正式题名、作者、年份、venue、arXiv、source URLs、PDF path/URL、代码 URL、SHA-256（本地 PDF 必须算）。
- 依赖：服务已启动（当前已 ready）。

**实施内容：**
1. 用 arXiv API/页面核对 AwareVLN `2605.22816`、StreamVLN `2507.05240`、DualVLN `2512.08186` 的题名、作者和年份；技术报告从 InternNav README 和 PDF 首页核对。
2. 计算 `05_StreamVLN_ICRA2026.pdf` 的 SHA-256，确认文件可读。
3. 通过 API 搜索确认 Ground Slow Move Fast 是否已登记，并记下其 asset UUID（供任务 6 使用）。
4. 确认三个 GitHub URL 返回 200（已在计划准备阶段验证过）。
5. 若用户要求本地 PDF，先把 AwareVLN/InternVLA-N1 PDF 下载到 `D:/ResearchLibrary/个人新增资料/2026-08-03-vln-models/<model>/`；禁止放进 `research-asset-hub`。

**验收标准：**
- 每个知识单元有唯一稳定 ID、完整元数据和至少一个可验证论文表示。
- 不出现 Downloads/临时目录、绝对盘符进入 manifest 路径、空路径或伪造路径。
- 与已登记资产的重叠关系已明确（尤其 InternVLA-N1 ↔ Ground Slow Move Fast）。

**验证：**
- 运行：`git status --short` 确认工作树基线；`./scripts/hub.ps1 status` 确认服务 ready。
- 预期：无冲突改动；三个 URL 可访问；StreamVLN PDF 存在且 SHA-256 已记录。

### 任务 2：创建旁车 manifest 与 source 配置

**涉及文件或模块：**
- 创建：3 份 `config/intake/<source-id>/manifest.json`
- 修改：`config/sources.yaml`（追加 3 条）

**实施内容：**

每份 manifest 使用 `schema_version: 1` 和 `items[]`，示例结构（AwareVLN）：

```json
{
  "schema_version": 1,
  "items": [
    {
      "id": "awarevln-2026",
      "title": "AwareVLN: Reasoning with Self-awareness for Vision-Language Navigation",
      "title_zh": "AwareVLN：基于自我感知推理的视觉语言导航",
      "authors": ["Wenxuan Guo", "Xiuwei Xu", "Yichen Liu"],
      "year": 2026,
      "venue": "CVPR 2026 / arXiv preprint",
      "abstract": "以 arXiv 原文摘要为准，不得填 Agent 推测",
      "summary": "在关键导航节点做稀疏自我感知推理，统一 VLM 在 [REASON]/[ACT] 之间切换，并用自动数据引擎生成监督",
      "tags": ["vln", "self-awareness", "reasoning", "cvpr-2026"],
      "source_urls": [
        "https://arxiv.org/abs/2605.22816",
        "https://gwxuan.github.io/AwareVLN/",
        "https://huggingface.co/gwx22/AwareVLN-ck"
      ],
      "doi": null,
      "arxiv_id": "2605.22816",
      "representations": {
        "pdf": {"path": null, "url": "https://arxiv.org/pdf/2605.22816", "sha256": null},
        "code": {"path": null, "url": "https://github.com/gwxuan/AwareVLN"},
        "note": {"path": "note.md"}
      },
      "relations": {"related_record_ids": []}
    }
  ]
}
```

StreamVLN 的 `representations.pdf` 使用 `{"path": "05_StreamVLN_ICRA2026.pdf", "url": null, "sha256": "<任务1记录值>"}`；InternVLA-N1 的 `source_urls` 同时包含技术报告 URL、InternNav 仓库和 DualVLN arXiv 链接，`arxiv_id` 保持 `null`（技术报告本身无 arXiv 标识）。

在 `config/sources.yaml` 的 `sources` 列表末尾追加 3 条，每条如下（以 AwareVLN 为例）：

```yaml
- id: intake-awarevln-2026
  label: 个人新增 · AwareVLN
  domain: vision-language-navigation
  adapter: generic_manifest
  root: config/intake/intake-awarevln-2026
  manifest: manifest.json
  expected_records: 1
  read_only: true
  asset_roots:
    pdf: D:/ResearchLibrary/个人新增资料/2026-08-03-vln-models/awarevln
    repository: D:/ResearchLibrary/个人新增资料/2026-08-03-vln-models/awarevln
    note: D:/ResearchLibrary/个人新增资料/2026-08-03-vln-models/awarevln
  options:
    record_selector: items
    field_mapping:
      source_record_id: id
      title: title
      title_zh: title_zh
      authors: authors
      year: year
      venue: venue
      abstract: abstract
      summary: summary
      tags: tags
      source_urls: source_urls
      doi: doi
      arxiv: arxiv_id
    representations:
      - type: pdf
        role: original
        root_key: pdf
        path_field: representations.pdf.path
        url_field: representations.pdf.url
        sha256_field: representations.pdf.sha256
        mime_type: application/pdf
      - type: repository
        role: official
        root_key: repository
        path_field: representations.code.path
        url_field: representations.code.url
      - type: markdown
        role: guide
        root_key: note
        path_field: representations.note.path
        mime_type: text/markdown
    relations:
      - type: related_to
        target_kind: source_record_id
        target_field: relations.related_record_ids
```

StreamVLN 的 `asset_roots.pdf` 指向 `C:/Users/ExampleUser/Desktop/VideoLLM压缩裁剪`；其余 root 指向各自稳定目录。若任务 1 决定移动或复制文件，root 必须同步指向最终位置，并在冻结前确定。

**验收标准：**
- 3 份 manifest JSON 合法；`source_id`、`source_record_id` 符合 slug 规则；路径全部相对且不含 `.`/`..`/盘符。
- `expected_records` 均为 1；`domain` 均为 `vision-language-navigation`。
- `relations.related_record_ids` 为空（不表达跨来源关系）。
- `sources.yaml` 无重复 source id，其余条目未改动。

**验证：**
- 运行：`./.venv/Scripts/python.exe -c "import json,pathlib; [json.load(open(p,encoding='utf-8')) for p in pathlib.Path('config/intake').glob('intake-*-2026/manifest.json')]"`（或人工 JSON 校验）。
- 预期：JSON 无解析错误。

### 任务 3：只读检查与全量 dry-run

**实施内容：**
按手册顺序执行，不改任何来源文件：

```powershell
./scripts/hub.ps1 start
./scripts/hub.ps1 preflight
./scripts/hub.ps1 validate -All -Json
./scripts/hub.ps1 inventory -All
./scripts/hub.ps1 ingest -All -DryRun
```

**验收标准：**
打开 `work/validation/ingest-dry-run.json` 逐项核对：

- 顶层 `status=passed`，`errors=[]`；
- `duplicate_source_record_keys=[]`；
- 3 个新 source 的 `expected_count_matches`/`record_count_matches=true`；
- `unapproved_review_groups=[]`；若出现 InternVLA-N1 与 Ground Slow Move Fast 的候选，先在任务 1 核对并在 `config/identity-merge-approvals.yaml` 显式处置（只针对确认为同一作品的集合）；
- `asset_path_missing` 只允许等于已接受清单（URL 表示本就不落盘；StreamVLN PDF 必须存在）；
- 3 条记录的领域、表示类型/角色、路径/URL 与任务 2 一致。

**验证：**
- 运行上述命令并人工检查报告 JSON。
- 预期：无错误、无未解释 warning；每个新来源记录数=1。

### 任务 4：备份与两次正式导入

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/create_backup.py `
  --report work/validation/backup-before-ingest.json
./scripts/hub.ps1 ingest -All
./scripts/hub.ps1 ingest -All
```

**验收标准：**
- 备份报告生成且记录 `snapshot_id`（不删除备份）。
- 第一次 `ingest-formal.json`：schema v2、`validated_dry_run.status=passed`、新增 3 条来源记录。
- 第二次 `ingest-formal.json`：`source_records_created`、`source_record_versions_created`、`paper_assets_created`、`repository_assets_created`、`representations_created`、`relations_created`、`relation_evidence_created`、`field_claims_created` 全部为 0。
- 两次之间不得修改 manifest、sources.yaml 或来源文件。

**验证：**
- 运行：`Get-Content work\validation\ingest-formal.json -Raw | ConvertFrom-Json` 检查计数。
- 预期：第一次 3 条新增，第二次 0 新增。

### 任务 5：抽取、索引与表示可用性

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/seed_jobs.py
```

打开 `http://127.0.0.1:8765/jobs` 等待 `pending/running` 归零；失败任务必须解释。然后：

```powershell
./.venv/Scripts/python.exe scripts/build_indexes.py `
  --version 20260803-three-vln-models `
  --report work/validation/index-build-bge-m3.json
```

**验收标准：**
- 抽取任务收敛，无未解释失败；StreamVLN 本地 PDF 可抽取，URL 表示按平台规则标记网络依赖。
- 索引报告：embedding model `BAAI/bge-m3`、向量维数 1024、`degraded=false`；3 个新论文资产进入 assets 索引，可检索题名/arXiv/作者/代码符号；无上一版本残留失效文档。
- 不把 `--hashing-only` 冒充正式索引。

**验证：**
- 运行：`./scripts/hub.ps1 status` 确认 worker 存活；检查索引报告 JSON。
- 预期：全部健康、失败列表为空。

### 任务 6：导入后跨来源关系（仅核实语义后执行）

**实施内容：**
InternVLA-N1 技术报告与已登记 `ground-slow-move-fast-dual-system-foundation-2025` 属于同一模型家族；导入后先确认两个真实 asset UUID，再用正式 API 建立 `related_to` 关系并添加 evidence：

```powershell
$payload = @{
  subject_asset_id = "<internvla-n1 asset uuid>"
  object_asset_id = "<ground-slow-move-fast asset uuid>"
  relation_type = "related_to"
  source_key = "manual"
  confirmation_status = "confirmed"
  confidence = 0.9
  attributes = @{ note = "InternVLA-N1 技术报告与 DualVLN/Ground Slow Move Fast 是同一模型家族的正式论文。" }
} | ConvertTo-Json -Depth 4
```

关系建立后调用 `/evidence` 写审计证据。AwareVLN、StreamVLN 之间不强行建关系；只有语义已核对才使用 `confirmed`。

**验收标准：**
- 关系可通过 API/图谱查询到；evidence 可见；审计事件写入。
- 没有未核对的候选关系被确认。

**验证：**
- 运行：`Invoke-RestMethod http://127.0.0.1:8767/api/v1/relations/<id>`。
- 预期：返回两个正确资产 UUID 和关系语义。

### 任务 7：来源完整性与网站打开验证

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/verify_source_integrity.py
```

网站逐项检查：

1. 用完整题名、arXiv ID、作者和代码仓库名在搜索页检索三个新单元。
2. 打开资产详情，核对题名、作者、年份、领域、标签、source URLs 和表示。
3. 分别打开 StreamVLN 本地 PDF、AwareVLN/InternVLA-N1 的 PDF URL 和三个代码仓库 URL；缺失按钮必须禁用，不得伪造。
4. 确认代码作为独立 repository 资产，存在 `paper --implemented_by--> repository` 关系。
5. 打开可选 `note.md`，确认正文可搜索。

**验收标准：**
- 三个模型都能搜到；详情元数据正确；表示打开链路安全可用。
- 来源完整性报告通过；URL 表示被正确跳过本地文件检查。

**验证：**
- 运行完整性脚本；人工打开 3 个详情页和表示。
- 预期：`verify_source_integrity.py` 通过；网站无 404/权限错误。

### 任务 8：开放目录、回归测试与完成报告

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/export_catalog.py
./.venv/Scripts/ruff.exe check apps packages scripts tests
./.venv/Scripts/python.exe -m pytest -q
pnpm --filter @research-asset-hub/web lint
pnpm --filter @research-asset-hub/web typecheck
```

若任务未改代码，`mypy` 与前端 build 可作为回归门禁按需执行；正式发布才要求恢复演练和 `generate_release_report.py`。

生成完成报告（示例路径）：

- `work/validation/2026-08-03-three-vln-models-report.md`
- `work/validation/2026-08-03-three-vln-models-report.json`

报告必须包含：新增/更新/合并/缺失/失败数量、表示候选/可用/缺失/失败数量、关系及未解析关系、dry-run 和第二次导入幂等结论、抽取与索引状态、来源完整性结果、备份 snapshot ID、网站已验证的搜索/打开方式、用户仍需手工执行的步骤（Zotero 导入确认、外部文件备份）。

**验收标准：**
- `outputs/open-catalog/manifest.json` 中来源二进制复制数为 0。
- 测试无回归；报告覆盖完成定义全部字段。
- 不隐藏 warning，不把退出码 0 当唯一证据。

## 5. 待确认事项与默认假设

| 事项 | 影响 | 默认假设 |
|---|---|---|
| 第三个模型是否确为 InternVLA-N1/InternNav | 决定第三份 manifest 的题名、仓库和关系 | 是；证据为服务器 `InternNav`/`InternVLA-candidate-20260801` 与官方 README |
| 本地 PDF 策略 | 决定 `asset_roots` 与是否联网打开 | StreamVLN 引用桌面现有 PDF；其余两篇 URL-only；用户要求离线时先下载到永久目录 |
| InternVLA-N1 与已登记 Ground Slow Move Fast 的关系 | 触发弱合并门禁和任务 6 | 两者是不同的正式文档，保留两条记录并建立 `related_to` |
| 是否生成中文导读 `note.md` | 影响表示数量和 RAG 效果 | 生成三份（推荐）；不需要则删除表示 |
| 是否更新 VLN 领域门户内容 | 决定是否修改 `config/domain-portals/v2/` 与 `content_version` | 不更新；仅用户确认发展脉络改变时更新 |

## 6. 风险与回滚

- 旁车首次正式导入后会冻结 manifest SHA-256 和 source 配置摘要；冻结后发现元数据错误不能原地改写，应创建新的更正来源并走审计流程。
- 弱合并是硬门禁：不得删除候选、伪造 DOI/arXiv 或修改 importer 绕过。
- URL 表示依赖联网；离线时打开链路会失败，报告必须明确说明，不能隐藏。
- 桌面文件若被用户视为临时位置，必须先在任务 1 移到永久目录；未经授权不移动、重命名来源文件。
- 正式导入不提供“撤销式”回滚；唯一回滚路径是平台软删除/审计更正，因此在任务 1–3 完成前不写库。

## 7. 交付说明

- 本计划保存在 `docs/plans/2026-08-03-add-three-vln-models.md`。
- 执行者按任务 1→8 顺序逐项执行，每项以“验收标准 + 验证命令”为准，可配合执行类 skill 但不依赖。
