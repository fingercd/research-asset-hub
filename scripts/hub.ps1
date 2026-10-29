[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'inventory', 'ingest', 'validate', 'status')]
    [string]$Command = 'status',
    [switch]$All,
    [switch]$DryRun,
    [switch]$Json,
    [string]$Source
)

# Source-maintenance sidecar only. The native desktop owns all service lifecycle.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project Python is missing. Run scripts/bootstrap.ps1 first.'
}
$arguments = @('-m', 'packages.cli.main', $Command)
if ($All) { $arguments += '--all' }
if ($DryRun) { $arguments += '--dry-run' }
if ($Json) { $arguments += '--json' }
if ($Source) { $arguments += @('--source', $Source) }
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath @arguments
    exit $LASTEXITCODE
}
finally { Pop-Location }
