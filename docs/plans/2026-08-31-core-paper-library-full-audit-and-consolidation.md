# 核心领域论文全量审查、压缩专题整合与资产归拢实施计划

> 日期：2026-08-31  
> 项目：Research Asset Hub / 核心领域本地论文资料库  
> 输入批次：`C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向`  
> 目标根：`D:\ResearchLibrary`  
> 状态：已完成（2026-08-31）。正式采集批次、核心副本、HTML/代码结局、Hub 导入、索引、备份恢复和发布门禁均已验证；全库非正式嵌入 PDF 另以分类报告保留。
> 权限边界：本任务不包含 commit、push、发布或删除未经验证的原始资料。

## 1. 目标与完成定义

一句话目标：对“核心领域”中全部正式论文资产和桌面新增批次建立逐篇可追踪台账，把软件实际展示的 PDF、HTML、官方代码及其配套元数据全部收拢到 `D:\ResearchLibrary` 下，并在 Research Asset Hub 中形成可搜索、可打开、可审计、可重复导入的论文单元。

完成必须同时满足：

1. 所有正式主论文 PDF 都有稳定 `paper_id/source_record_id`、完整身份元数据和一份独立 HTML；同一论文的不同版本作为同一资产的多个表示保留，不按文件名或 hash 误拆成两篇。
2. 对每篇论文都给出代码结局：`official_local`、`official_url_only`、`announced_pending`、`none_found`、`third_party_only` 或 `not_applicable`。发现作者或项目组可核验的公开官方仓库时，必须在核心领域内固定 commit 归档，并在 Hub 建立 `implemented_by` 关系。
3. 视频压缩资料经身份去重后融合进现有 `VLM框架3DV压缩`；具身智能压缩、导航压缩各自建立独立顶层单元和 Hub 领域。
4. 新批次中不属于三个压缩核心口径的论文不删除：17 篇视频生成/扩散效率论文、9 篇具身边界论文和 `其他` 191 篇均重新路由到已有领域或显式待审区，并仍形成 PDF/HTML 闭环。
5. Hub 中每个新增/变化论文单元的 PDF、HTML、代码按钮都与库内事实一致；无代码论文只能通过英文精确标题白名单通过，不能静默缺失。
6. 所有被软件主动展示的本地正式表示，其实际字节最终位于 `D:\ResearchLibrary` 下。历史冻结 source 不改 root、不改 manifest、不改原文件；先以新的核心领域 sidecar source 导入等价表示，再通过带原 source/manifest digest 的不可变 retirement/supersession 记录把旧表示退出主动展示与完整性扫描。禁止用改名、移动或 NTFS junction 替换已登记 source root。
7. 全来源 dry-run 通过；正式导入前有恢复点；输入不变的第二次 `ingest -All` 八类新增计数全部为 0；抽取、索引、HTML 审计、来源完整性、逐领域单元按钮和浏览器打开验证全部有报告。
8. 最终报告对每个候选给出明确结局，并列出新增、合并、版本、缺失、失败、无代码、待发布代码、外部路径、历史 mismatch 和人工待确认项，不能只写“已导入”。

### 1.1 “全部 PDF”的审计口径

本任务的主口径是“正式论文及软件可展示表示”，不是把上游代码仓库、依赖包、测试夹具、模型权重或缓存内的所有 PDF 当作新论文。全盘发现的每个 PDF 仍必须在台账中得到以下之一的结局：

- `formal_primary`：正式主论文，必须有独立 HTML；
- `formal_version`：同一论文的另一公开版本，关联同一论文资产并保留版本说明；
- `duplicate_copy`：字节或强身份完全重复，不重复发布，记录 canonical 目标；
- `supplement_or_attachment`：补充材料、幻灯片、模型卡导出等，按附件登记或明确排除；
- `repository_document`：上游代码仓库自带文档，不作为独立论文发布；
- `invalid_or_nonpaper`：损坏或非论文内容，进入待审区，并生成说明页而非伪造论文解读。

桌面输入批次中的每一份有效论文 PDF 均按 `formal_primary/formal_version` 处理；26 份原“排除”PDF 只是从原压缩子类排除，不从核心领域总库删除。

## 2. 已核实基线与关键事实

### 2.1 实际输入路径和批次完整性

用户消息中的两级路径 `C:\Users\ExampleUser\Desktop\顶会论文PDF\_按方向` 不存在；本机实际输入是单目录名：

`C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向`

只读盘点结果：

| 分组 | 当前 PDF | 页数 | 当前角色 |
|---|---:|---:|---|
| `其他` | 191 | 3,778 | 通用 LLM/VLM/KV/上下文/推理压缩，待映射已有领域 |
| `Embodied` | 44 | 923 | 具身智能压缩核心 |
| `Embodied\排除_非压缩或部分相关` | 9 | 214 | 转投 Agent、世界模型、RL、VLN 等邻接领域 |
| `Video` | 55 | 929 | VideoLLM 模型侧压缩核心 |
| `Video\work\deleted_pdf` | 17 | 379 | 视频生成/扩散效率等，转投“生成”等领域 |
| `VLN` | 20 | 323 | 导航压缩核心 |
| **合计** | **336** | **6,546** | 310 主集合 + 26 原排除集合 |

配套现状：336 PDF、115 Markdown、9 TXT、4 JSON、0 HTML、0 本地代码；总计 464 文件、1.469 GiB。336 份 PDF 均有 `%PDF-` 文件头、可由 Poppler 解析、未加密，源内 SHA-256 完全重复为 0。

