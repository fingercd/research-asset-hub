# 论文官方代码审计与归档工具

`paper_code_audit` 把 PDF 离线抽取的代码线索、`paper-record` 中已复核的代码状态和核心领域内现有 Git remote 汇总为逐篇审计报告。默认模式完全离线：不访网、不 clone、不修改 PDF、Hub 配置或已有代码目录。

工具严格区分“线索”和“官方事实”：

- `paper_metadata.code_leads` 永远是 `candidate_unverified`，即使 URL 在本地恰好找到同 remote 的仓库，也不会自动升级为官方代码。
- `paper-record.code.status=official_url_only` 是硬阻断；它不会映射为 Hub `availability=available`。
- 只有已复核官方 URL 在核心领域内存在、remote 一致、HEAD 是固定 40 位 commit、工作树干净时，才输出 `official_local`。
- 只有审批文件明确记录“当前无官方公开代码”，才输出 `not_provided` 和英文精确题名白名单候选。工具不会修改 `config/check-unit-actions-allowlist.yaml`。

## 输入

### 1. `--metadata`

可重复传入 `tools.paper_metadata` 的 JSONL 或 CSV。JSONL 包含完整 evidence，应优先使用；CSV 只有 facts 和序列化后的 `code_leads_json`，是有损输入。追加式 JSONL 按 PDF 规范路径只保留最新修订。

### 2. `--paper-records`

可重复传入 `tools/paper_intake_html/paper-record.schema.json` 形状的单对象、数组、JSONL 或 CSV；JSON 也可用 `records` / `papers` / `items` 包装。安全匹配优先使用 PDF SHA-256、规范路径和唯一 DOI/arXiv；题名+年份只产生人工复核 warning，不自动绑定。

若当前批次尚无生产 `paper-record`，可仅传 `--metadata`。报告会用完整 `pdf-sha256-<64hex>` 作临时审计键；它不是 Hub `source_record_id`，后续生成 paper-record 时应把同一 SHA-256 写入 `pdf.sha256` 再重跑。

`paper-record.paper_id` 也只是 HTML/批处理工作流键，本工具不会把它默认当成 Hub `source_record_id`。生成新 generic sidecar 时仍需按项目规则分配独立、稳定的来源键，不得直接用 DOI、arXiv、题名或文件名替代。

### 3. 本地仓库根

`--core-root` 是必填的核心领域根。默认在该根下查找 `.git/config`；可用可重复的 `--repository-root` 限制到已知代码资产根以加快扫描。扫描不跟随 symlink/junction，不读取 `.env`、token 或认证缓存，并会从输出 remote 中删除 userinfo、端口、query 和 fragment。

## 离线审计

```powershell
.\.venv\Scripts\python.exe -m tools.paper_code_audit `
  --metadata work/validation/2026-08-31-acquisition-paper-metadata.jsonl `
  --paper-records D:/ResearchLibrary/work/paper-records.jsonl `
  --core-root D:/ResearchLibrary `
  --repository-root D:/ResearchLibrary/VLM框架3DV压缩/outputs/code `
  --output-prefix work/validation/2026-08-31-paper-code-audit
```

如果尚无 paper-record，删除对应参数即可。会生成：

- `<prefix>.json`：完整逐篇证据、状态映射、本地 remote 命中、共享仓库和阻断项；
- `<prefix>.csv`：便于人工复核的逐篇平表；
- `<prefix>.network-queue.jsonl`：待联网核验/解析/归档队列；
- `<prefix>.clone-plan.json`：只含已审批官方仓库的安全命令数组与目标。

`--strict` 在任一论文不是 `official_local` 或已审批 `not_provided` 时返回 3；普通离线审计只要输入和报告完整即返回 0。

## 显式审批和安全归档

审批文件必须符合 `paper-code-approvals.schema.json`。最小示例：

```json
{
  "schema_version": "paper-code-approvals/v1",
  "repositories": [
    {
      "url": "https://github.com/example/repository",
      "official": true,
      "commit": "0123456789abcdef0123456789abcdef01234567",
      "destination": "github/example-repository",
      "papers": [
        {"paper_id": "paper-id", "exact_title": "Exact English Paper Title"}
      ],
      "sparse_paths": ["src", "configs"],
      "evidence": ["paper statement + reviewed official project page"]
    }
  ],
  "no_code": [
    {
      "paper_id": "another-paper-id",
      "exact_title": "Another Exact English Title",
      "verified_no_public_code": true,
      "reviewed_at": "2026-08-31",
      "evidence_urls": ["https://example.org/reviewed-paper-page"]
    }
  ]
}
```

只加 `--approvals` 会生成归档计划，仍不访网。将来人工确认后才可额外加 `--apply --archive-root <核心领域内已存在目录>`。`--apply` 的强制条件是：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_code_audit `
  --paper-records work/validation/2026-08-31-paper-intake-applied/paper-records.jsonl `
  --approvals work/validation/2026-08-31-paper-code-runtime-approvals.json `
  --core-root D:/ResearchLibrary `
  --repository-root D:/ResearchLibrary/共享代码 `
  --archive-root D:/ResearchLibrary/共享代码 `
  --output-prefix work/validation/2026-08-31-paper-code-apply `
  --apply `
  --clone-jobs 6
```

