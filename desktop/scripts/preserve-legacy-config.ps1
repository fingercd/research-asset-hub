[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$LegacyConfig,
    [Parameter(Mandatory)][string]$SourceWorkspaceRoot,
    [Parameter(Mandatory)][string]$PythonExe,
    [string]$DataDir = $(if ($env:HUB_DATA_DIR) { $env:HUB_DATA_DIR } else { Join-Path $env:USERPROFILE 'Documents/ResearchAssetHub' }),
    [switch]$ValidateOnly
)
$ErrorActionPreference = 'Stop'
$LegacyConfig = (Resolve-Path -LiteralPath $LegacyConfig).ProviderPath
$SourceWorkspaceRoot = (Resolve-Path -LiteralPath $SourceWorkspaceRoot).ProviderPath
$destination = [IO.Path]::GetFullPath((Join-Path $DataDir 'config'))
if (-not (Test-Path -LiteralPath (Join-Path $LegacyConfig 'sources.yaml'))) { throw 'legacy sources.yaml missing' }
if (Test-Path -LiteralPath $destination) { throw 'Existing user config must be reused; refusing to overwrite it' }
# Validate against the original source workspace configuration before copying.
& $PythonExe (Join-Path $PSScriptRoot 'validate-source-mapping.py') `
    --reference (Join-Path $SourceWorkspaceRoot 'config/sources.yaml') `
    --candidate (Join-Path $LegacyConfig 'sources.yaml') --workspace-root $SourceWorkspaceRoot
if ($LASTEXITCODE -ne 0) { throw 'legacy source root mapping validation failed' }
if ($ValidateOnly) { Write-Host 'Legacy roots verified; no files copied'; return }
New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
Copy-Item -LiteralPath $LegacyConfig -Destination $destination -Recurse
$utf8 = New-Object System.Text.UTF8Encoding($false)
[IO.File]::WriteAllText((Join-Path $destination 'workspace-root.txt'), $SourceWorkspaceRoot, $utf8)
[IO.File]::WriteAllText((Join-Path $destination 'release-profile.txt'), 'personal', $utf8)
& $PythonExe (Join-Path $PSScriptRoot 'validate-source-mapping.py') `
    --reference (Join-Path $SourceWorkspaceRoot 'config/sources.yaml') `
    --candidate (Join-Path $destination 'sources.yaml') --workspace-root $SourceWorkspaceRoot
if ($LASTEXITCODE -ne 0) { throw 'Copied legacy roots failed validation; preserve original and inspect the new config' }
Write-Host "Legacy config preserved at $destination. Original files remain unchanged."