不能直接用文件名作为身份：118 个标题在 90 字符处被批处理截断，4 个文件有尾随空格，3 个带“已读/未读”，3 个混用全角标点，2 个含 LaTeX 文件名；另有 6 个 PDF 同时缺内嵌 Title/Author。

### 2.2 Video 缺失 40 份应保留 PDF

`Video\outputs\筛选报告.md` 与 `keep_list.md` 记载原集合 112 篇、应保留 95 篇、排除 17 篇；当前磁盘只有 55 篇保留 PDF + 17 篇排除 PDF。`work\mineru` 仍有 112 份 Markdown，其中 40 份只剩 Markdown、没有 PDF，而且全部属于应保留清单。

因此正式执行前必须闭合：

- 当前实物：336 PDF；
- 待恢复：40 篇 Video 核心 PDF；
- 恢复后预期输入：376 PDF；
- 其中三个/既有领域的正式论文候选：350；原排除但仍需转投的候选：26。

40 篇的精确清单以本批次 `keep_list.md` 与本计划准备阶段的盘点报告为准。优先从核心领域现有版本或其他本地稳定目录恢复；本地不存在时，才从论文官网、OpenReview/arXiv 或作者项目页下载。MinerU Markdown 只能作为匹配证据，不能冒充原 PDF。

### 2.3 核心领域现状

工作区根不是 Git 仓库；真正的平台仓库是 `research-asset-hub`，且当前有大量用户已有修改、删除和未跟踪文件。执行必须保存 `git status --short` 基线，绝不 reset、checkout 或覆盖无关改动。

全盘只读发现约 1,229 个 PDF；排除 `.git`、`node_modules`、`.venv`、缓存、恢复演练和明显派生目录后仍有约 1,120 个 PDF 候选。桌面 336 PDF 与核心领域规范化文件名交集为 0；源内 hash 已完成，但核心领域 1,120 PDF 的跨目录分块 SHA-256 对账尚未完成，这是执行任务 1 的硬门禁。

现有主要规范库目录计数：

| 领域资料库 | 规范 PDF | 逐篇 HTML | 顶层本地官方/资源仓库 |
|---|---:|---:|---:|
| Agent | 72 | 72 | 53 |
| Infra | 71 | 71 | 44 |
| 世界模型 | 46 | 46 | 35 |
| 强化学习 | 108 | 108 | 65 |
| 微调 | 66 | 67 | 40 |
| 生成 | 64 | 64 | 36 |
| 大厂技术报告 | 86 | 90 | 54 |
| VLN | 72 | 72 | 30 |
| 游戏 RL | 72 | 72 | 23 |
| WSVAD | 45 | 45 | 24 |
| VideoLLM 压缩（尚未登记 Hub） | 33 | 33 | 29 |

Hub 当前配置实际是 11 个领域、20 个来源、`expected_records` 合计 775；根 README 中“10 个领域、11 个来源、717 条”是 2026-07-26 旧基线，不能当当前事实。

2026-08-04 的代码覆盖报告记录 654 个 paper asset、390 个 repository asset、404 篇有代码关系、250 篇无代码关系、408 个唯一本地代码目录。该报告已过时，但 250 是现有全库需重新联网分流的可靠候选队列，不能直接解释为“250 篇无公开代码”。

2026-07-26 HTML 审计只证明 836 个已登记 HTML 候选可用，不能证明没有 HTML 的论文不存在；2026-08-11 图像编辑专项为 33/33 PDF、33/33 HTML。最终必须新增 paper-level closure 审计，不再只靠 representation 候选总数。

### 2.4 已确认的三个压缩目标

| 方向 | 决策 | 物理目标 | Hub slug |
|---|---|---|---|
| 视频压缩 | 融合已有单元 | `D:\ResearchLibrary\VLM框架3DV压缩` | `video-llm-compression` |
| 具身智能压缩 | 新建独立单元 | `D:\ResearchLibrary\具身智能压缩` | `embodied-ai-compression` |
| 导航压缩 | 新建独立单元 | `D:\ResearchLibrary\导航压缩` | `navigation-compression` |

`VLM框架3DV压缩` 已有 33 PDF、33 逐篇 HTML、29 个本地官方仓库，`outputs\manifest.json` 为 `passed`，正是用户要求的可融合目标；它的边界是 VideoLLM 的视觉 token、帧选择、KV/cache、计算路径和流式记忆压缩，不包含传统 H.264/H.265/AV1、学习式 codec 或生成侧 VAE。

导航压缩与现有 VLN 允许强标识合并后多领域共存：Hub 的 `Asset.domain_slugs` 是数组，正式导入会对同一 DOI/arXiv 身份的来源领域做稳定并集。因此 Walk and Read Less、GA-VLN、StreamVLN 等无需复制成第二个 paper asset；同一资产可同时出现在 `vision-language-navigation` 和 `navigation-compression`。

### 2.5 仍在核心领域外的正式字节

当前 `config/sources.yaml` 有 5 个唯一外部 asset root，涉及 7 个已登记 source-id：

| 外部根 | 去重后的正式目标 |
|---|---:|
| `C:\Users\ExampleUser\Desktop\VideoLLM压缩裁剪` | StreamVLN PDF 1 个 |
| `C:\Users\ExampleUser\Desktop\difussion\outputs\pdfs` | PDF 33 个 |
| `C:\Users\ExampleUser\Desktop\difussion\outputs\code` | 代码目录 23 个 |
| `C:\Users\ExampleUser\Desktop\difussion\outputs\papers_html` | HTML 31 个 |
| `C:\Users\ExampleUser\Desktop\difussion\outputs\anchors` | HTML 1 个 |

