[CmdletBinding()]
param(
    [string]$PostgresZip,      # EDB PostgreSQL 16 Windows zip binaries 路径(必填,一次性下载)
    [string]$MeiliExe,         # meilisearch-windows-amd64.exe 路径(必填,一次性下载)
    [string]$SourceWorkspaceRoot # sources.yaml workspace_root 的宿主覆盖;缺省为项目根
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $PostgresZip)) { throw "PostgresZip 路径不存在: $PostgresZip" }
if (-not (Test-Path $MeiliExe)) { throw "MeiliExe 路径不存在: $MeiliExe" }
$desktopRoot = Split-Path -Parent $PSScriptRoot          # desktop/
$projectRoot = Split-Path -Parent $desktopRoot
if ([string]::IsNullOrWhiteSpace($SourceWorkspaceRoot)) {
    $SourceWorkspaceRoot = $projectRoot
}
if (-not (Test-Path -LiteralPath $SourceWorkspaceRoot -PathType Container)) {
    throw "SourceWorkspaceRoot 目录不存在: $SourceWorkspaceRoot"
}
$SourceWorkspaceRoot = (Resolve-Path -LiteralPath $SourceWorkspaceRoot).ProviderPath
$stage = Join-Path $desktopRoot 'stage'
$runtimeDir = Join-Path $stage 'runtime'
$appDir = Join-Path $stage 'app'

Write-Host '[1/4] 清理并创建 stage 目录'
Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $runtimeDir, $appDir | Out-Null

Write-Host '[2/4] 组装便携 Python(基环境 + 项目依赖合并)'
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
# 拷贝基环境(uv 托管 CPython)作为便携解释器
Copy-Item -Recurse -Force (Join-Path $pyHomeSource '*') (Join-Path $targetPy '')
# 把 .venv 的 site-packages 合并进便携解释器,使其自带全部项目依赖:
# 壳直接用 runtime\python\python.exe 运行 -m apps.*,无 venv 概念
$venvSitePackages = Join-Path $venvSource 'Lib\site-packages'
$targetSitePackages = Join-Path $targetPy 'Lib\site-packages'
if (-not (Test-Path $venvSitePackages)) { throw "未找到 .venv site-packages: $venvSitePackages" }
New-Item -ItemType Directory -Force -Path $targetSitePackages | Out-Null
Copy-Item -Recurse -Force (Join-Path $venvSitePackages '*') (Join-Path $targetSitePackages '')
# 项目源码会单独复制到 stage/app。开发 venv 中的 editable 安装元数据既无必要，
# 又包含开发机绝对路径和构建时 README 快照，不能进入可分发运行时。
Get-ChildItem -LiteralPath $targetSitePackages -Force -ErrorAction SilentlyContinue |
    Where-Object {
        $_.Name -like 'research_asset_hub-*.dist-info' -or
        $_.Name -like '_editable_impl_research_asset_hub*'
    } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
# 删减:ML/科研类死重包(apps/ 与 packages/ 零导入),否则 NSIS 32 位 makensis 因输入超 2GB 失败。
# 前缀匹配一并带走 dist-info / 生态残留(functorch/torchgen/sentence_transformers/scikit_learn 等)
Get-ChildItem $targetSitePackages -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^(torch|torchvision|torchaudio|torchdata|scipy|scikit_learn|numpy|pandas|sklearn|sympy|transformers|sentence_transformers|functorch|torchgen)' } |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Force (Join-Path $targetSitePackages 'isympy.py') -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Force -Directory $targetPy -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

Write-Host '[3/4] 组装便携 PostgreSQL'
New-Item -ItemType Directory -Force -Path (Join-Path $runtimeDir 'postgres') | Out-Null
Expand-Archive -Force $PostgresZip (Join-Path $runtimeDir 'postgres')
$pgRoot = Get-ChildItem (Join-Path $runtimeDir 'postgres') -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'bin\pg_ctl.exe') } | Select-Object -First 1
if (-not $pgRoot) { throw 'PostgreSQL zip 中未找到 bin\pg_ctl.exe' }
Get-ChildItem $pgRoot.FullName | Move-Item -Destination (Join-Path $runtimeDir 'postgres') -Force
Remove-Item $pgRoot.FullName -Force
# 删减:pgAdmin 4(图形管理工具,桌面版用不上)/ doc / StackBuilder
foreach ($dead in @('pgAdmin 4', 'doc', 'StackBuilder')) {
    Remove-Item -Recurse -Force (Join-Path $runtimeDir "postgres\$dead") -ErrorAction SilentlyContinue
}

