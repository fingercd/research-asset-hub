[CmdletBinding()]
param(
    [ValidateSet('Inspect', 'Apply', 'Rollback')][string]$Mode = 'Inspect',
    [string]$PackageDir = $PSScriptRoot,
    [string]$BackupDir
)

$ErrorActionPreference = 'Stop'
$install = 'C:\Program Files\ResearchAssetHub'
$package = (Resolve-Path -LiteralPath $PackageDir).ProviderPath
$manifestFile = Join-Path $package 'canonical-update-manifest.json'
$manifest = Get-Content -LiteralPath $manifestFile -Raw -Encoding UTF8 | ConvertFrom-Json
if ([IO.Path]::GetFullPath($manifest.installation) -ne $install) {
    throw 'Only the canonical Program Files installation is supported.'
}
$payload = Join-Path $package 'payload'

function Get-HashOrNull([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    return $null
}

function Resolve-UpdateTarget([string]$Relative) {
    $parts = $Relative -split '[/\\]'
    if (($parts | Where-Object { $_ -in @('', '.', '..') }).Count -gt 0 -or
        [IO.Path]::IsPathRooted($Relative) -or $Relative.Contains(':')) {
        throw "Unsafe manifest path: $Relative"
    }
    if ($Relative -ne 'research-asset-hub-desktop.exe' -and
        $Relative -notmatch '^app/(apps|packages|migrations)/.+\.(py|mako)$' -and
        $Relative -notmatch '^runtime/python/Lib/site-packages/.+') {
        throw "Path outside the reviewed update scope: $Relative"
    }
    $absolute = [IO.Path]::GetFullPath((Join-Path $install $Relative))
    if (-not $absolute.StartsWith($install + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Target escapes canonical installation: $Relative"
    }
    $cursor = Split-Path -Parent $absolute
    while ($cursor.Length -ge $install.Length) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Update target uses a reparse point: $cursor"
            }
        }
        if ($cursor -eq $install) { break }
        $cursor = Split-Path -Parent $cursor
    }
    if (Test-Path -LiteralPath $absolute) {
        if ((Get-Item -LiteralPath $absolute -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Update target is a reparse point: $absolute"
        }
    }
    return $absolute
}

function Assert-ConfigPreserved {
    foreach ($property in $manifest.protected_config.PSObject.Properties) {
        $actual = Get-HashOrNull (Join-Path $install $property.Name)
        if ($actual -ne $property.Value) { throw "Protected config changed: $($property.Name)" }
    }
}

function Get-HubProcesses {
    @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -in @('research-asset-hub-desktop.exe', 'postgres.exe', 'meilisearch.exe', 'pg_ctl.exe') -or
        ($_.Name -in @('python.exe', 'pythonw.exe') -and (
            $_.ExecutablePath -like ($install + '\*') -or
            $_.CommandLine -match 'apps\.(desktop_services|worker_start|api\.main|worker\.main|asset_gateway\.main|migrate)'
        ))
    } | Select-Object ProcessId, Name)
}

function Assert-Quiet {
    $running = @(Get-HubProcesses)
    if ($running.Count -gt 0) {
        throw ('Stop Hub gracefully before copying; the updater never kills processes: ' +
            ($running | ConvertTo-Json -Compress))
    }
    foreach ($port in @(8766, 8767, 5432, 7700)) {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $pending = $client.BeginConnect('127.0.0.1', $port, $null, $null)
            if ($pending.AsyncWaitHandle.WaitOne(300)) {
                try { $client.EndConnect($pending) } catch { }
                if ($client.Connected) { throw "Service still listens on port $port; refusing copy." }
            }
        } finally { $client.Dispose() }
    }
}

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Apply/Rollback requires an already elevated PowerShell. This script does not invoke UAC.'
    }
}

foreach ($file in $manifest.files) {
    $target = Resolve-UpdateTarget $file.path
    if ((Get-HashOrNull (Join-Path $payload $file.path)) -ne $file.after_sha256) {
        throw "Payload hash mismatch: $($file.path)"
    }
    if ($Mode -ne 'Rollback' -and (Get-HashOrNull $target) -ne $file.before_sha256) {
        throw "Installed file differs from reviewed baseline: $($file.path)"
    }
}
Assert-ConfigPreserved
if ($Mode -eq 'Inspect') {
    [PSCustomObject]@{
        Status = 'payload-and-installed-baseline-verified'
        Installation = $install
        Files = @($manifest.files).Count
        Running = @(Get-HubProcesses)
        ManifestSha256 = Get-HashOrNull $manifestFile
    } | ConvertTo-Json -Depth 5
    exit 0
}
Assert-Administrator
Assert-Quiet