合计 106 条 representation 引用、89 个唯一文件系统目标：34 PDF、23 代码目录、32 HTML。所有目标当前存在。图像编辑正式外部数据约 1.083 GB；`difussion` 全目录约 4.936 GiB，`VideoLLM压缩裁剪` 全目录约 55.3 MiB。D 盘准备阶段可用空间约 286.5 GiB，容量足够，但正式克隆前仍要重新估算并保留至少 2 倍复制/回滚余量。

这些 source 已冻结，直接改 `sources.yaml` 会触发 `FrozenSourceChangedError`。默认迁移策略改为“显式正式资产 allowlist 复制到核心领域 + 新 immutable sidecar source + hash 等价验证 + 正式 source retirement/supersession”。在 Hub 尚未具备并验证 retirement 机制前，旧 source root 和原文件必须保持原位，不能改名、移动、删除或用 junction 替换；不能以完成复制冒充完成来源迁移。

## 3. 目标目录与数据接口

### 3.1 三个压缩单元

新建单元沿用成熟资料库结构；新数据不再创建/登记 `guide` 表示，旧 `library/code/guides` 只读保留：

```text
<压缩单元>/
├─ README.md
├─ index.html
├─ library/
│  ├─ papers/<paper-id>/paper.pdf
│  ├─ analysis/<paper-id>.md
│  ├─ code/upstream/<repo-id>/
│  ├─ catalog/
│  │  ├─ papers.v1-or-v2.json
│  │  ├─ repositories.v1-or-v2.json
│  │  ├─ timeline.v1.json
│  │  └─ trends.v1.json
│  └─ site/
│     ├─ index.html
│     ├─ papers/<paper-id>.html
│     └─ assets/{styles.css,app.js}
├─ outputs/manifest.json
├─ scripts/{build_site,fetch_repositories,validate_library}.*
└─ work/
   ├─ intake-20260831/
   └─ validation/
```

- `VLM框架3DV压缩` 保留现有 v1 catalog 和 33 个单元，增量生成 v2 catalog，不覆盖历史验收证据。
- `具身智能压缩`、`导航压缩` 从 v1 开始。
- 论文目录使用稳定短 ID，不使用被截断的原文件名；优先 `arxiv-<id>`，其次规范化方法名+年份。
- 同一仓库实现多篇论文时只保留一个 `<repo-id>` 目录，并在 catalog/Hub 建多条关系。

### 3.2 `其他` 和转投论文

不新建第四个“泛大模型压缩”领域。默认按论文主任务映射到现有领域，并用稳定标签保留压缩维度：

```text
D:\ResearchLibrary\个人新增资料\2026-08-31-顶会压缩论文\
├─ agent/
├─ infra/
├─ world-model/
├─ reinforcement-learning/
├─ fine-tuning/
├─ generation/
├─ vision-language-navigation/
├─ game-rl/
├─ wsvad/
├─ image-editing/
└─ needs-review/
```

每个正式论文目录统一为：

```text
<domain>/<paper-id>/
├─ paper.pdf
├─ paper.html
├─ metadata.json
└─ code/<repo-id>/       # 仅有官方公开仓库时
```

### 3.3 全量台账

生成以下可审计产物：

- `work/validation/2026-08-31-core-paper-census.json`
- `work/validation/2026-08-31-core-paper-census.csv`
- `work/validation/2026-08-31-core-paper-census.md`
- `work/validation/2026-08-31-paper-closure.json`
- `work/validation/2026-08-31-external-root-relocation.json`

每个 canonical 记录至少包含：

```text
paper_id, source_paths[], source_sha256[], disposition,
title, title_zh, authors[], year, venue, doi, arxiv_id,
abstract, summary, tags[], domain_slugs[], source_urls[],
pdf{path,sha256,size,page_count,version},
html{path,sha256,link_check,render_check},
code{status,url,local_path,commit,license,evidence},
hub{source_id,source_record_id,asset_id,relation_status},
decision_evidence[], warnings[]
```

## 4. 实施任务

### 任务 0：冻结执行基线并隔离现有改动

**涉及位置：**

- 只读：`research-asset-hub` Git 工作树、Hub 服务状态、D 盘容量、全部 source 配置。
- 创建：本轮 `work/validation/2026-08-31-preflight-*` 报告。

**实施内容：**

1. 完整阅读并遵守 `AGENTS.md`、`prj.md`、`docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md`、`docs/USER_KNOWLEDGE_BASE_GUIDE.md`、`docs/architecture/source-boundaries.md`。
2. 运行 `git status --short`，保存本轮基线；把已有修改视为用户资产，后续只编辑本计划列明的新目录和必要配置块。
3. 运行 `./scripts/hub.ps1 status`；在物理迁移阶段前停止 Hub、worker、GitNexus 等可能写来源树的进程。
4. 记录 D 盘可用空间；估算目标 PDF、HTML、代码 shallow clone、迁移副本和回滚副本总量，要求迁移阶段至少 2 倍余量。
5. 导出现有 source-id、冻结摘要、asset roots、representation 路径和最新完整性 mismatch 基线。

**验收标准：**

- 有可复核的脏工作树清单和服务状态；没有执行 reset/clean/checkout。
- 本轮后续文件清单与已有修改无未解释重叠。
- 历史缺失、`tree_digest_mismatch` 和外部 skipped 有单独基线；旧报告对全库 mismatch 数量存在差异时，以本轮重跑结果为准，不把历史问题算成本轮回归。

### 任务 1：建立核心领域与桌面批次的 paper-level 全量台账

**涉及位置：**

- 核心领域全部 manifest/catalog/PDF/HTML/代码根。
- 桌面输入 `C:\Users\ExampleUser\Desktop\顶会论文PDF_按方向`。
- 输出：任务 3.3 的 census 文件。

