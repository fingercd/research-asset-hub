[CmdletBinding()]
param(
    [string]$PostgresZip,
    [string]$VerifiedPostgresDirectory,
    [Parameter(Mandatory)][string]$PostgresVersion,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$PostgresSha256,
    [Parameter(Mandatory)][string]$MeiliExe,
    [Parameter(Mandatory)][string]$MeiliVersion,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$MeiliSha256,
    [Parameter(Mandatory)][string]$ResticExe,
    [Parameter(Mandatory)][string]$ResticVersion,
    [Parameter(Mandatory)][ValidatePattern('^[a-fA-F0-9]{64}$')][string]$ResticSha256,
    [ValidateSet('personal', 'public')][string]$Profile = 'public',
    [string]$SourceWorkspaceRoot,
    [string]$WebView2OfflineInstaller,
    [ValidatePattern('^$|^[a-fA-F0-9]{64}$')][string]$WebView2Sha256,
    [Parameter(Mandatory)][string]$ThirdPartyNotices,
    [string]$PythonVersion = '3.11',
    [string]$StageDir,
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$desktopRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $desktopRoot
$stage = [IO.Path]::GetFullPath($(if ($StageDir) { $StageDir } else { Join-Path $desktopRoot 'stage' }))
$utf8 = New-Object System.Text.UTF8Encoding($false)
function Test-Artifact([string]$Path, [string]$Hash, [string]$Name) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw "$Name input missing: $Path" }
    if ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -ne $Hash) { throw "$Name SHA-256 mismatch" }
}
if ([bool]$PostgresZip -eq [bool]$VerifiedPostgresDirectory) { throw 'Choose exactly one PostgreSQL zip or verified directory' }
if ($PostgresZip) { Test-Artifact $PostgresZip $PostgresSha256 'PostgreSQL' }
else {
    if (-not (Test-Path -LiteralPath (Join-Path $VerifiedPostgresDirectory 'bin/pg_ctl.exe'))) { throw 'PostgreSQL directory is missing bin/pg_ctl.exe' }
    $treeHash = & (Join-Path $projectRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'runtime-tree-digest.py') $VerifiedPostgresDirectory
    if ($LASTEXITCODE -ne 0 -or $treeHash.Trim() -ne $PostgresSha256) { throw 'PostgreSQL directory SHA-256 mismatch' }
}
Test-Artifact $MeiliExe $MeiliSha256 'Meilisearch'
Test-Artifact $ResticExe $ResticSha256 'Restic'
if ($WebView2OfflineInstaller) {
    if (-not $WebView2Sha256) { throw 'WebView2 offline input requires SHA-256' }
    Test-Artifact $WebView2OfflineInstaller $WebView2Sha256 'WebView2'
}
if (-not (Test-Path -LiteralPath $ThirdPartyNotices -PathType Leaf)) { throw 'Reviewed ThirdPartyNotices missing' }
foreach ($inputPath in @($PostgresZip, $VerifiedPostgresDirectory, $MeiliExe, $ResticExe, $ThirdPartyNotices, $WebView2OfflineInstaller)) {
    if ($inputPath -and [IO.Path]::GetFullPath($inputPath).StartsWith($stage + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Inputs cannot be inside the stage directory that will be rebuilt; choose a separate StageDir'
    }
}
if ($Profile -eq 'public' -and $SourceWorkspaceRoot) { throw 'public profile cannot embed a host workspace path' }
if ($Profile -eq 'personal') {
    if (-not $SourceWorkspaceRoot) { $SourceWorkspaceRoot = $projectRoot }
    $SourceWorkspaceRoot = (Resolve-Path -LiteralPath $SourceWorkspaceRoot).ProviderPath
}
if ($ValidateOnly) { Write-Host 'Inputs verified; no stage/runtime/build changes made'; return }
# Fixed deletion targets stay inside desktop. Never operate on caller-provided data directories.
if ((Split-Path -Parent $stage) -ne $desktopRoot) { throw 'stage target escaped desktop directory' }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
$runtimeDir = Join-Path $stage 'runtime'
$appDir = Join-Path $stage 'app'
New-Item -ItemType Directory -Force -Path $runtimeDir, $appDir | Out-Null
$buildDir = Join-Path $desktopRoot 'build'
New-Item -ItemType Directory -Force -Path $buildDir | Out-Null
$requirements = Join-Path $buildDir 'runtime-requirements.txt'
$runtimeVenv = Join-Path $buildDir 'runtime-venv'
# Export exact versions and hashes from uv.lock; never copy the development .venv.
Push-Location $projectRoot
try {
    & uv export --frozen --no-dev --no-header --no-emit-project --no-editable --format requirements-txt --output-file $requirements
    if ($LASTEXITCODE -ne 0) { throw 'uv frozen runtime export failed' }
    & uv venv --clear --python $PythonVersion $runtimeVenv
    if ($LASTEXITCODE -ne 0) { throw 'clean runtime venv creation failed' }
    & uv pip sync --require-hashes --python (Join-Path $runtimeVenv 'Scripts/python.exe') $requirements
    if ($LASTEXITCODE -ne 0) { throw 'locked runtime dependency sync failed' }
    & pnpm --filter @research-asset-hub/web build
    if ($LASTEXITCODE -ne 0) { throw 'Web build failed' }
} finally { Pop-Location }
$pythonHome = (Get-Content -LiteralPath (Join-Path $runtimeVenv 'pyvenv.cfg') -Encoding UTF8 |
    Where-Object { $_ -like 'home = *' } | Select-Object -First 1) -replace '^home = ', ''
if (-not (Test-Path -LiteralPath (Join-Path $pythonHome 'python.exe'))) { throw 'runtime base Python missing' }
$targetPy = Join-Path $runtimeDir 'python'
New-Item -ItemType Directory -Force -Path $targetPy | Out-Null
Copy-Item -Path (Join-Path $pythonHome '*') -Destination $targetPy -Recurse -Force
$targetSite = Join-Path $targetPy 'Lib/site-packages'
# The base interpreter might have system packages; only the clean locked venv
# may supply third-party dependencies to this distributable.
if (Test-Path -LiteralPath $targetSite) { Remove-Item -LiteralPath $targetSite -Recurse -Force }
New-Item -ItemType Directory -Force -Path $targetSite | Out-Null
Copy-Item -Path (Join-Path $runtimeVenv 'Lib/site-packages/*') -Destination $targetSite -Recurse -Force
# Code allowlist: Web sources/node_modules and user config/intake cannot enter public builds.
function Copy-CodeTree([string]$Relative) {
    $source = [IO.Path]::GetFullPath((Join-Path $projectRoot $Relative))
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File) {
        if ($file.Extension -notin @('.py', '.sql', '.mako', '.ini', '.css')) { continue }
        if ($file.FullName -match '[\\/](?:__pycache__|node_modules|\.venv)[\\/]') { continue }
        $suffix = $file.FullName.Substring($source.Length).TrimStart('\', '/')
        $destination = Join-Path (Join-Path $appDir $Relative) $suffix
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination -Force
    }
}
foreach ($tree in @('apps/api', 'apps/asset_gateway', 'apps/worker', 'apps/mcp_server',
    'packages/adapters', 'packages/backup', 'packages/cli', 'packages/domain',
    'packages/domain_portals', 'packages/exporting', 'packages/extraction', 'packages/graph',
    'packages/indexing', 'packages/ingestion', 'packages/integrations', 'packages/knowledge',
    'packages/security', 'packages/vln_guides', 'packages/managed_sources',
    'packages/domain_catalog', 'packages/intake', 'packages/repository_snapshots',
    'packages/workflows', 'migrations')) { Copy-CodeTree $tree }
foreach ($entry in @('apps/__init__.py', 'packages/__init__.py', 'packages/version.py')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot $entry) -Destination (Join-Path $appDir $entry) -Force
}
Copy-Item -LiteralPath (Join-Path $projectRoot 'LICENSE') -Destination (Join-Path $appDir 'LICENSE')
New-Item -ItemType Directory -Force -Path (Join-Path $appDir 'apps/web') | Out-Null
Copy-Item -LiteralPath (Join-Path $projectRoot 'apps/web/dist') -Destination (Join-Path $appDir 'apps/web/dist') -Recurse -Force
$configSource = if ($Profile -eq 'public') { Join-Path $projectRoot 'newconfig/release-defaults' } else { Join-Path $projectRoot 'config' }
Copy-Item -LiteralPath $configSource -Destination (Join-Path $appDir 'config') -Recurse -Force
[IO.File]::WriteAllText((Join-Path $appDir 'config/release-profile.txt'), $Profile, $utf8)
if ($Profile -eq 'personal') {
    & (Join-Path $targetPy 'python.exe') (Join-Path $PSScriptRoot 'validate-source-mapping.py') `
        --reference (Join-Path $projectRoot 'config/sources.yaml') `
        --candidate (Join-Path $appDir 'config/sources.yaml') --workspace-root $SourceWorkspaceRoot
    if ($LASTEXITCODE -ne 0) { throw 'personal source root mapping changed' }
    [IO.File]::WriteAllText((Join-Path $appDir 'config/workspace-root.txt'), $SourceWorkspaceRoot, $utf8)
}
$postgres = Join-Path $runtimeDir 'postgres'
if ($PostgresZip) { Expand-Archive -LiteralPath $PostgresZip -DestinationPath $postgres -Force }
else { Copy-Item -LiteralPath $VerifiedPostgresDirectory -Destination $postgres -Recurse }
if (-not (Test-Path -LiteralPath (Join-Path $postgres 'bin/pg_ctl.exe'))) {
    $nested = Get-ChildItem -LiteralPath $postgres -Directory | Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin/pg_ctl.exe') } | Select-Object -First 1
    if (-not $nested) { throw 'PostgreSQL archive has no bin/pg_ctl.exe' }
    foreach ($child in Get-ChildItem -LiteralPath $nested.FullName) { Move-Item -LiteralPath $child.FullName -Destination $postgres }
    Remove-Item -LiteralPath $nested.FullName
}
Copy-Item -LiteralPath $MeiliExe -Destination (Join-Path $runtimeDir 'meilisearch.exe')
& (Join-Path $projectRoot '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'postgres-utf8-manifests.py') `
    --postgres-directory $postgres --report (Join-Path $runtimeDir 'postgres-utf8-manifests.json')
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL UTF-8 process manifest transform failed' }
Copy-Item -LiteralPath $ResticExe -Destination (Join-Path $runtimeDir 'restic.exe')
Copy-Item -LiteralPath $ThirdPartyNotices -Destination (Join-Path $runtimeDir 'THIRD-PARTY-NOTICES.txt')
Copy-Item -LiteralPath $requirements -Destination (Join-Path $runtimeDir 'runtime-requirements.txt')
if ($WebView2OfflineInstaller) {
    Copy-Item -LiteralPath $WebView2OfflineInstaller -Destination (Join-Path $runtimeDir 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe')
}
$manifest = @{
    schema = 'research-asset-hub.runtime/v1'; profile = $Profile
    python = @{ requested_version = $PythonVersion; version = (& (Join-Path $targetPy 'python.exe') --version) }
    lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'uv.lock')).Hash.ToLowerInvariant()
    postgres = @{ version = $PostgresVersion; sha256 = $PostgresSha256; utf8_process_manifest = $true; transform_report = 'postgres-utf8-manifests.json' }
    meilisearch = @{ version = $MeiliVersion; sha256 = $MeiliSha256 }
    restic = @{ version = $ResticVersion; sha256 = $ResticSha256 }
    webview2 = @{ offline = [bool]$WebView2OfflineInstaller; sha256 = $WebView2Sha256 }
}
[IO.File]::WriteAllText((Join-Path $runtimeDir 'manifest.json'), ($manifest | ConvertTo-Json -Depth 5), $utf8)
Write-Host "Stage ready: $stage ($Profile). Select tauri.offline.conf.json for offline WebView2 builds."