if ($Mode -eq 'Rollback') {
    if ([string]::IsNullOrWhiteSpace($BackupDir)) { throw 'Rollback requires -BackupDir.' }
    $backup = (Resolve-Path -LiteralPath $BackupDir).ProviderPath
    $state = Get-Content -LiteralPath (Join-Path $backup 'canonical-update-backup.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.ManifestSha256 -ne (Get-HashOrNull $manifestFile)) { throw 'Backup belongs to a different manifest.' }
    foreach ($file in $manifest.files) {
        $target = Resolve-UpdateTarget $file.path
        $current = Get-HashOrNull $target
        if ($current -ne $file.before_sha256 -and $current -ne $file.after_sha256) {
            throw "File changed after deployment; preserve it for review: $($file.path)"
        }
        if ($null -ne $file.before_sha256) {
            if ((Get-HashOrNull (Join-Path $backup $file.path)) -ne $file.before_sha256) {
                throw "Backup hash mismatch: $($file.path)"
            }
        }
    }
    Assert-Quiet
    foreach ($file in $manifest.files) {
        $target = Resolve-UpdateTarget $file.path
        if ($null -eq $file.before_sha256) {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        } else { Copy-Item -LiteralPath (Join-Path $backup $file.path) -Destination $target -Force }
        if ((Get-HashOrNull $target) -ne $file.before_sha256) { throw "Rollback verification failed: $($file.path)" }
    }
    Assert-ConfigPreserved
    [PSCustomObject]@{ Status = 'rolled-back'; BackupDir = $backup } | ConvertTo-Json
    exit 0
}

if ([string]::IsNullOrWhiteSpace($BackupDir)) {
    $BackupDir = Join-Path $package ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
}
$backup = [IO.Path]::GetFullPath($BackupDir)
if ($backup.StartsWith($install, [StringComparison]::OrdinalIgnoreCase) -or
    $backup -eq $package -or $backup.StartsWith($payload, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Backup must be outside installation and payload.'
}
if (Test-Path -LiteralPath $backup) { throw 'Use a new backup directory.' }
New-Item -ItemType Directory -Path $backup | Out-Null
foreach ($file in $manifest.files) {
    if ($null -ne $file.before_sha256) {
        $old = Resolve-UpdateTarget $file.path
        $copy = Join-Path $backup $file.path
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $copy) | Out-Null
        Copy-Item -LiteralPath $old -Destination $copy
        if ((Get-HashOrNull $copy) -ne $file.before_sha256) { throw "Backup verification failed: $($file.path)" }
    }
}
[PSCustomObject]@{
    ManifestSha256 = Get-HashOrNull $manifestFile
    Installation = $install
    Created = (Get-Date).ToUniversalTime().ToString('o')
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $backup 'canonical-update-backup.json') -Encoding UTF8
Assert-Quiet
$changed = New-Object System.Collections.Generic.List[object]
try {
    foreach ($file in $manifest.files) {
        $target = Resolve-UpdateTarget $file.path
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        $changed.Add($file)
        Copy-Item -LiteralPath (Join-Path $payload $file.path) -Destination $target -Force
        if ((Get-HashOrNull $target) -ne $file.after_sha256) { throw "Installed hash mismatch: $($file.path)" }
    }
    Assert-ConfigPreserved
} catch {
    $failure = $_
    # Roll back only files this transaction has touched. No directories/data are deleted.
    foreach ($file in $changed) {
        $target = Resolve-UpdateTarget $file.path
        if ($null -eq $file.before_sha256) {
            if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
        } else { Copy-Item -LiteralPath (Join-Path $backup $file.path) -Destination $target -Force }
        if ((Get-HashOrNull $target) -ne $file.before_sha256) { throw "Automatic rollback failed: $($file.path); backup: $backup" }
    }
    throw $failure
}
$result = [PSCustomObject]@{
    Status = 'installed-files-verified'
    Installation = $install
    BackupDir = $backup
    ManifestSha256 = Get-HashOrNull $manifestFile
    Files = @($manifest.files).Count
    ReleaseSha256 = Get-HashOrNull (Join-Path $install 'research-asset-hub-desktop.exe')
    ServicesRestarted = $false
}
$result | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $package 'canonical-update-applied.json') -Encoding UTF8
$result | ConvertTo-Json