**实施内容：**

1. 以 Hub manifest/catalog 中已声明的正式表示为第一权威，配合全盘 `rg --files` 发现遗漏；排除 `.git`、`.venv`、`node_modules`、cache、权重、数据集、恢复演练和构建依赖。
2. 对约 1,120 个核心领域 PDF 候选和桌面 336 PDF 分批计算 SHA-256、文件大小、页数、PDF 魔数和可解析性，避免单次命令超时；保存可恢复的分片结果后合并。
3. 对每个 PDF 分配 1.1 节中的 disposition；任何路径不得无结局。
4. 用 DOI、arXiv、正式题名、作者和年份建立身份候选。强标识一致自动候选合并；题名+年份弱匹配进入人工审查，不能自动批准。
5. 统计每个正式 paper 的 PDF/HTML/代码 URL/本地代码/关系/root-in-core 状态，形成真正的 paper-level closure 基线。

**验收标准：**

- 核心领域候选、桌面 336 PDF、已登记 paper assets 数量可相互解释。
- 源内 0 hash 重复结论复现；跨核心领域的不同名同内容重复已查清。
- 每个候选都有 canonical ID 或明确排除理由；无“扫描到了但未处理”。
- 生成 `paper-closure` 红线报告，能逐篇回答 PDF、HTML、代码、本地根和 Hub 关系，而不是只给总数。

### 任务 2：恢复 Video 40 篇缺失 PDF并完成身份、元数据和领域路由

**依赖：**任务 1 的身份台账。

**实施内容：**

1. 用 `keep_list.md` 的 40 项建立恢复队列；先在核心领域及其他稳定本地目录按强标识/hash/正式题名查找。
2. 本地不存在时，使用 agent-reach 的 GitHub/网页检索和论文一手来源下载 PDF；优先 OpenReview、arXiv、会议论文页、作者项目页，不从聚合转载站取代官方版本。
3. 将恢复 PDF 与现有 MinerU Markdown 的题名、作者、摘要/首段匹配；记录下载 URL、时间、SHA-256、版本。
4. 对全部 376 个预期输入补齐正式题名、中文题名、作者顺序、年份、venue、DOI/arXiv、abstract、summary、标签和来源 URL。118 个截断文件名和 6 个无内嵌元数据 PDF必须回到正文/官方页核实。
5. 路由规则：
   - VideoLLM token/frame/KV/cache/流式记忆压缩 → `video-llm-compression`；
   - VLA/机器人策略/具身世界模型的模型、表示、记忆或计算压缩 → `embodied-ai-compression`；
   - VLN/ObjectNav/地图或导航记忆的任务专用压缩 → `navigation-compression`；
   - 视频生成/扩散加速 → `generation`；
   - 通用 LLM KV、注意力、上下文、RAG、推理压缩 → 按主任务映射现有领域；
   - 传统 codec/learned video coding → `needs-review`，不自动混入 VideoLLM 单元。
6. 26 篇原排除项全部重分类；只有无法判断主任务时进入 `needs-review`，不得删除。

**验收标准：**

- 40 篇恢复队列每项最终必须为 `recovered_local` 或 `downloaded_official`；中间状态可以是带证据的 `unavailable`，但只要仍有一项 `unavailable` 就硬停止任务 4–9，不冻结 expected count、不正式导入、不宣告交付。
- 目标输入为 376 PDF；若未达到，继续从一手来源恢复。只有用户另行批准缩小正式范围并留下缺失 disposition 时才能改变目标，本计划不自动降低数量。
- 全部正式论文有完整身份；无截断文件名直接充当题名/ID。
- `prunevid-2025` 等不同 hash 的公开版本合并成一个论文资产的多个版本，不误判为两篇。

### 任务 3：把桌面正式资产和旧外部 asset roots 物理归拢到核心领域

**目标位置：**

- 新批次：三个压缩单元和 `个人新增资料\2026-08-31-顶会压缩论文`。
- 图像编辑旧工程：`D:\ResearchLibrary\图像编辑`。
- VideoLLM 旧桌面目录：`D:\ResearchLibrary\个人新增资料\已迁移桌面来源\VideoLLM压缩裁剪`。

**实施内容：**

1. 先复制、不覆盖；为源树和目标树生成逐文件相对路径、大小、mtime、SHA-256、目录 digest 清单。
2. 新批次 PDF 按任务 2 的 canonical ID 落入目标单元；Video 的 115 Markdown、9 TXT、4 JSON 和筛选报告作为审查证据**复制**到 `VLM框架3DV压缩\work\intake-20260831`，不登记为论文 HTML；桌面原件在最终确认前不动。
3. 对 `Desktop\difussion` 和 `Desktop\VideoLLM压缩裁剪` 只复制 manifest 明确引用的 PDF、HTML、代码目录，以及 HTML/代码打开所必需且经清单批准的相对依赖。不得默认复制完整工程、隐藏文件或未声明样本。
4. 复制前执行显式 allowlist/denylist：不跟随 reparse point/symlink；拒绝 `.git`（只另存 remote/commit 元数据）、依赖环境、缓存、build、浏览器配置、`.env`、认证/令牌/密钥、权重、checkpoint、训练数据、生成样本和孤儿 clone；每个跳过类别进入报告。若 HTML 引用的本地图片/CSS/JS 不在正式清单，先单项审核后加入依赖 allowlist。
5. 用不覆盖方式复制到核心领域目标；对 66 个文件逐文件校验 SHA-256，对 23 个代码目录以 importer-compatible tree digest 和逐文件清单双重校验。旧 source root 保持不变。
6. 为核心领域副本新建 immutable sidecar sources 并全量 dry-run/导入；实现并测试独立的 source retirement/supersession 机制：记录旧 source-id、冻结 config/manifest digest、新 source-id、逐 representation 映射和确认时间；退休只改变 Hub 主动展示/打开/完整性作用域，不改写历史 source 或 representation 证据。
7. 在新表示三按钮/网关打开、89 个目标等价、全量幂等和恢复演练通过前，旧 source 不得 retirement。retirement 通过后重扫软件主动引用，必须只指向核心领域；桌面旧字节仍暂存，最终报告后另向用户请求精确删除确认。

