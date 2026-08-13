[CmdletBinding()]
param(
    [string]$PostgresZip,      # EDB PostgreSQL 16 Windows zip binaries 路径(必填,一次性下载)
    [string]$MeiliExe,         # meilisearch-windows-amd64.exe 路径(必填,一次性下载)
    [string]$NodeZip           # node-v20.x-win-x64.zip 路径(必填,一次性下载)
)

$ErrorActionPreference = 'Stop'
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
$pyHomeSource = Split-Path -Parent $venvSource   # 系统 python 根(如 anaconda3/envs/...)
$targetPy = Join-Path $runtimeDir 'python'
New-Item -ItemType Directory -Force -Path $targetPy | Out-Null
# 拷贝解释器:若项目 .venv 基于系统级 python,则拷贝其基环境核心文件
# 简化路径:使用 python.org embeddable 不可行(缺依赖),改用完整基环境拷贝
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
# 启动脚本:安装目录内的 python 需以其自身为解释器
Copy-Item (Join-Path $targetPy 'python.exe') (Join-Path $runtimeDir 'python\python-real.exe') -ErrorAction SilentlyContinue

Write-Host '[3/5] 组装便携 Node + gitnexus'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'node') | Out-Null
Expand-Archive -Force $NodeZip (Join-Path $runtimeDir 'node')
$nodeRoot = Get-ChildItem (Join-Path $runtimeDir 'node') -Directory | Select-Object -First 1
if ($nodeRoot) {
    Get-ChildItem $nodeRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'node') -Force
    Remove-Item $nodeRoot.FullName -Force
}
# gitnexus 包整体拷贝(含其 node_modules 与 shim)
$gitnexusGlobal = & (Join-Path $runtimeDir 'node\node.exe') (Join-Path $runtimeDir 'node\node_modules\npm\bin\npm-cli.js') root -g 2>$null | Out-Null
$gitnexusDir = Join-Path $runtimeDir 'gitnexus'
New-Item -ItemType Directory -Force -Path $gitnexusDir | Out-Null
$gitnexusPkg = Join-Path (Split-Path (& (Join-Path $runtimeDir 'node\node.exe') (Join-Path $runtimeDir 'node\node_modules\npm\bin\npm-cli.js') root -g)) 'node_modules\gitnexus'
if (-not (Test-Path $gitnexusPkg)) {
    Write-Host '  未找到全局 gitnexus,请在开发机 `npm install -g gitnexus` 后重跑'
    throw 'gitnexus 全局包缺失'
}
Copy-Item -Recurse -Force $gitnexusPkg (Join-Path $gitnexusDir 'pkg')
# 生成 ps1 shim(镜像 npm 生成的 shim:node 执行包入口)
$shim = @'
# gitnexus shim (portable)
param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Args)
$root = Split-Path -Parent $PSScriptRoot
$node = Join-Path $root '..\node\node.exe'
$entry = Join-Path $root 'pkg\bin\gitnexus.js'
if (-not (Test-Path $entry)) { $entry = Join-Path $root 'pkg\dist\cli.js' }
if (-not (Test-Path $entry)) { throw "gitnexus entry not found under $root" }
& $node $entry @Args
exit $LASTEXITCODE
'@
$shim | Set-Content (Join-Path $gitnexusDir 'gitnexus.ps1')
Write-Host '  gitnexus shim 已生成(入口文件名按包实际结构核对)'

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