`--clone-jobs N` 取值 1..8，默认 1。并行模式仅调度尚缺失且 destination 唯一的仓库；已有 remote 命中会立即复用，重复 destination 会对所有冲突项失败关闭且不进入 worker。每个 worker 只持有自己的审批对象、Git 子进程和 `TemporaryDirectory`；单个 clone 失败不会取消其他任务，最终结果始终按 `canonical_key` 稳定排序，可以安全断点重跑。

`--apply` 的其余强制条件是：

- URL 必须是无凭据/无 query 的 HTTPS GitHub/GitLab 仓库；
- `official=true`，并按 `paper_id + exact_title` 绑定当前输入；
- commit 必须是完整 40 位 SHA，不接受 branch/tag/HEAD；
- 使用 `GIT_LFS_SKIP_SMUDGE=1` + `--filter=blob:none` + `--no-recurse-submodules`；
- checkout 前固定 `core.autocrlf=false` / `core.eol=lf`，再用 `git ls-tree -r -z --name-only <commit>` 构建 non-cone sparse 工作树；以 include-all 为基础，自动排除权重/检查点/数据集目录、权重后缀、UTF-16 相对路径超过 220 字符和 Windows 非法路径组件（尾点/尾空格、保留名、冒号等），不再因仓库内存在这些路径而放弃整仓代码；
- 显式 approval `sparse_paths` 与自动排除合并，安全排除优先；checkout/reapply 后通过 skip-worktree 位与文件系统双重断言所有排除项均未落地，并在结果记录 `auto_excluded_paths` 及原因；
- 同一相对路径的大小写折叠碰撞按原始路径稳定排序，保留首项（例如 `README.md`）并以 `windows_casefold_collision` 排除其余项；验证会核对物理文件的 Git blob 确实对应保留项；
- 若 Git for Windows 仍因尾点/冒号等拒绝普通 checkout，则改用 fixed-commit `update-ref + read-tree + sparse reapply + checkout-index(safe paths only)` fallback；HEAD 仍锁定审批 commit，非安全路径不会物化；
- dirty 验证仅例外接受 ` D <path>` 且 path 精确属于 `auto_excluded_paths`；任何其他 `M`/`D`/未跟踪项仍失败关闭；
- LFS 只记录指针，绝不 pull；
- clone 在同盘临时目录完成，复核 remote/commit/dirty 后才原子改名；目标已存在或途中出现都拒绝覆盖；
- 归档证据记录 remote、commit、license 文件/判定、dirty、submodule 路径、LFS 文件/模式和 sparse 范围。

同一规范 remote 只产生一个 clone 计划；一条审批可在 `papers` 中绑定多篇论文，报告会输出仓库复用关系。

## 编译 Agent 官方代码研究

`compile_research.py` 把五批 `work/validation/code-research/batch-*.jsonl` 与 paper-intake-builder 的 JSONL 编译成一份 `paper-code-approvals/v1` 审批文件。该命令只写 JSON/Markdown，永远不 clone。

```powershell
$batches = Get-ChildItem work/validation/code-research/batch-*.jsonl | Sort-Object Name
$compileArgs = foreach ($batch in $batches) { "--batch"; $batch.FullName }

.\.venv\Scripts\python.exe -m tools.paper_code_audit.compile_research `
  @compileArgs `
  --paper-records work/validation/2026-08-31-paper-intake-plan/paper-records.jsonl `
  --output work/validation/2026-08-31-paper-code-approvals.json `
  --summary work/validation/2026-08-31-paper-code-approvals-summary.md
```

编译门禁：