**验收标准：**

- 34 个旧外部 PDF、23 个完整代码目录、32 个 HTML 的实际解析目标都在核心领域。
- 7 个冻结 source 不发生 config/manifest digest 变化，不触发 `FrozenSourceChangedError`。
- 历史 representation ID 作为 provenance 可审计；主动打开使用新核心领域 representation，退休后的旧本地 URL 返回明确 superseded/retired 状态，而不是静默指向已移动文件。
- 来源完整性相对任务 0 基线没有新增 mismatch；历史 `.gitnexus` 漂移单列。
- 新批次正式 Hub source 只引用核心领域内路径，不引用 Desktop。

**回滚：**retirement 未应用前直接停用尚未正式冻结的新 sidecar 配置并保留核心领域副本；retirement 已应用后使用备份恢复数据库快照并撤销对应 retirement 事务。任何阶段都不改动旧 source root；在两边 hash 完全一致、恢复演练和软件验收前，不删除任何原始字节。

### 任务 4：构建/更新三个压缩物理单元

**视频压缩：**

1. 保留 `VLM框架3DV压缩` 已验证 33 篇及 v1 catalog；把新 Video 论文与现有 33 篇按 DOI/arXiv/题名去重。
2. 将真正新增或新版本写入 `library/papers`、`analysis`、`site/papers`、`code/upstream`；生成 v2 catalog 和新的 `outputs/manifest.json`，保留 v1 历史报告。
3. 对传统 codec、视频生成 VAE/DiT 加速坚持边界，不为凑数混入。

**具身智能压缩：**

1. 新建 `具身智能压缩` 完整结构，以 44 篇 Embodied 主集合为核心。
2. 复核现有邻接种子（如 `VAD` 中 `lightvla-2025`）；强身份相同则复用同一 Hub paper asset，物理上可用 D 盘 hardlink/明确版本引用，不能改写冻结来源。
3. 将 GUI/通用 Agent memory 论文只有在核心问题确属具身观察、行动或长期交互压缩时纳入；纯通用 Agent 压缩转投 `agent`。

**导航压缩：**

1. 新建 `导航压缩` 完整结构，以 20 篇 VLN 批次为核心。
2. 纳入/关联现有 Walk and Read Less、VLN-Cache、DyGeoVLN、GA-VLN、StreamVLN、MSNav 等明确压缩种子；仅“高效”但压缩不是主贡献的工作列为邻接参考，不计核心统计。
3. 对与 VLN atlas 相同 DOI/arXiv 的论文使用强标识合并，得到同一 paper asset 的多领域 `domain_slugs`；不得关闭合并制造重复 paper。

**共同验收：**

- 每个单元 `README.md` 明确边界、时间范围、方法分类和代码政策。
- 分别核对 canonical paper 数、source-record 数、PDF version/representation 数、HTML version 映射数和多领域 membership 数；每个 PDF version 必须映射一个 HTML 结局，但这些数量不要求彼此机械相等。`validate_library` 退出 0、manifest `status=passed`。
- 新数据不创建 `role: guide`；旧 VLM guides 只读保留。
- 根 `index.html`、`library/site/index.html` 和每篇 HTML 的相对链接在离线环境可用。

### 任务 5：逐篇核验官方公开代码并本地归档

**范围：**新批次全部正式论文 + 现有代码覆盖基线中无 repository relation 的候选论文。旧报告的“250”只作为起点，先按任务 1 当前数据库重算。

**证据优先级：**

1. PDF 正文脚注/Code available 链接；
2. 作者或机构官方项目页；
3. 官方 GitHub/GitLab README 明确引用题名、DOI/arXiv；
4. 作者主页到仓库的直接链接。

搜索聚合页、第三方复现和同名仓库只能作为候选，不得冒充官方实现。技术检索只引用一手来源。

**实施内容：**

1. 使用 agent-reach 的 GitHub/网页路由，按互不重叠的 paper ID 批次并行核验；保存 URL、证据页面和判断日期。
2. 先以规范化 remote URL 对照核心领域现有约 408 个本地代码目录；已有官方仓库则复用，不重复 clone。
3. 对新官方公共仓库执行固定 commit 的 shallow/partial clone，设置 `GIT_LFS_SKIP_SMUDGE=1`，不下载模型权重、训练数据、视频、压缩包、缓存、依赖环境或无关大子模块；必要时做 sparse checkout。只要官方公共代码仓库真实存在，`official_url_only` 不能通过正式验收：必须成功归档为 `official_local`，否则本轮硬停止并逐篇修复。
4. 保存 remote、default branch、commit、license 文件/hash、clone 方式、是否含 LFS/submodule；无 license 的公开源码标记 `source_available_no_license`，不把它描述成可自由再分发。
5. `announced_pending` 保留官方声明与复查日期；`none_found` 必须记录搜索范围；`third_party_only` 不克隆为官方代码。导入层统一映射为：`official_local -> available`；其余确无当前官方公共仓库的状态 -> `not_provided`，并按英文精确标题进入无代码白名单，同时在本轮证据报告保存细分状态。`official_url_only` 不得映射到白名单。
6. 更新 unit catalog/manifest 的本地 path 和真实 URL；导入后用 `reconcile_repository_relations.py` 建立 `implemented_by`。
7. 确无公开代码的英文精确标题加入 `config/check-unit-actions-allowlist.yaml` 对应 domain；白名单变更必须有本轮调研证据。

