[CmdletBinding()]
param(
    [switch]$SkipPython,
    [switch]$SkipWeb,
    [switch]$StartInfrastructure
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$projectRoot = Split-Path -Parent $PSScriptRoot

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Command,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $Command $($Arguments -join ' ')"
    }
}

Push-Location -LiteralPath $projectRoot
try {
    Write-Host "Research Asset Hub bootstrap" -ForegroundColor Cyan
    Write-Host "Project: $projectRoot"

    if (-not $SkipPython) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pythonCommand) {
            throw "Python 3.11 or newer is required but 'python' is not available."
        }

        $pythonVersionText = & $pythonCommand.Source -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
        if ($LASTEXITCODE -ne 0) {
            throw "Unable to determine the Python version."
        }
        $pythonVersion = [version]$pythonVersionText.Trim()
        if ($pythonVersion -lt [version]"3.11") {
            throw "Python 3.11 or newer is required; found $pythonVersion."
        }
        Write-Host "Python $pythonVersion"

        $uvCommand = Get-Command uv -ErrorAction SilentlyContinue
        if ($uvCommand) {
            Invoke-CheckedCommand -Command $uvCommand.Source -Arguments @(
                "sync",
                "--frozen",
                "--extra",
                "dev"
            )
        }
        else {
            Write-Warning "uv was not found; using the standard venv + pip fallback."
            $venvPython = Join-Path $projectRoot ".venv\Scripts\python.exe"
            if (-not (Test-Path -LiteralPath $venvPython)) {
                Invoke-CheckedCommand -Command $pythonCommand.Source -Arguments @("-m", "venv", ".venv")
            }
            Invoke-CheckedCommand -Command $venvPython -Arguments @("-m", "pip", "install", "--upgrade", "pip")
            Invoke-CheckedCommand -Command $venvPython -Arguments @("-m", "pip", "install", "-e", ".[dev]")
        }
    }

    if (-not $SkipWeb) {
        $pnpmCommand = Get-Command pnpm -ErrorAction SilentlyContinue
        if ($pnpmCommand) {
            Invoke-CheckedCommand -Command $pnpmCommand.Source -Arguments @(
                "install",
                "--frozen-lockfile"
            )
        }
        else {
            $corepackCommand = Get-Command corepack -ErrorAction SilentlyContinue
            if (-not $corepackCommand) {
                throw "pnpm 11 or Corepack is required to install the web application."
            }
            Invoke-CheckedCommand -Command $corepackCommand.Source -Arguments @(
                "pnpm",
                "install",
                "--frozen-lockfile"
            )
        }
    }

    $dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
    if ($dockerCommand) {
        Invoke-CheckedCommand -Command $dockerCommand.Source -Arguments @(
            "compose",
            "-f",
            "deploy/compose.yaml",
            "config",
            "--quiet"
        )
        if ($StartInfrastructure) {
            Invoke-CheckedCommand -Command $dockerCommand.Source -Arguments @(
                "compose",
                "-f",
                "deploy/compose.yaml",
                "up",
                "-d"
            )
        }
    }
    elseif ($StartInfrastructure) {
        throw "Docker is required when -StartInfrastructure is supplied."
    }
    else {
        Write-Warning "Docker was not found; Compose validation was skipped."
    }

    Write-Host ""
    Write-Host "Bootstrap completed." -ForegroundColor Green
    Write-Host "Start all local services with: .\scripts\hub.ps1 start"
    Write-Host "Open the platform at:          http://127.0.0.1:8765"
}
finally {
    Pop-Location
}
