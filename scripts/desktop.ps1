[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('build', 'run', 'plan')]
    [string]$Command = 'build',
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$RuntimeRoot,
    [string]$DataDir,
    [ValidateRange(1, 65535)][int]$ApiPort = 18767,
    [ValidateRange(1, 65535)][int]$GatewayPort = 18766,
    [ValidateRange(1, 65535)][int]$PostgresPort = 15432,
    [ValidateRange(1, 65535)][int]$MeiliPort = 17700
)

# One native development path: static React assets + custom-protocol debug shell.
# plan is read-only; run deliberately builds first and then starts the native app.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Resolve-AbsoluteDirectory([string]$Value, [string]$Name) {
    if (-not [IO.Path]::IsPathFullyQualified($Value)) { throw "$Name must be an absolute path." }
    if (-not (Test-Path -LiteralPath $Value -PathType Container)) { throw "$Name must be an existing directory." }
    return (Resolve-Path -LiteralPath $Value -ErrorAction Stop).ProviderPath
}
$SourceRoot = Resolve-AbsoluteDirectory $SourceRoot 'SourceRoot'
$manifest = Join-Path $SourceRoot 'desktop\src-tauri\Cargo.toml'
if (-not (Test-Path -LiteralPath $manifest -PathType Leaf)) { throw 'SourceRoot is not a Hub checkout.' }
$launchArguments = @()
$developmentPorts = [ordered]@{
    HUB_API_PORT = [string]$ApiPort
    HUB_ASSET_GATEWAY_PORT = [string]$GatewayPort
    HUB_POSTGRES_PORT = [string]$PostgresPort
    HUB_MEILISEARCH_PORT = [string]$MeiliPort
}
if (@($developmentPorts.Values | Sort-Object -Unique).Count -ne 4) {
    throw 'API, gateway, PostgreSQL and Meilisearch ports must be distinct.'
}
# These settings otherwise allow a development data directory to reuse production services.
$isolatedEnvironment = @(
    'HUB_DATABASE_URL', 'DATABASE_URL', 'HUB_MEILISEARCH_URL', 'HUB_MEILISEARCH_API_KEY',
    'HUB_GATEWAY_SECRET', 'HUB_POSTGRES_PASSWORD', 'HUB_SOURCES_CONFIG_PATH',
    'HUB_TAXONOMY_CONFIG_PATH', 'HUB_DOMAIN_PORTAL_CONFIG_PATH',
    'HUB_WORKER_HEARTBEAT_PATH', 'HUB_HOST_WORKSPACE_ROOT', 'HUB_DATA_DIR',
    'HUB_API_ORIGIN', 'HUB_GATEWAY_ORIGIN', 'HUB_MCP_API_URL'
)
if ($Command -in @('run', 'plan')) {
    if (-not $RuntimeRoot -or -not $DataDir) { throw 'run/plan require explicit RuntimeRoot and DataDir.' }
    $RuntimeRoot = Resolve-AbsoluteDirectory $RuntimeRoot 'RuntimeRoot'
    if (-not [IO.Path]::IsPathFullyQualified($DataDir)) { throw 'DataDir must be an absolute path.' }
    $DataDir = [IO.Path]::GetFullPath($DataDir)
    foreach ($relative in @('postgres\bin\initdb.exe', 'postgres\bin\pg_ctl.exe',
        'postgres\bin\pg_isready.exe', 'postgres\bin\psql.exe', 'meilisearch.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $RuntimeRoot $relative) -PathType Leaf)) {
            throw "RuntimeRoot is missing $relative"
        }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $SourceRoot '.venv\Scripts\python.exe') -PathType Leaf)) {
        throw 'SourceRoot is missing .venv/Scripts/python.exe; run bootstrap first.'
    }
    $launchArguments = @('--dev-source-root', $SourceRoot, '--dev-runtime-root', $RuntimeRoot, '--data-dir', $DataDir)
}
$buildArguments = @('build', '--manifest-path', $manifest, '--features', 'custom-protocol')
if ($Command -eq 'plan') {
    [ordered]@{
        frontend = @('pnpm', '--filter', '@research-asset-hub/desktop-ui', 'build')
        native = @('cargo') + $buildArguments
        executable = 'research-asset-hub-desktop.exe'
        arguments = $launchArguments
        environment = $developmentPorts
        cleared_environment = $isolatedEnvironment
        tauri_config_override = @{ bundle = @{ resources = $null } }
        starts_services = $false
    } | ConvertTo-Json -Depth 4
    return
}
$previousEnvironment = @{}
foreach ($name in @('TAURI_CONFIG') + @($developmentPorts.Keys) + $isolatedEnvironment) {
    $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name)
}
Push-Location -LiteralPath $SourceRoot
try {
    # Development has explicit external runtime inputs, not a staged installer payload.
    $tauriConfig = if ($env:TAURI_CONFIG) { ConvertFrom-Json -InputObject $env:TAURI_CONFIG -AsHashtable } else { @{} }
    if (-not $tauriConfig.ContainsKey('bundle')) { $tauriConfig['bundle'] = @{} }
    $tauriConfig['bundle']['resources'] = $null
    $env:TAURI_CONFIG = ConvertTo-Json -InputObject $tauriConfig -Depth 20 -Compress
    & pnpm --filter @research-asset-hub/desktop-ui build
    if ($LASTEXITCODE -ne 0) { throw 'Static frontend build failed.' }
    & cargo @buildArguments
    if ($LASTEXITCODE -ne 0) { throw 'Native debug build failed.' }
    if ($Command -eq 'run') {
        # metadata respects Cargo target-dir configuration; never locate an installed app.
        $metadataJson = & cargo metadata --manifest-path $manifest --no-deps --format-version 1
        if ($LASTEXITCODE -ne 0) { throw 'Could not locate the Cargo target directory.' }
        $targetDirectory = ($metadataJson | ConvertFrom-Json).target_directory
        $executable = Join-Path $targetDirectory 'debug\research-asset-hub-desktop.exe'
        foreach ($name in $isolatedEnvironment) { [Environment]::SetEnvironmentVariable($name, $null) }
        foreach ($name in $developmentPorts.Keys) { [Environment]::SetEnvironmentVariable($name, $developmentPorts[$name]) }
        & $executable @launchArguments
        exit $LASTEXITCODE
    }
}
finally {
    foreach ($name in $previousEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name])
    }
    Pop-Location
}
