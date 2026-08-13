[CmdletBinding()]
param(
    [string]$PostgresZip,      # EDB PostgreSQL 16 Windows zip binaries 路径(必填,一次性下载)
    [string]$MeiliExe,         # meilisearch-windows-amd64.exe 路径(必填,一次性下载)
    [string]$NodeZip           # node-v20.x-win-x64.zip 路径(必填,一次性下载)
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $PostgresZip)) { throw "PostgresZip 路径不存在: $PostgresZip" }
if (-not (Test-Path $MeiliExe)) { throw "MeiliExe 路径不存在: $MeiliExe" }
if (-not (Test-Path $NodeZip)) { throw "NodeZip 路径不存在: $NodeZip" }
$desktopRoot = Split-Path -Parent $PSScriptRoot          # desktop/
$projectRoot = Split-Path -Parent $desktopRoot
$stage = Join-Path $desktopRoot 'stage'
$runtimeDir = Join-Path $stage 'runtime'
$appDir = Join-Path $stage 'app'

Write-Host '[1/5] 清理并创建 stage 目录'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $runtimeDir, $appDir | Out-Null

Write-Host '[2/5] 组装便携 Python + 项目依赖(拷 .venv)'
$venvSource = Join-Path $projectRoot '.venv'
if (-not (Test-Path $venvSource)) { throw '.venv 不存在,先在项目根跑 uv sync' }
# 解析 .venv/pyvenv.cfg 的 home 行作为 Python 基环境目录(uv 创建的 .venv 位于项目根,
# 其 home 指向 uv 托管的 CPython 安装目录,不能靠父目录推导)
$pyvenvCfgSource = Join-Path $venvSource 'pyvenv.cfg'
if (-not (Test-Path $pyvenvCfgSource)) { throw '项目 .venv 缺少 pyvenv.cfg,无法定位 Python 基环境' }
$pyHomeSource = (Get-Content $pyvenvCfgSource -Encoding UTF8 | Where-Object { $_ -like 'home = *' } | Select-Object -First 1) -replace '^home = ', ''
if (-not (Test-Path (Join-Path $pyHomeSource 'python.exe'))) { throw "Python 基环境不存在: $pyHomeSource" }
$targetPy = Join-Path $runtimeDir 'python'
New-Item -ItemType Directory -Force -Path $targetPy | Out-Null
# 拷贝解释器:将 .venv 的基环境(uv 托管 CPython)整体拷入 runtime/python,
# 便携包内 venv 的 pyvenv.cfg 将指向此目录,无需依赖开发机环境
Copy-Item -Recurse -Force (Join-Path $pyHomeSource '*') (Join-Path $targetPy '')
$targetVenv = Join-Path $runtimeDir 'venv'
Copy-Item -Recurse -Force (Join-Path $venvSource '*') $targetVenv
# 修 pyvenv.cfg 指向安装目录内的解释器
$pyvenvCfg = Join-Path $targetVenv 'pyvenv.cfg'
if (Test-Path $pyvenvCfg) {
    $cfg = Get-Content $pyvenvCfg
    $cfg = $cfg | ForEach-Object {
        if ($_ -like 'home = *') { 'home = C:\Program Files\ResearchAssetHub\runtime\python' }
        else { $_ }
    }
    $cfg | Set-Content $pyvenvCfg
    Write-Host '  已修正 venv pyvenv.cfg'
}

Write-Host '[3/5] 组装便携 Node + gitnexus'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'node') | Out-Null
Expand-Archive -Force $NodeZip (Join-Path $runtimeDir 'node')
$nodeRoot = Get-ChildItem (Join-Path $runtimeDir 'node') -Directory | Select-Object -First 1
if ($nodeRoot) {
    Get-ChildItem $nodeRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'node') -Force
    Remove-Item $nodeRoot.FullName -Force
}
# gitnexus 包整体拷贝(含其 node_modules 与 shim)
# 必须用开发机 PATH 上的 npm 定位全局包:刚解压的便携 node 无全局安装记录
$devNpm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $devNpm) { throw '开发机未找到 npm(需安装 Node.js 用于定位全局 gitnexus)' }
$devNpmRoot = (& $devNpm.Source root -g).Trim()   # npm root -g 返回全局 node_modules 目录本身
$gitnexusDir = Join-Path $runtimeDir 'gitnexus'
New-Item -ItemType Directory -Force -Path $gitnexusDir | Out-Null
$gitnexusPkg = Join-Path $devNpmRoot 'gitnexus'
if (-not (Test-Path $gitnexusPkg)) {
    # 兜底:个别 npm 包装实现返回 prefix 而非 node_modules 目录,
    # 此时直接在其下拼接 node_modules\gitnexus(prefix 本身不剥层)
    $gitnexusPkg = Join-Path $devNpmRoot 'node_modules\gitnexus'
}
if (-not (Test-Path $gitnexusPkg)) {
    Write-Host '  未找到全局 gitnexus,请在开发机 `npm install -g gitnexus` 后重跑'
    throw 'gitnexus 全局包缺失'
}
Copy-Item -Recurse -Force $gitnexusPkg (Join-Path $gitnexusDir 'pkg')
# 生成 ps1 shim(镜像 npm 生成的 shim:node 执行包入口)
$shim = @'
# gitnexus shim (portable)
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
$root = $PSScriptRoot
$node = Join-Path $root '..\node\node.exe'
$entry = Join-Path $root 'pkg\dist\cli\index.js'
if (-not (Test-Path $entry)) { $entry = Join-Path $root 'pkg\dist\cli.js' }
if (-not (Test-Path $entry)) { $entry = Join-Path $root 'pkg\bin\gitnexus.js' }
if (-not (Test-Path $entry)) { throw "gitnexus entry not found under $root" }
& $node $entry @Args
exit $LASTEXITCODE
'@
$shim | Set-Content (Join-Path $gitnexusDir 'gitnexus.ps1')
Write-Host '  gitnexus shim 已生成(入口按 pkg\dist\cli\index.js 依序探测)'

Write-Host '[4/5] 组装便携 PostgreSQL'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'postgres') | Out-Null
Expand-Archive -Force $PostgresZip (Join-Path $runtimeDir 'postgres')
$pgRoot = Get-ChildItem (Join-Path $runtimeDir 'postgres') -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'bin\pg_ctl.exe') } | Select-Object -First 1
if (-not $pgRoot) { throw 'PostgreSQL zip 中未找到 bin\pg_ctl.exe' }
Get-ChildItem $pgRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'postgres') -Force
Remove-Item $pgRoot.FullName -Force

Write-Host '[5/5] 组装 Meilisearch 与 app 源码'
Copy-Item -Force $MeiliExe (Join-Path $runtimeDir 'meilisearch.exe')
foreach ($item in @('apps', 'packages', 'migrations', 'config')) {
    Copy-Item -Recurse -Force (Join-Path $projectRoot $item) (Join-Path $appDir $item)
}

Write-Host "stage 完成: $stage"