**验收标准：**

- 每篇论文的 code 状态非空；发现官方公开仓库时本地目录存在、remote/commit 可核对。
- 没有把 HF 模型、数据集、论文附件或第三方复现误登记为官方 repository。
- 同一仓库只归档一次并可关联多篇论文；没有空壳 clone、LFS 权重或无关数据把磁盘占满。
- 代码 URL 不为空；本地目录存在不等于完成，Hub repository asset 与 `implemented_by` 关系必须可查。

### 任务 6：使用子 Agent 批量生成并复核逐篇 HTML

用户明确要求 HTML 交给子 Agent。执行时采用“主 Agent 冻结模板/记录，子 Agent 只写互斥输出目录”的方式，避免共享文件冲突。

**主 Agent 前置产物：**

- 每篇独立 `paper-record.json`，包含已核实元数据、PDF 路径、允许使用的证据片段、代码状态和目标 HTML 路径。
- 一份只读 HTML 模板/样式和必需章节契约。
- 按 stable ID 排序的 disjoint batch 清单。

**建议并行拆分：**

- 2 个 HTML worker：VideoLLM 压缩；
- 1 个 worker：具身智能压缩；
- 1 个 worker：导航压缩；
- 2–3 个 worker：`其他`/转投论文按目标 domain 分批；
- 1 个独立 verifier：只审计结构、证据、链接和渲染，不改共享 catalog。

每个 worker 只能拥有其 batch 的 `analysis/<paper-id>.md` 和 `site/papers/<paper-id>.html`（或单篇目录的 `paper.html`），不得编辑共享 CSS、catalog、manifest、sources/taxonomy 或其他 worker 文件。

**HTML 最小内容：**

- UTF-8、`lang=zh-CN`、viewport；
- 正式题名/中文题名、作者、年份、venue、DOI/arXiv、来源；
- 问题、方法、关键实验与适用条件、局限、与领域关系；
- 代码状态、官方 URL、固定 commit 和复现注意事项；
- 本地 PDF、代码、返回总览、上一篇/下一篇链接；
- 结论和数字必须来自 PDF/一手证据，不填 Agent 猜测；无法确认时明确写“未核实”。

**特殊项：**

- 26 篇原排除论文完成重新路由后仍生成完整 HTML。
- 真正的非论文/损坏件只生成“待确认说明页”，包含原路径、hash、原因，不编造论文内容。
- 与核心领域既有论文强身份合并且已有合格 HTML 时，可复用 canonical 内容并生成领域导航页/别名映射，不重复生成相互漂移的两份事实文本。

**验收标准：**

- 当前 336 PDF 均有 HTML 结局；恢复 40 后目标为 376 个逐 PDF/版本映射，无遗漏。
- 每页本地链接 0 broken，外部依赖 0，禁止 `file://` 和缺失远程 CSS/JS。
- 静态审计覆盖全部新页；Playwright 对全部页面做加载/console/overflow 检查，对每批抽样视觉截图；390px 与桌面宽度均无横向溢出。
- verifier 报告逐篇列 passed/failed；失败页返给原 owner 修复，主 Agent最终整合 catalog。

### 任务 7：创建 Hub taxonomy、旁车 source 和三个 v2 领域门户

**创建/修改：**

- 修改：`config/taxonomy.yaml`，新增 `video-llm-compression`、`embodied-ai-compression`、`navigation-compression`。
- 新建：`config/intake/video-llm-compression-20260831/manifest.json`。
- 新建：`config/intake/embodied-ai-compression-20260831/manifest.json`。
- 新建：`config/intake/navigation-compression-20260831/manifest.json`。
- 新建：`config/intake/top-conference-20260831-<domain>/manifest.json`（只为有转投论文的既有领域创建）。
- 修改：`config/sources.yaml`，只追加新 source；不修改已冻结 source 块。
- 新建：`config/domain-portals/v2/<compression-slug>/` 的八个内容文件。
- 修改：`config/domain-portals/v2/portal-index.yaml`。
- 按证据修改：`config/check-unit-actions-allowlist.yaml`。
- 仅经确认的弱合并才修改：`config/identity-merge-approvals.yaml`。

**实施内容：**

1. 每个批量来源使用最终冻结的 `generic_manifest`；一个 source 只对应一个 domain，`expected_records` 等于实际 items 数。
2. manifest 路径全部相对于声明的核心领域 asset root；保存 PDF/HTML SHA-256、正式代码 URL 和本地路径；新 HTML 角色为 `analysis` 或 `derived`，不得登记 `guide`。
3. DOI/arXiv 相同的导航/VLN、具身/VAD 等记录允许强身份合并；题名+年份弱候选逐项核对，未经用户确认不写审批。
4. 三个新门户各建 `portal.yaml`、`overview.md`、`timeline.yaml`、`capability-method-map.yaml`、`landmarks.yaml`、`benchmarks.yaml`、`reading-routes.yaml`、`open-questions.md`，按固定九章节生成。
5. 默认页面由 v2 portal 和数据库动态目录生成；不要把领域正文写死在 React，也不要把物理 `index.html` 重新塞进默认 iframe。

**验收标准：**

