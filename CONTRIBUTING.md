# 参与开发与维护

[返回首页](README.md) · [隔离开发](docs/DEVELOPMENT.md) · [代码地图](docs/architecture/code-map.md)

## 先保护资料和工作区

1. 运行 `git status --short`，记录并绕开已有与无关改动；不执行 `reset --hard`、`clean -fd` 或强推来制造干净状态。
2. 阅读 [AGENTS.md](AGENTS.md)与[来源边界](docs/architecture/source-boundaries.md)。涉及入库时另读[完整操作手册](docs/AGENT_KNOWLEDGE_BASE_PLAYBOOK.md)。
3. 已登记来源、冻结 manifest、原 PDF 与第三方代码只读。测试使用临时 fixture 和隔离数据库，不对个人正式库写入。
4. 使用[原生隔离开发流程](docs/DEVELOPMENT.md)。明确指定 runtime/source/data-dir，冲突端口必须保留已有进程；不得启动独立 Web/Compose 后端。

## 按改动范围验证

| 改动 | 最少检查 | 仍需说明 |
| --- | --- | --- |
| 文档、导航、仓库规则 | `python scripts/check_repository.py`、`git diff --check` | 链接检查不是内容正确性或页面视觉验收 |
| Python 业务代码 | 相关 pytest、Ruff、目标模块 mypy | 外部服务与真实文件依赖是否可用 |
| 入库 / 身份 / 来源 | 相关契约与幂等测试；有真实入库则遵循完整门禁 | dry-run、备份、第二次导入、缺失与拒绝结果 |
| 桌面前端 | typecheck、test、build；交互改动做真实原生窗口验证 | JSDOM 不能代替 WebView 的 PDF/代码实际打开 |
| 桌面壳 / 打包 | Rust 测试与目标运行时检查 | 安装、升级、退出、卸载和恢复的实际验证范围 |

在仓库根执行（Windows）：

```powershell
.\.venv\Scripts\python.exe scripts/check_repository.py
.\.venv\Scripts\python.exe -m ruff check apps packages scripts tests
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m mypy apps packages
pnpm --filter @research-asset-hub/desktop-ui typecheck
pnpm --filter @research-asset-hub/desktop-ui test
pnpm --filter @research-asset-hub/desktop-ui build
$previousTauriConfig = $env:TAURI_CONFIG
try {
    $env:TAURI_CONFIG='{"bundle":{"resources":null}}'
    cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib
} finally { $env:TAURI_CONFIG = $previousTauriConfig }
git diff --check
```

pytest 默认发现 `tests/`；`tools/` 自带的测试需对相关工具显式运行，例如 `python -m pytest tools/paper_metadata/test_extract.py -q`。资料原件不随 Git 分发，依赖它们的测试会在缺少输入时跳过；跳过不算资料验收通过。遇到原有失败，记录基线与归因，不降低断言。

检查器默认读取 Git 跟踪的 Markdown；新文档在暂存后纳入，也可用 `--files README.md docs/新文档.md` 显式检查草稿。历史计划/报告的缺链默认是 warning，可用 `--strict-history` 严格审查。它不联网，不检查锚点或秘密内容，不扫描仓库外论文目录。

## 代码与文档一起改

- 改用户行为时同步[使用手册](docs/USER_MANUAL.md)；改 MCP 参数时同步[MCP 指南](docs/MCP_USER_GUIDE.md)。
- 改启动或配置优先级时同步[开发指南](docs/DEVELOPMENT.md)；新增脚本/工具时加入对应索引并标明是否写库、下载或复制文件。
- 数据库结构通过 Alembic 迁移；搜索索引保持可重建。不要直接改正式数据库绕过 API、迁移或审计。
- `docs/plans/`、`docs/reports/` 与 `docs/superpowers/` 是日期化历史，不把旧验收改成新结果；当前规则写入现行指南。
- 论文 HTML 还须遵守资料根的制作规范。生成文件、结构通过、页码有效都不等于内容和视觉完整验收。

## Git 提交与推送

```powershell
git config fetch.prune true
git config pull.ff only
git fetch --prune origin
git status --short --branch
git config --show-origin --get user.name
git config --show-origin --get user.email
git var GIT_AUTHOR_IDENT
git var GIT_COMMITTER_IDENT
```

本维护环境使用 `fingercd <202753694+fingercd@users.noreply.github.com>`。GitHub 登录与提交署名分别检查；优先复用 `gh auth status` 所示的现有登录，不重复认证或清理凭据。其他贡献者使用自己的真实身份。

只暂存本任务明确文件，不用 `git add .` 带入用户草稿。提交前读 `git diff --cached --stat` 和 `git diff --cached`；分叉时先看差异，不自动合并、不强推。提交说明交代动机、行为和验证。推送后检查远端提交及 CI，**本机测试通过不等于远端 CI 已通过**。

发布 Release、改变仓库可见性、删库、改写已推送历史需单独授权。本项目代码为 [MIT](LICENSE)；论文、代码快照与第三方依赖保留各自许可证。
