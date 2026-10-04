# Windows 运行时组装

公开构建默认使用 `public` profile，sources 和 taxonomy 从通用空模板初始化。
`personal` profile 用于当前用户升级，复制已有配置，并核验所有冻结来源根映射保持一致。
配置与原件的区别、离线安装及许可约束详见 [RUNTIME-RELEASE.md](RUNTIME-RELEASE.md)。

准备本地输入：PostgreSQL Windows x64 zip（或已校验的便携目录）、Meilisearch exe、
Restic exe，以及覆盖实际 Python、Rust、JavaScript 和二进制依赖的已审查第三方 notices。
每个二进制输入必须注明确切版本，并提供独立核验过的 SHA-256。版本标签不能替代版本核验。

下面的版本是完整调用示例；路径、版本和哈希必须换成实际已核验输入。

```powershell
$pgZip = 'D:\downloads\postgresql-16.11-windows-x64-binaries.zip'
$meiliExe = 'D:\downloads\meilisearch-windows-amd64.exe'
$resticExe = 'D:\downloads\restic_0.19.1_windows_amd64.exe'

.\desktop\scripts\stage-runtime.ps1 `
  -PostgresZip $pgZip `
  -PostgresVersion '16.11' `
  -PostgresSha256 (Get-FileHash -LiteralPath $pgZip -Algorithm SHA256).Hash `
  -MeiliExe $meiliExe `
  -MeiliVersion '1.53.0' `
  -MeiliSha256 (Get-FileHash -LiteralPath $meiliExe -Algorithm SHA256).Hash `
  -ResticExe $resticExe `
  -ResticVersion '0.19.1' `
  -ResticSha256 (Get-FileHash -LiteralPath $resticExe -Algorithm SHA256).Hash `
  -ThirdPartyNotices 'D:\downloads\THIRD-PARTY-NOTICES.txt' `
  -Profile public
```

这里 `Get-FileHash` 计算输入文件的实际哈希；正式构建前还需与供应商发布的校验值比对。
增加 `-ValidateOnly` 可只检查输入；省略该开关才会创建干净、锁定依赖的 Python 环境并构建 Web。
Node.js 仅用于开发机上的前端构建。安装包包含 Python、PostgreSQL、Meilisearch、Restic、
运行必需源码、迁移和构建后的 Web dist，包含 `python -m apps.mcp_server` 所需模块。

复用已有 PostgreSQL 目录时，使用目录树哈希并将 `-PostgresZip` 换成 `-VerifiedPostgresDirectory`：

```powershell
$postgresDirectory = 'C:\Program Files\ResearchAssetHub\runtime\postgres'
$postgresTreeHash = & .\.venv\Scripts\python.exe `
  .\desktop\scripts\runtime-tree-digest.py $postgresDirectory
# 在上面的完整调用中改用：
# -VerifiedPostgresDirectory $postgresDirectory -PostgresSha256 $postgresTreeHash
```

默认产物为 `desktop/stage/{app,runtime}`。候选产物可用 `-StageDir` 指向 desktop 的另一个直接子目录，
例如 `desktop/stage-candidate`；所有输入必须位于将清理的目标 stage 外。
`-PythonVersion` 可固定完整 Python patch 版本，实际解释器版本也会写入 runtime manifest。

个人升级前，先用 `preserve-legacy-config.ps1` 将旧安装的 `app/config` 复制到用户 `data_dir/config`，
显式提供原有 `SourceWorkspaceRoot` 并校验来源映射。该脚本拒绝覆盖已有用户 config。
随后按完整构建参数使用 `-Profile personal -SourceWorkspaceRoot <原工作区根>`。
`public` profile 不允许写入宿主工作区提示。所有模式均不移动来源原件。

离线安装可增加 `-WebView2OfflineInstaller <微软离线exe>` 和 `-WebView2Sha256 <已核验sha>`，
并在 Tauri 构建时传入 `--config tauri.offline.conf.json`。该配置跳过联网 bootstrap；
没有 WebView2 的机器需要先运行随包提供的微软离线 installer，再安装应用。
