[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'inventory', 'ingest', 'validate', 'status', 'start', 'stop')]
    [string]$Command = 'status',

    [switch]$All,
    [switch]$DryRun,
    [switch]$Json,
    [string]$Source
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
$stateDirectory = Join-Path $projectRoot 'work\state'
$logDirectory = Join-Path $projectRoot 'work\logs'
$processStatePath = Join-Path $stateDirectory 'processes.json'

if (-not $pythonPath) {
    Write-Error 'Python was not found on PATH.'
    exit 2
}

function Get-RecordedProcesses {
    if (-not (Test-Path -LiteralPath $processStatePath)) {
        return @()
    }
    try {
        $document = Get-Content -LiteralPath $processStatePath -Encoding UTF8 -Raw | ConvertFrom-Json
        return @($document.processes)
    }
    catch {
        Write-Warning "Could not read process state: $($_.Exception.Message)"
        return @()
    }
}

function Test-RecordedProcess {
    param([Parameter(Mandatory)]$Record)

    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if (-not $process) {
        return $false
    }
    try {
        $actual = $process.StartTime.ToUniversalTime()
        $recorded = [datetime]::Parse([string]$Record.started_at).ToUniversalTime()
        return [math]::Abs(($actual - $recorded).TotalSeconds) -lt 5
    }
    catch {
        return $false
    }
}

function Start-HubProcess {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [string]$WorkingDirectory = $projectRoot
    )

    $stdout = Join-Path $logDirectory "$Name.stdout.log"
    $stderr = Join-Path $logDirectory "$Name.stderr.log"
    $process = Start-Process `
        -FilePath $FilePath `
        -ArgumentList $ArgumentList `
        -WorkingDirectory $WorkingDirectory `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    return [pscustomobject]@{
        name = $Name
        pid = $process.Id
        started_at = $process.StartTime.ToUniversalTime().ToString('o')
        stdout = $stdout
        stderr = $stderr
    }
}

function Stop-HubProcessTree {
    param([Parameter(Mandatory)]$Record)

    if (-not (Test-RecordedProcess $Record)) {
        return $false
    }

    $rootPid = [int]$Record.pid
    $snapshot = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId)
    $pending = @($rootPid)
    $ordered = @()
    while ($pending.Count -gt 0) {
        $current = [int]$pending[0]
        $pending = @($pending | Select-Object -Skip 1)
        $ordered += $current
        $pending += @(
            $snapshot |
                Where-Object { [int]$_.ParentProcessId -eq $current } |
                ForEach-Object { [int]$_.ProcessId }
        )
    }

    [array]::Reverse($ordered)
    foreach ($processId in $ordered) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }
    return $true
}

if ($Command -eq 'start') {
    New-Item -ItemType Directory -Force -Path $stateDirectory, $logDirectory | Out-Null
    $alreadyRunning = @(Get-RecordedProcesses | Where-Object { Test-RecordedProcess $_ })
    if ($alreadyRunning.Count -gt 0) {
        Write-Host 'Research Asset Hub already has recorded running processes:'
        $alreadyRunning | Select-Object name, pid, started_at | Format-Table -AutoSize
        exit 0
    }

    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) {
        & $docker.Source compose -f (Join-Path $projectRoot 'deploy\compose.yaml') up -d
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Infrastructure containers did not start; application services will run degraded.'
        }
    }
    else {
        Write-Warning 'Docker is unavailable; PostgreSQL, Meilisearch, and Qdrant will remain degraded.'
    }

    $node = Get-Command node -ErrorAction SilentlyContinue
    $viteCli = Join-Path $projectRoot 'apps\web\node_modules\vite\bin\vite.js'
    if (-not $node) {
        Write-Error 'Node.js was not found on PATH.'
        exit 2
    }
    if (-not (Test-Path -LiteralPath $viteCli)) {
        Write-Error 'Vite dependencies are missing. Run .\scripts\bootstrap.ps1 first.'
        exit 2
    }

    $started = @(
        Start-HubProcess -Name 'api' -FilePath $pythonPath -ArgumentList @('-m', 'apps.api.main')
        Start-HubProcess -Name 'worker' -FilePath $pythonPath -ArgumentList @('-m', 'apps.worker.main')
        Start-HubProcess -Name 'asset-gateway' -FilePath $pythonPath -ArgumentList @('-m', 'apps.asset_gateway.main')
        Start-HubProcess -Name 'web' -FilePath $node.Source -ArgumentList @($viteCli, '--host', '127.0.0.1', '--port', '8765') -WorkingDirectory (Join-Path $projectRoot 'apps\web')
    )
    $state = [pscustomobject]@{
        schema_version = 1
        project_root = $projectRoot
        processes = $started
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $processStatePath -Encoding UTF8
    Write-Host 'Research Asset Hub started. Web: http://127.0.0.1:8765'
    $started | Select-Object name, pid | Format-Table -AutoSize
    exit 0
}

if ($Command -eq 'stop') {
    $records = @(Get-RecordedProcesses)
    foreach ($record in $records) {
        if (Stop-HubProcessTree $record) {
            Write-Host "Stopped $($record.name) (PID $($record.pid))."
        }
    }
    if (Test-Path -LiteralPath $processStatePath) {
        Remove-Item -LiteralPath $processStatePath -Force
    }
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) {
        & $docker.Source compose -f (Join-Path $projectRoot 'deploy\compose.yaml') down
    }
    exit 0
}

if ($Command -eq 'status' -and (Test-Path -LiteralPath $processStatePath)) {
    $records = @(Get-RecordedProcesses)
    $status = foreach ($record in $records) {
        [pscustomobject]@{
            name = $record.name
            pid = $record.pid
            running = Test-RecordedProcess $record
            started_at = $record.started_at
        }
    }
    if ($Json) {
        [pscustomobject]@{ processes = $status } | ConvertTo-Json -Depth 4
    }
    else {
        $status | Format-Table -AutoSize
    }
    exit 0
}

$arguments = @('-m', 'packages.cli.main', $Command)
if ($All) {
    $arguments += '--all'
}
if ($DryRun) {
    $arguments += '--dry-run'
}
if ($Json) {
    $arguments += '--json'
}
if ($Source) {
    $arguments += @('--source', $Source)
}

Push-Location $projectRoot
try {
    & $pythonPath @arguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