- 必须恰好 5 个 `batch-*.jsonl`、合计 376 条、`paper_id` 唯一，且与 376 条 paper-record 集合完全一致；
- 状态只能是 `official_public` / `none_found` / `third_party_only` / `announced_pending`，并强制 237 条公开、139 条非公开；
- 公开条目必须是干净 HTTPS GitHub/GitLab URL + 40 位 commit；`/tree/<branch>/<path>` 会拆成规范 repo URL 和 `sparse_paths`；
- 按规范 remote 去重；同 remote 不同 commit 硬失败，同 remote 的多篇论文只生成一个 destination/clone plan；
- `papers` 保留 `paper_id + Agent 核验 exact_title`，同时保留原 paper-record 题名和匹配类型；
- 139 条非公开决策进入 `no_code`，保留细分状态、公开占位仓库/固定 commit（如有）、license、evidence URL/原因、复核时间和源行号；
- 输出 `audit.clone_plan`、共享仓库关系与 Markdown 全量预计 clone 表，`network_or_clone_performed=false`。

编译产物仍只是归档授权。真正归档时必须把核心领域内共享代码根显式传给前述 `--archive-root`，并额外加 `--apply`。

## 绑定当前最终 paper-record

Research approvals 保留 Agent 联网核验时的精确题名，而经过后续 PDF/元数据修正后的 applied/final paper-record 题名可能不同。`bind_current_records.py` 会产生专供运行时/归档的 approvals：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_code_audit.bind_current_records `
  --research-approvals work/validation/2026-08-31-paper-code-approvals-v2.json `
  --paper-records work/validation/2026-08-31-paper-intake-applied/paper-records.jsonl `
  --output work/validation/2026-08-31-paper-code-runtime-approvals.json
```

该步骤的边界是：

- Research approvals 和当前 paper-record 必须各覆盖恰好 376 个唯一 `paper_id`，两个 ID 集合必须完全一致；
- 一个 ID 只能出现在一个 repository binding 或一条 `no_code`，不得重复或无证据换绑；
- repository 每个 paper ID 必须有同 ID `paper_evidence`；`no_code` 必须有 evidence URL/原因/复核时间；
- 输出把 `exact_title` 和 `paper_record_title` 设为当前 record.title，同时在 `research_exact_title` / `research_paper_record_title` / `title_match` 保留研究阶段信息；
- repository/no-code 决策、commit、remote、license、细分状态和原证据均原样保留；
- 输出前使用当前 paper-record 重跑 runtime approval parser；仍不联网、不 clone。

## HTML / Hub API 状态映射

| 审计状态 | paper HTML `code.status` | Hub repository availability | API code quick action | 验收 |
| --- | --- | --- | --- | --- |
| `official_local` | `official_local` | `available` | `available` | 通过 |
| `official_url_only` | `official_url_only` | 不得标 `available` | 暂不应作为完成资产 | **硬阻断** |
| 已审批 `not_provided` | `none_found` | 无 repository 表示 | `not_provided` | 仅精确题名白名单后通过 |
| 其他 | 对应 warning/`unknown` | 不得标 `available` | 未闭环 | 阻断 |

本工具只产生证据与归档；不修改冻结 manifest、`config/sources.yaml`、白名单或 Hub 数据库。正式入库仍必须走 preflight/dry-run/备份/两次幂等 ingest/reconcile/check-unit-actions 的项目门禁。

## 清理中断的 clone staging

`cleanup_staging.py` 默认只报告。它只枚举显式 archive root 的直属 `.paper-code-audit-*` 目录，不会进入或删除 `github/` 等正式目录。

```powershell
.\.venv\Scripts\python.exe -m tools.paper_code_audit.cleanup_staging `
  --archive-root D:/ResearchLibrary/共享代码 `
  --min-age-seconds 300 `
  --report work/validation/paper-code-staging-cleanup.json `
  --apply `
  --confirm CLEAN_STALE_CODE_STAGING
```

每个候选都会重新校验 resolved path 是 archive root 的严格直属子项、名称前缀正确、树内无 reparse point、最新 mtime 超过年龄门禁，且不存在命令行引用该路径的 Git/Python 进程。报告先记录文件数和总字节，应用时再复查一次后才对该精确路径调用 `shutil.rmtree`。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_audit.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_compile_research.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_bind_current_records.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_parallel_clone.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_sparse_safety.py -q
.\.venv\Scripts\python.exe -m pytest tools/paper_code_audit/test_cleanup_staging.py -q
.\.venv\Scripts\ruff.exe check tools/paper_code_audit
```
