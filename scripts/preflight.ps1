[CmdletBinding()]
param(
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$pythonPath = if (Test-Path -LiteralPath $projectPython) {
    $projectPython
}
else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand) { $pythonCommand.Source } else { $null }
}

if (-not $pythonPath) {
    Write-Error 'Python was not found on PATH. Install Python 3.11+ before continuing.'
    exit 2
}

Push-Location $projectRoot
try {
    $arguments = @('-m', 'packages.cli.main', 'preflight')
    if ($Json) {
        $arguments += '--json'
    }
    & $pythonPath @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
