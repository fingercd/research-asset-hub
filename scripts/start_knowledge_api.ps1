[CmdletBinding()]
param([switch]$Restart)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$apiUrl = 'http://127.0.0.1:8877/api/v1/knowledge/status'
$statePath = Join-Path $projectRoot 'work\state\knowledge-api.json'
if ($Restart -and (Test-Path -LiteralPath $statePath)) {
    $record = Get-Content -LiteralPath $statePath -Encoding UTF8 -Raw | ConvertFrom-Json
    $owners = @(Get-NetTCPConnection -LocalPort 8877 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($owner in $owners) {
        $running = Get-CimInstance Win32_Process -Filter "ProcessId = $owner"
        if ($running.CommandLine -match 'uvicorn apps.api.main:app' -and
            $running.CommandLine -match '--port 8877' -and
            ($running.ParentProcessId -eq $record.pid -or $running.ProcessId -eq $record.pid)) {
            Stop-Process -Id $owner
        } else {
            throw 'Port 8877 ownership differs; the existing process was left running.'
        }
    }
}
try {
    $status = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 5 -NoProxy
    if ($status.read_only -eq $true) {
        Write-Output 'Knowledge API is ready at http://127.0.0.1:8877'
        exit 0
    }
} catch {
    # A missing service is expected before the first start.
}
if (Get-NetTCPConnection -LocalPort 8877 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8877 is occupied. Existing processes were left running.'
}
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Project Python environment is missing. Install project dependencies first.'
}
$logDirectory = Join-Path $projectRoot 'work\logs'
$stateDirectory = Join-Path $projectRoot 'work\state'
New-Item -ItemType Directory -Path $logDirectory, $stateDirectory -Force | Out-Null
$previousPort = $env:HUB_API_PORT
$previousMeiliKey = $env:HUB_MEILISEARCH_API_KEY
try {
    $env:HUB_API_PORT = '8877'
    if ([string]::IsNullOrWhiteSpace($previousMeiliKey)) {
        $dataRoot = if ($env:HUB_DATA_DIR) { $env:HUB_DATA_DIR } else {
            Join-Path $env:USERPROFILE 'Documents\ResearchAssetHub'
        }
        $keyPath = Join-Path $dataRoot 'secrets\meili-master-key'
        if (Test-Path -LiteralPath $keyPath) {
            $env:HUB_MEILISEARCH_API_KEY = (Get-Content -LiteralPath $keyPath -Encoding UTF8 -Raw).Trim()
        }
    }
    $startArguments = @{
        FilePath = $pythonPath
        ArgumentList = @('-m', 'uvicorn', 'apps.api.main:app', '--host', '127.0.0.1', '--port', '8877')
        WorkingDirectory = $projectRoot
        WindowStyle = 'Hidden'
        PassThru = $true
        RedirectStandardOutput = (Join-Path $logDirectory 'knowledge-api.stdout.log')
        RedirectStandardError = (Join-Path $logDirectory 'knowledge-api.stderr.log')
    }
    $process = Start-Process @startArguments
} finally {
    $env:HUB_API_PORT = $previousPort
    $env:HUB_MEILISEARCH_API_KEY = $previousMeiliKey
}
@{
    pid = $process.Id
    started_at_utc = $process.StartTime.ToUniversalTime().ToString('o')
    executable = $pythonPath
    url = 'http://127.0.0.1:8877'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $stateDirectory 'knowledge-api.json') -Encoding UTF8
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Seconds 1
    try {
        $status = Invoke-RestMethod -Uri $apiUrl -TimeoutSec 5 -NoProxy
        if ($status.read_only -eq $true) {
            Write-Output 'Knowledge API is ready at http://127.0.0.1:8877'
            exit 0
        }
    } catch {
        if ($process.HasExited) {
            throw 'Knowledge API exited. Inspect work/logs/knowledge-api.stderr.log.'
        }
    }
}
throw 'Knowledge API startup is still unverified. Inspect its log before restarting.'
