[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'inventory', 'ingest', 'validate', 'status', 'start', 'dev', 'stop')]
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
$script:wslKeepAliveRecord = $null

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

function Invoke-HubCompose {
    param([Parameter(Mandatory)][string[]]$Arguments)

    $composePath = Join-Path $projectRoot 'deploy\compose.yaml'
    $docker = Get-Command docker -ErrorAction SilentlyContinue
    if ($docker) {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            # Docker Compose writes ordinary progress to stderr.  Windows
            # PowerShell 5 promotes that stream to NativeCommandError when
            # the script-wide preference is Stop, even when Compose exits 0.
            $ErrorActionPreference = 'Continue'
            $composeOutput = & $docker.Source compose -f $composePath @Arguments 2>&1
            $composeExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        $composeOutput | ForEach-Object { Write-Host $_ }
        return $composeExitCode -eq 0
    }

    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        return $false
    }
    $resolvedComposePath = [IO.Path]::GetFullPath($composePath)
    if (
        $resolvedComposePath.Length -lt 4 -or
        $resolvedComposePath[1] -ne ':' -or
        $resolvedComposePath[2] -ne '\'
    ) {
        return $false
    }
    $drive = $resolvedComposePath.Substring(0, 1).ToLowerInvariant()
    $relativePath = $resolvedComposePath.Substring(3).Replace('\', '/')
    $wslComposePath = "/mnt/$drive/$relativePath"
    if ($Arguments -contains 'up') {
        $script:wslKeepAliveRecord = Start-HubProcess `
            -Name 'wsl-keepalive' `
            -FilePath $wsl.Source `
            -ArgumentList @(
                '-d',
                'Ubuntu',
                '-u',
                'root',
                '--',
                'sh',
                '-lc',
                'while true; do sleep 3600; done'
            )
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $composeOutput = & $wsl.Source -d Ubuntu -u root -- docker compose -f $wslComposePath @Arguments 2>&1
        $composeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $composeOutput | ForEach-Object { Write-Host $_ }
    return $composeExitCode -eq 0
}

if ($Command -in @('start', 'dev')) {
    New-Item -ItemType Directory -Force -Path $stateDirectory, $logDirectory | Out-Null
    $alreadyRunning = @(Get-RecordedProcesses | Where-Object { Test-RecordedProcess $_ })
    if ($alreadyRunning.Count -gt 0) {
        Write-Host 'Research Asset Hub already has recorded running processes:'
        $alreadyRunning | Select-Object name, pid, started_at | Format-Table -AutoSize
        exit 0
    }

    if ($Command -eq 'start') {
        $webIndex = Join-Path $projectRoot 'apps\web\dist\index.html'
        if (-not (Test-Path -LiteralPath $webIndex)) {
            Write-Error 'Production web assets are missing. Build apps\web\dist before hub start.'
            exit 2
        }
    }
    else {
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
    }

    if ([string]::IsNullOrWhiteSpace($env:HUB_GATEWAY_SECRET)) {
        $secretBytes = New-Object byte[] 48
        $randomGenerator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $randomGenerator.GetBytes($secretBytes)
        }
        finally {
            $randomGenerator.Dispose()
        }
        $env:HUB_GATEWAY_SECRET = [Convert]::ToBase64String($secretBytes)
    }
    if ([string]::IsNullOrWhiteSpace($env:HUB_DATABASE_URL)) {
        $postgresPassword = if ([string]::IsNullOrWhiteSpace($env:HUB_POSTGRES_PASSWORD)) {
            'research_hub_local'
        }
        else {
            [uri]::EscapeDataString($env:HUB_POSTGRES_PASSWORD)
        }
        $postgresPort = if ([string]::IsNullOrWhiteSpace($env:HUB_POSTGRES_PORT)) {
            '5432'
        }
        else {
            $env:HUB_POSTGRES_PORT
        }
        $env:HUB_DATABASE_URL = "postgresql://research_hub:$postgresPassword@127.0.0.1:$postgresPort/research_asset_hub"
    }
    if ([string]::IsNullOrWhiteSpace($env:DATABASE_URL)) {
        $env:DATABASE_URL = $env:HUB_DATABASE_URL -replace '^postgresql://', 'postgresql+psycopg://'
    }

    $infrastructureReady = Invoke-HubCompose -Arguments @('up', '-d', '--wait')
    if (-not $infrastructureReady) {
        Write-Warning 'Infrastructure containers did not start; application services will run degraded.'
    }
    else {
        Push-Location $projectRoot
        try {
            & $pythonPath -m alembic -c 'migrations/alembic.ini' upgrade head
            if ($LASTEXITCODE -ne 0) {
                Write-Warning 'Database migrations did not complete; database-backed APIs will report degraded.'
            }
        }
        finally {
            Pop-Location
        }
    }

    if ($Command -eq 'start') {
        $webProcess = Start-HubProcess `
            -Name 'web' `
            -FilePath $pythonPath `
            -ArgumentList @('-m', 'apps.api.web_host')
    }
    else {
        $webProcess = Start-HubProcess `
            -Name 'web-dev' `
            -FilePath $node.Source `
            -ArgumentList @($viteCli, '--host', '127.0.0.1', '--port', '8765') `
            -WorkingDirectory (Join-Path $projectRoot 'apps\web')
    }

    $started = @()
    if ($script:wslKeepAliveRecord) {
        $started += $script:wslKeepAliveRecord
    }
    $started += @(
        Start-HubProcess -Name 'api' -FilePath $pythonPath -ArgumentList @('-m', 'apps.api.main')
        Start-HubProcess -Name 'worker' -FilePath $pythonPath -ArgumentList @('-m', 'apps.worker.main')
        Start-HubProcess -Name 'asset-gateway' -FilePath $pythonPath -ArgumentList @('-m', 'apps.asset_gateway.main')
        $webProcess
    )
    $state = [pscustomobject]@{
        schema_version = 1
        project_root = $projectRoot
        mode = $Command
        processes = $started
    }
    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $processStatePath -Encoding UTF8
    Write-Host "Research Asset Hub $Command mode started. Web: http://127.0.0.1:8765"
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
    Invoke-HubCompose -Arguments @('down') | Out-Null
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