Write-Host '[4/4] 构建前端并组装 Meilisearch 与 app 源码'
# 安装包复制的是 desktop/stage/app 下的前端产物；必须在复制 apps 前构建，不能依赖
# cargo tauri build 的 beforeBuild（它发生在 stage 完成之后）。
& pnpm --filter @research-asset-hub/web build
if ($LASTEXITCODE -ne 0) {
    throw 'Web 前端构建失败，未继续复制 apps 到 stage'
}
Copy-Item -Force $MeiliExe (Join-Path $runtimeDir 'meilisearch.exe')
foreach ($item in @('apps', 'packages', 'migrations', 'config')) {
    Copy-Item -Recurse -Force (Join-Path $projectRoot $item) (Join-Path $appDir $item)
}
# `workspace_root` 是 sources.yaml 中所有 source.root 的解析基准，不是资料
# 目录本身。以源项目 config 按其默认 workspace_root 解析的完整根映射为冻结参照，
# 再比较安装版经 SourceWorkspaceRoot 覆盖后的映射，防止把上一级“核心领域”误写进
# hint 后令每个 ../ 路径再多退一级。历史上已缺失的来源只作告警，不能据此误判根。
$stagedPython = Join-Path $targetPy 'python.exe'
$rootValidation = @'
import os
import sys
from pathlib import Path

app_dir = Path(sys.argv[1]).resolve()
workspace_root = Path(sys.argv[2]).resolve()
project_root = Path(sys.argv[3]).resolve()
sys.path.insert(0, str(app_dir))

from apps.asset_gateway.registry import SourceRegistry


def root_paths(registry):
    return {
        (item.source_id, item.root_key): item.path
        for item in registry.describe()
    }


# The reference must use sources.yaml's own workspace_root, even if the staging
# shell inherited a host override. The staged registry is the only one that uses
# the requested override.
os.environ.pop("HUB_HOST_WORKSPACE_ROOT", None)
reference = SourceRegistry.load(project_root / "config" / "sources.yaml")
# Capture the reference before switching the environment: describe() hot-refreshes
# when HUB_HOST_WORKSPACE_ROOT changes.
reference_paths = root_paths(reference)
os.environ["HUB_HOST_WORKSPACE_ROOT"] = str(workspace_root)
staged = SourceRegistry.load(app_dir / "config" / "sources.yaml")

staged_paths = root_paths(staged)
if staged_paths != reference_paths:
    print("  ERROR SourceWorkspaceRoot changes the registered source-root mapping")
    for key in sorted(set(reference_paths) | set(staged_paths)):
        expected = reference_paths.get(key)
        actual = staged_paths.get(key)
        if actual != expected:
            print(f"  MISMATCH {key[0]}/{key[1]}: expected={expected} actual={actual}")
    raise SystemExit(2)

status = staged.root_status()
print(
    "  source roots: "
    f"configured={status['configured']} verification={status['verification']} "
    f"source_files_verified={status['source_files_verified']} workspace_root={workspace_root}"
)
'@
& $stagedPython -c $rootValidation $appDir $SourceWorkspaceRoot $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "SourceWorkspaceRoot 改变了 sources.yaml 的注册根映射: $SourceWorkspaceRoot"
}
# 不改写 sources.yaml，也不把开发机绝对路径提交到仓库。提示仅存在于已忽略的
# desktop/stage/app/config 中，安装后由桌面壳验证目录存在再注入子进程环境。
$workspaceHint = Join-Path $appDir 'config\workspace-root.txt'
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($workspaceHint, $SourceWorkspaceRoot, $utf8NoBom)

# Precompile pure Python imports into one archive. Native extensions, templates
# and source metadata remain in the runtime's normal package directories.
& $stagedPython (Join-Path $projectRoot 'scripts\build_startup_archive.py') --app-dir $appDir
if ($LASTEXITCODE -ne 0) {
    throw 'Python startup archive build failed'
}

Write-Host "stage 完成: $stage"