- taxonomy slug 唯一且稳定；source id 无重复；expected count 与 manifest 相等。
- `asset_roots` 全部位于核心领域，新 source 不含 Desktop/Downloads/临时路径。
- 每个 item 有稳定 ID、正式题名、PDF、HTML、代码状态；无新 guide。
- 三个 `/domains/<slug>/portal` 均返回 v2、九章节、`stale=false`，不因缺门户返回 503。
- 同一导航论文能以一个 asset 同时出现在 VLN 与导航压缩领域。

### 任务 8：冻结前检查与全来源 dry-run

**命令顺序：**

```powershell
./scripts/hub.ps1 preflight
./scripts/hub.ps1 validate -All -Json
./scripts/hub.ps1 inventory -All
./scripts/hub.ps1 ingest -All -DryRun
```

另运行三个门户契约测试和单元资料库 `validate_library`。

**逐项检查：**

- 顶层 `status=passed`、`errors=[]`；
- `duplicate_source_record_keys=[]`；
- 每个 source `expected_count_matches/record_count_matches=true`；
- `unapproved_review_groups=[]`；
- 路径、hash、表示角色、URL 与任务 3–7 一致；
- `asset_path_missing` 只允许等于逐篇批准清单；
- 三个压缩 source 的实际输出 domain 与 taxonomy 一致；
- Desktop 新批次路径没有进入正式 manifest；
- 冻结旧 source config digest 无变化；
- paper-level closure 中 PDF/HTML 缺口为 0，代码只有 available 或明确白名单状态。

任何弱合并、路径缺失、记录数差异或冻结摘要错误都是硬停止，不能为了继续而改题名、伪造 DOI 或绕过 importer。

### 任务 9：备份、两次正式导入和论文—代码关系闭环

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/create_backup.py `
  --report work/validation/2026-08-31-backup-before-ingest.json
./scripts/hub.ps1 ingest -All
./scripts/hub.ps1 ingest -All
```

两次导入之间不得修改 manifest、sources、taxonomy、来源文件或 retirement/supersession 记录。第二次以下计数必须全部为 0：

- `source_records_created`
- `source_record_versions_created`
- `paper_assets_created`
- `repository_assets_created`
- `representations_created`
- `relations_created`
- `relation_evidence_created`
- `field_claims_created`

随后对每个新 source 先预览、再应用：

```powershell
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py `
  --source <alias> --confirmation-status confirmed
./.venv/Scripts/python.exe scripts/reconcile_repository_relations.py `
  --source <alias> --confirmation-status confirmed --apply
```

**验收标准：**

- 备份报告有 snapshot ID；第一次导入 schema v2 且 validated dry-run passed。
- 第二次八类新增均为 0。
- 每个本地官方仓库有 repository asset 和已确认 `implemented_by`；无错误跨论文关系。
- 多领域强身份合并结果与台账一致，未丢失任何 PDF/HTML 版本。

### 任务 10：抽取、HTML 策略、正式索引和软件打开验证

**实施内容：**

```powershell
./.venv/Scripts/python.exe scripts/seed_jobs.py
./.venv/Scripts/python.exe scripts/audit_html.py `
  --report work/validation/2026-08-31-html-audit.json `
  --dynamic-samples 24
./.venv/Scripts/python.exe scripts/apply_html_policies.py `
  --report work/validation/2026-08-31-html-policy.json
./.venv/Scripts/python.exe scripts/build_indexes.py `
  --version 20260831-core-paper-consolidation `
  --report work/validation/2026-08-31-index-build.json
./.venv/Scripts/python.exe scripts/verify_source_integrity.py `
  --report work/validation/2026-08-31-source-integrity.json
```

等待 worker 的 pending/running 归零；任何 failed/dead-letter 都逐项解释。随后对全部受影响领域运行：

```powershell
./.venv/Scripts/python.exe scripts/check_unit_actions.py --domain <domain-slug>
```

并验证：

1. 完整题名、DOI/arXiv、作者、代码符号均可搜索；
2. PDF/HTML/代码三类按钮和网关链接可打开；
3. 代码按钮只在 repository asset + relation 完整时可用；
4. 三个新领域门户九章节、无 iframe、桌面/390px 无横向溢出；
5. 新资产进入正式 assets/documents/code 索引，正式向量索引不得用 hashing-only 冒充；
6. 来源完整性相对任务 0 无新增 mismatch；历史 9 个 missing/历史 tree drift 单列。

### 任务 11：全库回归、外部路径闭包和最终交付

**测试：**

```powershell
./.venv/Scripts/ruff.exe check apps packages scripts tests
./.venv/Scripts/mypy.exe --strict apps packages scripts
./.venv/Scripts/python.exe -m pytest -q
pnpm --filter @research-asset-hub/web lint
pnpm --filter @research-asset-hub/web typecheck
pnpm --filter @research-asset-hub/web test -- --run
pnpm --filter @research-asset-hub/web test:e2e
pnpm --filter @research-asset-hub/web build
./.venv/Scripts/python.exe scripts/export_catalog.py
```

批量新增和物理迁移完成后创建最终备份并做隔离恢复演练，生成 release report；只有 `fail=0` 才交付。

**外部路径闭包：**

1. 重扫 sources、manifest、数据库 representations 和资产网关注册表。
2. 对每个**主动展示**的本地正式表示解析最终真实路径；必须在核心领域。外部旧表示只能处于可审计的 retired/superseded 状态，不能继续作为按钮或网关的活动目标。
3. 新 source 的绝对 root 不得是 Desktop；历史 provenance 文本可保留，但不得作为可打开本地表示。
4. 桌面新批次只是采集源，Hub 不引用它。是否删除该原始采集副本不作为正式资产闭包的一部分；若用户要求桌面也清空，另做经过 hash 验证的迁移/删除确认。
5. 对 `difussion` 和 `VideoLLM压缩裁剪` 的旧 source 原件，在 retirement、恢复演练和最终报告通过后再请求用户确认精确删除；删除后说明目标、对应核心副本和可恢复性。

