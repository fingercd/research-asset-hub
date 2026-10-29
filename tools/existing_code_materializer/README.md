# 存量论文官方代码物化计划器

`existing_code_materializer` 把七个已有领域的联网审计 JSONL 变成可审查、可直接交给
`tools.paper_code_audit` 的归档授权，同时生成 Hub generic manifest 和无代码白名单草案。
它始终是 dry-run 规划器：不访网、不 clone、不修改 `config/`、不写数据库。

## 硬门禁

- 审计目录必须恰好包含 `video` / `agent` / `fine-tuning` / `generation` / `infra` /
  `world-model` / `vision-language-navigation` 七份 JSONL，不能缺失或多出。
- 每份 JSONL 的英文精确题名必须与对应 unit-action 日志中的失败题名一一覆盖。
- 状态只能是 `official_public` / `announced_pending` / `none_found` /
  `third_party_only`。官方公开行必须有干净 HTTPS GitHub/GitLab URL、40 位 commit 和
  license；非公开行不得携带仓库 URL/commit。
- 兼容 `canonical_repo_url` / `official_repo_url` / `repo_url` / `official_url`、
  `head` / `pinned_head` / `commit` 等异构字段；同一行别名值冲突立即失败。
- GitHub `/<owner>/<repo>/tree/<ref>/<subpath>` 和 GitLab
  `/<group>/<repo>/-/tree/<ref>/<subpath>` 会拆为 base remote + `sparse_paths`，原 URL 保留在
  `paper_evidence.original_official_url`。同 base remote 不同 commit/license 失败关闭。
- 只用开放目录中的“英文精确题名 + domain”绑定 paper asset；多候选时禁止模糊猜测。
  如果两个存量 asset 确实同题，可提供显式 `asset_id` 决议文件：

```json
{
  "resolutions": [
    {
      "domain": "vision-language-navigation",
      "exact_title": "Exact English Title",
      "asset_id": "019f..."
    }
  ]
}
```

决议只能在该题名/domain 的精确候选中选一个 asset，未使用或错配的决议也会失败。

## 运行

```powershell
cd "D:\ResearchLibrary\research-asset-hub"

.\.venv\Scripts\python.exe -m tools.existing_code_materializer `
  --audit-dir work/validation/existing-code-audit `
  --unit-actions-dir work/validation `
  --catalog outputs/open-catalog/catalog.jsonl `
  --core-root "D:/ResearchLibrary" `
  --current-allowlist config/check-unit-actions-allowlist.yaml `
  --output-dir work/validation/2026-08-31-existing-code-materialization-plan
```

如遇精确同题 asset 多候选，审查后追加：

```powershell
  --catalog-resolutions work/validation/2026-08-31-existing-code-catalog-resolutions.json
```

输出目录包含：

- `summary.json` / `summary.md`：计数、missing/extra、共享 remote、每领域 source 预期数。
- `paper-code-approvals.json`：`paper-code-approvals/v1`，已用安全归档器的真实 parser 复核。
- `paper-records.jsonl`：以开放目录 `asset_id` 为精确绑定键，专供后续归档运行。
- `manifests/<source-id>/manifest.json`：七个领域的 generic manifest 草案；无官方代码的领域为空草案。
- `hub-source-plan.yaml`：仅列出非空 source 的建议配置和 `expected_records`。
- `allowlist-additions.yaml`：按领域排序的英文精确题名增量，不覆盖当前白名单。
- `no-code-evidence.json`：保留三种非公开状态、证据 URL/原因、复核时间和源行号。

实际归档只能交给现有安全归档器；先不带 `--apply` 生成计划：

```powershell
.\.venv\Scripts\python.exe -m tools.paper_code_audit `
  --paper-records work/validation/2026-08-31-existing-code-materialization-plan/paper-records.jsonl `
  --approvals work/validation/2026-08-31-existing-code-materialization-plan/paper-code-approvals.json `
  --core-root "D:/ResearchLibrary" `
  --repository-root "D:/ResearchLibrary/共享代码/2026-08-31-存量领域补录/upstream" `
  --archive-root "D:/ResearchLibrary/共享代码/2026-08-31-存量领域补录/upstream" `
  --output-prefix work/validation/2026-08-31-existing-code-archive-review
```

人工核对 clone plan 后，主流程才可在同一命令上加 `--apply --clone-jobs 6`。这会复用
fixed-commit、`GIT_LFS_SKIP_SMUDGE=1`、non-cone sparse、Windows 非法/超长/大小写冲突路径排除、
reparse 检查与同盘原子改名语义；不要手写降级版 `git clone`。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tools/existing_code_materializer/test_materializer.py -q
.\.venv\Scripts\ruff.exe check tools/existing_code_materializer
.\.venv\Scripts\mypy.exe --strict --explicit-package-bases tools/existing_code_materializer
```
