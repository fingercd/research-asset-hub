[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('preflight', 'inventory', 'ingest', 'validate', 'status', 'start', 'dev', 'stop')]
    [string]$Command = 'status',

    [switch]$All,
    [switch]$DryRun,
    [switch]$Json,
    [switch]$KeepInfrastructure,
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

function Get-HubPort {
    param(
        [Parameter(Mandatory)][string]$EnvironmentName,
        [Parameter(Mandatory)][int]$Default
    )

    $raw = [Environment]::GetEnvironmentVariable($EnvironmentName)
    if ([string]::IsNullOrWhiteSpace($raw)) {
        return $Default
    }
    $value = 0
    if (-not [int]::TryParse($raw, [ref]$value) -or $value -lt 1 -or $value -gt 65535) {
        throw "$EnvironmentName must be an integer between 1 and 65535."
    }
    return $value
}

$webPort = Get-HubPort -EnvironmentName 'HUB_WEB_PORT' -Default 8765
$assetGatewayPort = Get-HubPort -EnvironmentName 'HUB_ASSET_GATEWAY_PORT' -Default 8766
$apiPort = Get-HubPort -EnvironmentName 'HUB_API_PORT' -Default 8767
if (@(@($webPort, $assetGatewayPort, $apiPort) | Sort-Object -Unique).Count -ne 3) {
    throw 'HUB_WEB_PORT, HUB_ASSET_GATEWAY_PORT, and HUB_API_PORT must be distinct.'
}
$webUrl = "http://127.0.0.1:$webPort"
$assetGatewayUrl = "http://127.0.0.1:$assetGatewayPort"
$apiUrl = "http://127.0.0.1:$apiPort"
$runtimeUrls = [ordered]@{
    web = "$webUrl/"
    api = "$apiUrl/"
    api_docs = "$apiUrl/docs"
    api_health = "$apiUrl/api/health"
    asset_gateway = "$assetGatewayUrl/"
    asset_gateway_health = "$assetGatewayUrl/health"
}

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
        $startedRaw = [string]$Record.started_at_utc
        if ([string]::IsNullOrWhiteSpace($startedRaw)) {
            $startedRaw = [string]$Record.started_at
        }
        try {
            $recorded = [datetime]::Parse(
                $startedRaw,
                [Globalization.CultureInfo]::InvariantCulture,
                [Globalization.DateTimeStyles]::AdjustToUniversal
            )
        }
        catch {
            $recorded = [datetime]::Parse($startedRaw).ToUniversalTime()
        }
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
    $startedUtc = $process.StartTime.ToUniversalTime()
    return [pscustomobject]@{
        name = $Name
        pid = $process.Id
        started_at = $startedUtc.ToLocalTime().ToString('o')
        started_at_utc = $startedUtc.ToString('o')
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

function Save-HubProcessState {
    param(
        [Parameter(Mandatory)][string]$Mode,
        [Parameter(Mandatory)][array]$Processes
    )

    $state = [pscustomobject]@{
        schema_version = 2
        project_root = $projectRoot
        mode = $Mode
        updated_at = [datetime]::UtcNow.ToString('o')
        urls = $runtimeUrls
        processes = $Processes
    }
    $state | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $processStatePath -Encoding UTF8
}

function Stop-HubProcesses {
    param([Parameter(Mandatory)][AllowEmptyCollection()][array]$Records)

    $ordered = @($Records)
    if ($ordered.Count -eq 0) {
        return
    }
    [array]::Reverse($ordered)
    foreach ($record in $ordered) {
        Stop-HubProcessTree $record | Out-Null
    }
}

function Invoke-HubHealthProbe {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Url,
        [switch]$AnySuccessfulResponse,
        [switch]$AllowDegradedWithReadyDependencies
    )

    try {
        $response = Invoke-WebRequest `
            -Uri $Url `
            -Method Get `
            -UseBasicParsing `
            -TimeoutSec 3
        $reportedStatus = $null
        try {
            $payload = $response.Content | ConvertFrom-Json
            if ($payload.PSObject.Properties.Name -contains 'status') {
                $reportedStatus = [string]$payload.status
            }
        }
        catch {
            $reportedStatus = $null
        }
        $successful = [int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 300
        $degradedDependenciesReady = (
            $AllowDegradedWithReadyDependencies -and
            $reportedStatus -eq 'degraded' -and
            $null -ne $payload -and
            [string]$payload.database -eq 'ready' -and
            [string]$payload.signing -eq 'ready'
        )
        $ready = $successful -and (
            $AnySuccessfulResponse -or
            $reportedStatus -eq 'healthy' -or
            $degradedDependenciesReady
        )
        return [pscustomobject]@{
            name = $Name
            url = $Url
            reachable = $successful
            status = if ($reportedStatus) { $reportedStatus } elseif ($successful) { 'reachable' } else { 'unavailable' }
            ready = $ready
            detail = "HTTP $([int]$response.StatusCode)"
        }
    }
    catch {
        return [pscustomobject]@{
            name = $Name
            url = $Url
            reachable = $false
            status = 'unavailable'
            ready = $false
            detail = "$($_.Exception.GetType().Name): endpoint did not answer."
        }
    }
}

function Get-HubRuntimeStatus {
    param(
        [array]$Records = @(),
        [string]$Mode = 'start',
        $Urls = $runtimeUrls
    )

    $resolvedApiUrl = ([string]$Urls.api).TrimEnd('/')
    $resolvedGatewayUrl = ([string]$Urls.asset_gateway).TrimEnd('/')
    $resolvedWebUrl = ([string]$Urls.web).TrimEnd('/')
    $processes = @(
        foreach ($record in $Records) {
            [pscustomobject]@{
                name = [string]$record.name
                pid = [int]$record.pid
                running = Test-RecordedProcess $record
                started_at = [string]$record.started_at
            }
        }
    )
    $expectedWebProcess = if ($Mode -eq 'dev') { 'web-dev' } else { 'web' }
    $requiredNames = @('api', 'worker', 'asset-gateway', $expectedWebProcess)
    if (@($Records | Where-Object { [string]$_.name -eq 'wsl-keepalive' }).Count -gt 0) {
        $requiredNames += 'wsl-keepalive'
    }
    $runningNames = @(
        $processes |
            Where-Object { $_.running } |
            ForEach-Object { [string]$_.name }
    )
    $missingProcesses = @($requiredNames | Where-Object { $_ -notin $runningNames })
    $services = @(
        Invoke-HubHealthProbe -Name 'api' -Url "$resolvedApiUrl/api/health"
        Invoke-HubHealthProbe `
            -Name 'asset-gateway' `
            -Url "$resolvedGatewayUrl/health" `
            -AllowDegradedWithReadyDependencies
        if ($Mode -eq 'dev') {
            Invoke-HubHealthProbe -Name 'web-dev' -Url "$resolvedWebUrl/" -AnySuccessfulResponse
        }
        else {
            Invoke-HubHealthProbe -Name 'web' -Url "$resolvedWebUrl/health"
        }
    )
    $ready = (
        $Records.Count -gt 0 -and
        $missingProcesses.Count -eq 0 -and
        @($services | Where-Object { -not $_.ready }).Count -eq 0
    )
    $applicationProcessCount = @(
        $processes | Where-Object { $_.name -ne 'wsl-keepalive' }
    ).Count
    $overallStatus = if ($ready) {
        'ready'
    }
    elseif ($applicationProcessCount -eq 0) {
        'stopped'
    }
    else {
        'degraded'
    }
    return [pscustomobject]@{
        schema_version = 2
        status = $overallStatus
        mode = $Mode
        urls = $Urls
        missing_processes = $missingProcesses
        processes = $processes
        services = $services
    }
}

function Write-HubRuntimeStatus {
    param(
        [Parameter(Mandatory)]$Runtime,
        [switch]$AsJson
    )

    if ($AsJson) {
        $Runtime | ConvertTo-Json -Depth 6
        return
    }
    Write-Host "Research Asset Hub runtime: $($Runtime.status) ($($Runtime.mode))"
    if ($Runtime.processes.Count -gt 0) {
        $Runtime.processes | Format-Table name, pid, running, started_at -AutoSize
    }
    else {
        Write-Host 'No recorded application processes.'
    }
    if ($Runtime.services.Count -gt 0) {
        $Runtime.services | Format-Table name, status, reachable, ready, url -AutoSize
    }
    Write-Host 'URLs:'
    Write-Host "  Web:          $($Runtime.urls.web)"
    Write-Host "  API docs:     $($Runtime.urls.api_docs)"
    Write-Host "  API health:   $($Runtime.urls.api_health)"
    Write-Host "  Asset health: $($Runtime.urls.asset_gateway_health)"
}

function Wait-HubReadiness {
    param(
        [Parameter(Mandatory)][array]$Records,
        [Parameter(Mandatory)][string]$Mode,
        [int]$TimeoutSeconds = 120
    )

    $deadline = [datetime]::UtcNow.AddSeconds($TimeoutSeconds)
    $last = Get-HubRuntimeStatus -Records $Records -Mode $Mode
    while ([datetime]::UtcNow -lt $deadline) {
        if ($last.status -eq 'ready') {
            return $last
        }
        if (
            $last.processes.Count -gt 0 -and
            @($last.processes | Where-Object { -not $_.running }).Count -gt 0
        ) {
            return $last
        }
        Start-Sleep -Seconds 1
        $last = Get-HubRuntimeStatus -Records $Records -Mode $Mode
    }
    return $last
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
    if (
        $Arguments -contains 'up' -and
        (
            -not $script:wslKeepAliveRecord -or
            -not (Test-RecordedProcess $script:wslKeepAliveRecord)
        )
    ) {
        $script:wslKeepAliveRecord = Start-HubProcess `
            -Name 'wsl-keepalive' `
            -FilePath $wsl.Source `
            -ArgumentList @(
                '-d',
                'Ubuntu',
                '-u',
                'root',
                '--',
                'sleep',
                'infinity'
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
    $recordedProcesses = @(Get-RecordedProcesses)
    $alreadyRunning = @($recordedProcesses | Where-Object { Test-RecordedProcess $_ })
    $requiredProcesses = @(
        'api',
        'worker',
        'asset-gateway',
        $(if ($Command -eq 'start') { 'web' } else { 'web-dev' })
    )
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $requiredProcesses += 'wsl-keepalive'
    }
    $runningNames = @($alreadyRunning | ForEach-Object { [string]$_.name })
    $missingProcesses = @($requiredProcesses | Where-Object { $_ -notin $runningNames })
    if ($missingProcesses.Count -eq 0) {
        $existingRuntime = Get-HubRuntimeStatus -Records $alreadyRunning -Mode $Command
        if ($existingRuntime.status -eq 'ready') {
            Write-Host 'Research Asset Hub is already ready.'
            Write-HubRuntimeStatus -Runtime $existingRuntime
            exit 0
        }
        Write-Warning 'Recorded processes are running but runtime health is degraded; restarting them.'
    }
    if ($recordedProcesses.Count -gt 0) {
        Write-Warning (
            'Research Asset Hub has a partial or stale process set; restarting it. ' +
            "Missing: $($missingProcesses -join ', ')"
        )
        $recordsToStop = @($recordedProcesses)
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            $reusableKeepAlive = @(
                $recordedProcesses |
                    Where-Object {
                        [string]$_.name -eq 'wsl-keepalive' -and
                        (Test-RecordedProcess $_)
                    } |
                    Select-Object -First 1
            )
            if ($reusableKeepAlive.Count -eq 1) {
                $script:wslKeepAliveRecord = $reusableKeepAlive[0]
                $recordsToStop = @(
                    $recordedProcesses |
                        Where-Object { [string]$_.name -ne 'wsl-keepalive' }
                )
            }
        }
        Stop-HubProcesses -Records $recordsToStop
        Remove-Item -LiteralPath $processStatePath -Force -ErrorAction SilentlyContinue
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
    $env:HUB_API_HOST = '127.0.0.1'
    $env:HUB_API_PORT = [string]$apiPort
    $env:HUB_WEB_HOST = '127.0.0.1'
    $env:HUB_WEB_PORT = [string]$webPort
    $env:HUB_ASSET_GATEWAY_HOST = '127.0.0.1'
    $env:HUB_ASSET_GATEWAY_PORT = [string]$assetGatewayPort
    $env:HUB_WEB_ORIGIN = $webUrl
    $env:HUB_GATEWAY_ORIGIN = $assetGatewayUrl

    $started = @()
    try {
        $infrastructureReady = Invoke-HubCompose -Arguments @('up', '-d', '--wait')
        if ($script:wslKeepAliveRecord) {
            $started += $script:wslKeepAliveRecord
            Save-HubProcessState -Mode $Command -Processes $started
        }
        if (-not $infrastructureReady) {
            throw 'Infrastructure containers did not reach their Compose health checks.'
        }

        Push-Location $projectRoot
        try {
            & $pythonPath -m alembic -c 'migrations/alembic.ini' upgrade head
            if ($LASTEXITCODE -ne 0) {
                throw "Database migrations failed with exit code $LASTEXITCODE."
            }
        }
        finally {
            Pop-Location
        }

        $started += Start-HubProcess `
            -Name 'api' `
            -FilePath $pythonPath `
            -ArgumentList @('-m', 'apps.api.main')
        Save-HubProcessState -Mode $Command -Processes $started

        $started += Start-HubProcess `
            -Name 'worker' `
            -FilePath $pythonPath `
            -ArgumentList @('-m', 'apps.worker.main')
        Save-HubProcessState -Mode $Command -Processes $started

        $started += Start-HubProcess `
            -Name 'asset-gateway' `
            -FilePath $pythonPath `
            -ArgumentList @('-m', 'apps.asset_gateway.main')
        Save-HubProcessState -Mode $Command -Processes $started

        if ($Command -eq 'start') {
            $started += Start-HubProcess `
                -Name 'web' `
                -FilePath $pythonPath `
                -ArgumentList @('-m', 'apps.api.web_host')
        }
        else {
            $started += Start-HubProcess `
                -Name 'web-dev' `
                -FilePath $node.Source `
                -ArgumentList @(
                    $viteCli,
                    '--host',
                    '127.0.0.1',
                    '--port',
                    [string]$webPort,
                    '--strictPort'
                ) `
                -WorkingDirectory (Join-Path $projectRoot 'apps\web')
        }
        Save-HubProcessState -Mode $Command -Processes $started

        $runtime = Wait-HubReadiness -Records $started -Mode $Command
        if ($runtime.status -ne 'ready') {
            $failedServices = @(
                $runtime.services |
                    Where-Object { -not $_.ready } |
                    ForEach-Object { "$($_.name)=$($_.status)" }
            )
            throw (
                'Application services did not become ready. ' +
                "Missing processes: $($runtime.missing_processes -join ', '); " +
                "unready services: $($failedServices -join ', ')."
            )
        }
        Save-HubProcessState -Mode $Command -Processes $started
        Write-Host "Research Asset Hub $Command mode is ready."
        Write-HubRuntimeStatus -Runtime $runtime
        exit 0
    }
    catch {
        $failure = $_.Exception.Message
        $applicationRecords = @(
            $started | Where-Object { [string]$_.name -ne 'wsl-keepalive' }
        )
        $keepAliveRecords = @(
            $started | Where-Object { [string]$_.name -eq 'wsl-keepalive' }
        )
        Stop-HubProcesses -Records $applicationRecords
        if ($keepAliveRecords.Count -gt 0) {
            Save-HubProcessState -Mode $Command -Processes $keepAliveRecords
        }
        else {
            Remove-Item -LiteralPath $processStatePath -Force -ErrorAction SilentlyContinue
        }
        [Console]::Error.WriteLine(
            "Research Asset Hub startup failed and partial application services were cleaned up: $failure"
        )
        exit 1
    }
}

if ($Command -eq 'stop') {
    $records = @(Get-RecordedProcesses)
    $applicationRecords = @(
        $records | Where-Object { [string]$_.name -ne 'wsl-keepalive' }
    )
    $keepAliveRecords = @(
        $records | Where-Object { [string]$_.name -eq 'wsl-keepalive' }
    )
    foreach ($record in $applicationRecords) {
        $wasRunning = Test-RecordedProcess $record
        Stop-HubProcessTree $record | Out-Null
        if ($wasRunning) {
            Write-Host "Stopped $($record.name) (PID $($record.pid))."
        }
    }
    $composeStopped = $true
    if ($KeepInfrastructure) {
        if ($keepAliveRecords.Count -gt 0) {
            Save-HubProcessState -Mode 'start' -Processes $keepAliveRecords
        }
        else {
            Remove-Item -LiteralPath $processStatePath -Force -ErrorAction SilentlyContinue
        }
        Write-Host 'Compose infrastructure was kept running.'
    }
    else {
        $composeStopped = Invoke-HubCompose -Arguments @('down')
        foreach ($record in $keepAliveRecords) {
            $wasRunning = Test-RecordedProcess $record
            Stop-HubProcessTree $record | Out-Null
            if ($wasRunning) {
                Write-Host "Stopped $($record.name) (PID $($record.pid))."
            }
        }
        if (Test-Path -LiteralPath $processStatePath) {
            Remove-Item -LiteralPath $processStatePath -Force
        }
    }
    if (-not $composeStopped) {
        [Console]::Error.WriteLine('Application processes stopped, but Compose infrastructure could not be stopped.')
        exit 1
    }
    Write-Host 'Research Asset Hub is stopped.'
    exit 0
}

if ($Command -eq 'status') {
    $records = @(Get-RecordedProcesses)
    $mode = 'start'
    $savedUrls = $runtimeUrls
    if (Test-Path -LiteralPath $processStatePath) {
        try {
            $savedState = Get-Content -LiteralPath $processStatePath -Encoding UTF8 -Raw | ConvertFrom-Json
            if ([string]$savedState.mode -in @('start', 'dev')) {
                $mode = [string]$savedState.mode
            }
            if ($savedState.PSObject.Properties.Name -contains 'urls') {
                $savedUrls = $savedState.urls
            }
        }
        catch {
            $mode = 'start'
        }
    }
    $runtime = Get-HubRuntimeStatus -Records $records -Mode $mode -Urls $savedUrls
    Write-HubRuntimeStatus -Runtime $runtime -AsJson:$Json
    exit $(if ($runtime.status -eq 'ready') { 0 } else { 1 })
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