**最终报告至少包含：**

- 全盘候选、正式论文、版本、重复、附件、非论文数量；
- 桌面 336 + 恢复 40 的逐项结局；
- 三个压缩单元的最终论文/PDF/HTML/本地代码数量；
- `其他` 191 和原排除 26 的路由数量；
- 现有无代码关系队列的 `official_local/none_found/pending/...` 分流；
- 新增/合并/更新 paper 和 repository asset 数；
- HTML passed/failed、链接和浏览器结果；
- dry-run、幂等、索引、来源完整性、单元按钮结果；
- 外部根 89 个正式目标的复制、supersession/retirement 和主动路径闭包状态；
- 备份 snapshot ID、恢复演练结果和用户仍需确认的桌面临时备份清理。

## 5. 子 Agent 协作与文件所有权

执行阶段允许并鼓励并行，但必须遵守：

1. 主 Agent 独占：taxonomy、sources、intake manifests、identity approvals、无代码白名单、共享 catalog、模板、导入、数据库、索引和最终报告。
2. HTML worker 只拥有分配 paper ID 的 `analysis` 和 HTML 文件；不编辑共享配置。
3. 代码调研 worker 只返回结构化证据或写其分配 `<repo-id>` 目录；同 remote URL 由主 Agent 去重后才允许 clone。
4. 迁移 worker 不与 HTML/代码 worker 同时操作同一来源树；source retirement/supersession 事务由主 Agent 单点执行。
5. verifier 不重写生产文件，只输出失败清单；修复交回原 owner。
6. 所有 agent 都知道自己不是唯一编辑者，不得回滚他人修改。

## 6. 风险、硬停止条件与回滚

| 风险/条件 | 处理 |
|---|---|
| Hub 工作树已有大量改动 | 保存基线、只做局部增量；发生重叠先停，不覆盖用户内容 |
| 40 篇 Video PDF 无法全部恢复 | 逐篇列失败证据；不把 MinerU Markdown伪装成 PDF，不冻结错误 expected count |
| 题名被截断/同名论文 | 以 DOI/arXiv、作者、正式题名核实；弱匹配未经确认不导入 |
| 冻结 source 改路径 | 禁止直接改；建立核心领域 sidecar source，并先实现可审计的 retirement/supersession |
| retirement/supersession 验证失败 | 不改变旧 source 活动状态，不删除旧字节；从数据库备份恢复后修复机制 |
| 复制/hash 不一致 | 不切换、不删除源；重新复制单项并保留报告 |
| 官方仓库巨大/LFS/子模块 | 先估算，skip smudge + sparse/partial clone；不下载权重和数据 |
| 公开仓库无 license | 可记录源码可见，但标记许可不明，不宣称可自由复用 |
| HTML 数字或结论无法核实 | 标“未核实”，不得补写推测；返工证据不足页面 |
| 历史 source integrity 已有 drift | 验收“无新增 mismatch”；历史问题单列，不伪报全绿 |
| 第二次导入非零 | 停止索引和交付，先修幂等/输入漂移 |
| D 盘余量不足 | 在 clone/复制前停止，不清理非任务数据；缩小 clone 或请求用户决策 |

正式导入不是简单文件撤销即可回滚，因此任务 0–8 未全部通过前不得写库。平台状态依赖备份/审计更正；来源物理迁移依赖 hash 相等的临时回滚副本。

## 7. 默认假设与需要在执行中确认的事项

| 事项 | 默认决策 | 何时需要停下确认 |
|---|---|---|
| 视频压缩范围 | VideoLLM 模型侧压缩融合 `VLM框架3DV压缩` | 发现传统 codec/learned coding 且无合适既有领域 |
| 三个领域 | 视频、具身、导航分别为三个 Hub slug；仅视频复用现有物理单元 | 用户要求不同显示名称/领域边界 |
| `其他` 191 | 映射已有领域，不建第四个“泛压缩”领域 | 大批论文无法合理归入现有领域 |
| 26 原排除项 | 转投其他领域并制作 HTML，不删除 | PDF 实际不是论文或内容损坏 |
| 导航/VLN 重复 | 强标识合并为一个多领域 asset | 存在人工 domain override 或强标识冲突 |
| 冻结外部根 | 核心副本 + 新 source + 不可变 retirement/supersession；旧 source 证据只读保留 | retirement 机制无法满足来源审计或恢复要求 |
| 桌面输入副本 | Hub 不引用，复制完成后暂时保留采集源 | 用户要求清空桌面时，执行前需确认精确删除目标 |
| HTML 内容 | 中文解读 + 一手证据，子 Agent 分批生成 | PDF 无法解析或元数据冲突 |
| 代码口径 | 只归档可核验官方公开仓库 | 只能找到第三方复现或许可/所有权冲突 |

## 8. 交付说明

- 本计划保存于 `research-asset-hub/docs/plans/2026-08-31-core-paper-library-full-audit-and-consolidation.md`。
- 执行顺序为任务 0→11；任务 5 与任务 6 可在任务 2–4 的记录冻结后并行，其余门禁不得跳过。
- 每项以“验收标准 + 证据报告”为完成依据，不以命令退出码 0 或文件存在作为唯一结论。
- 本计划不授权 commit、push、发布或提前删除桌面原件。
