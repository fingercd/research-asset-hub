[CmdletBinding()]
param()
$ErrorActionPreference='Stop'
$online=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
. (Join-Path $online 'prepare-runtime.ps1')
Add-Type -AssemblyName System.IO.Compression
$temporary=Join-Path ([IO.Path]::GetTempPath()) ('rah-online-safety-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$passed=New-Object 'Collections.Generic.List[string]'
function Assert-True($Condition,[string]$Name){if(-not $Condition){throw $Name};$passed.Add($Name)}
function Assert-Rejected([scriptblock]$Action,[string]$Name){$rejected=$false;try{& $Action | Out-Null}catch{$rejected=$true};Assert-True $rejected $Name}
function New-ZipFixture([string]$Name,[string]$Entry){
    $path=Join-Path $temporary $Name
    $zip=[IO.Compression.ZipFile]::Open($path,[IO.Compression.ZipArchiveMode]::Create)
    try{$entryObject=$zip.CreateEntry($Entry);$writer=New-Object IO.StreamWriter($entryObject.Open());try{$writer.Write('fixture-only')}finally{$writer.Dispose()}}finally{$zip.Dispose()}
    return $path
}
$oldData=$env:HUB_DATA_DIR
try {
    # Only this test-owned temporary root is written. No registry is modified,
    # no installer/dependency/service is started, and no vendor request is made.
    $env:HUB_DATA_DIR=Join-Path $temporary 'user-library'
    $safe=New-ZipFixture 'safe.zip' 'pgsql/bin/postgres.exe'
    Expand-VerifiedZip $safe (Join-Path $temporary 'safe') 'pgsql/'
    Assert-True (Test-Path -LiteralPath (Join-Path $temporary 'safe/bin/postgres.exe')) 'safe ZIP extracts'
    foreach($case in @(@('traversal.zip','../escape.txt'),@('ads.zip','pgsql/foo:bar'),@('absolute.zip','C:/escape.txt'))){
        $archive=New-ZipFixture $case[0] $case[1]
        $destination=Join-Path $temporary ($case[0]+'.output')
        Assert-Rejected {Expand-VerifiedZip $archive $destination} ('unsafe ZIP rejected: '+$case[0])
    }
    $occupied=Join-Path $temporary 'occupied'
    New-Item -ItemType Directory -Path $occupied | Out-Null
    $sentinel=Join-Path $occupied 'sentinel.txt'
    [IO.File]::WriteAllText($sentinel,'keep')
    Assert-Rejected {Assert-FreshTarget $occupied} 'nonempty installation rejected'
    Assert-True ([IO.File]::ReadAllText($sentinel) -eq 'keep') 'existing sentinel unchanged'
    Assert-Rejected {Get-SafeRoot 'C:\'} 'drive root rejected'
    Assert-Rejected {Get-SafeRoot 'relative/path'} 'relative path rejected'
    Assert-Rejected {Assert-FreshTarget (Join-Path $env:HUB_DATA_DIR 'subfolder')} 'user-library overlap rejected'
    # Junction test requires no service/GUI. Its external target is also a
    # sibling owned fixture, not an actual library or installation.
    $outside=Join-Path $temporary 'junction-target'
    New-Item -ItemType Directory -Path $outside | Out-Null
    $junction=Join-Path $temporary 'junction'
    New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
    try{Assert-Rejected {Get-SafeRoot (Join-Path $junction 'child')} 'linked ancestor rejected'}finally{[IO.Directory]::Delete($junction)}
    # Pure ValidateOnly fixture explicitly isolates the machine's real registry.
    # The production function still rejects real registered installs in .onInit.
    function Assert-NoRegisteredInstallation {}
    $InstallDir=Join-Path $temporary 'fresh';$CacheDir=Join-Path $temporary 'cache'
    $LockFile=Join-Path $temporary 'lock.json'
    [IO.File]::WriteAllText($LockFile,'{"schema":"research-asset-hub.online-runtime/v1","downloads":[]}')
    $ValidateOnly=$true;$RegistryOnly=$false
    Invoke-OnlinePreparation
    Assert-True (-not (Test-Path -LiteralPath $InstallDir)) 'ValidateOnly creates no installation'
    Assert-True (-not (Test-Path -LiteralPath $CacheDir)) 'ValidateOnly creates no cache'
    # Test file-removal helper only on owned fixtures, never NSIS uninstall.
    $remove=Join-Path $online 'remove-runtime.ps1'
    $owned=Join-Path $temporary 'owned-install';$runtime=Join-Path $owned 'runtime'
    New-Item -ItemType Directory -Path $runtime | Out-Null
    [IO.File]::WriteAllText((Join-Path $runtime 'online-vendor-runtime.json'),'{"schema":"research-asset-hub.online-runtime/v1"}')
    [IO.File]::WriteAllText((Join-Path $owned 'sibling-data.txt'),'keep')
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $remove -InstallDir $owned -ValidateOnly
    Assert-True ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $runtime)) 'uninstall ValidateOnly deletes nothing'
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $remove -InstallDir $owned
    Assert-True ($LASTEXITCODE -eq 0 -and -not (Test-Path -LiteralPath $runtime)) 'owned fixture runtime removed'
    Assert-True ([IO.File]::ReadAllText((Join-Path $owned 'sibling-data.txt')) -eq 'keep') 'sibling fixture data retained'
    $unknown=Join-Path $temporary 'unknown-install';$unknownRuntime=Join-Path $unknown 'runtime'
    New-Item -ItemType Directory -Path $unknownRuntime | Out-Null
    [IO.File]::WriteAllText((Join-Path $unknownRuntime 'sentinel.txt'),'keep')
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $remove -InstallDir $unknown
    Assert-True ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath (Join-Path $unknownRuntime 'sentinel.txt'))) 'missing ownership marker retains files'
    $libraryInstall=Join-Path $temporary 'library-in-install';$libraryRuntime=Join-Path $libraryInstall 'runtime'
    New-Item -ItemType Directory -Path (Join-Path $libraryRuntime 'postgres-data') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $libraryRuntime 'postgres-data/PG_VERSION'),'16')
    [IO.File]::WriteAllText((Join-Path $libraryRuntime 'online-vendor-runtime.json'),'{"schema":"research-asset-hub.online-runtime/v1"}')
    & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $remove -InstallDir $libraryInstall -ValidateOnly
    Assert-True ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath (Join-Path $libraryRuntime 'postgres-data/PG_VERSION'))) 'historical CLI library prevents uninstall'
    $linkedInstall=Join-Path $temporary 'linked-app-install'
    New-Item -ItemType Directory -Path (Join-Path $linkedInstall 'runtime') -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $linkedInstall 'runtime/online-vendor-runtime.json'),'{"schema":"research-asset-hub.online-runtime/v1"}')
    [IO.File]::WriteAllText((Join-Path $outside 'external-sentinel.txt'),'keep')
    $appLink=Join-Path $linkedInstall 'app'
    New-Item -ItemType Junction -Path $appLink -Target $outside | Out-Null
    try {
        & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $remove -InstallDir $linkedInstall -ValidateOnly
        Assert-True ($LASTEXITCODE -ne 0 -and [IO.File]::ReadAllText((Join-Path $outside 'external-sentinel.txt')) -eq 'keep') 'uninstall rejects linked app before ordinary deletion'
    } finally {[IO.Directory]::Delete($appLink)}
    # Static ordering regressions cover /S and the ordinary Tauri resource Delete.
    $template=Get-Content -LiteralPath (Join-Path $online 'installer.nsi') -Raw -Encoding UTF8
    $hooks=Get-Content -LiteralPath (Join-Path $online 'installer-hooks.nsh') -Raw -Encoding UTF8
    $init=$template.IndexOf('Function .onInit');$guard=$template.IndexOf('!insertmacro RAH_PUBLIC_ONLINE_INIT_GUARD',$init)
    Assert-True ($guard -gt $init -and $guard -lt $template.IndexOf('Section WebView2',$init)) 'silent init guard precedes WebView2 and install sections'
    Assert-True (-not $template.Contains('!insertmacro CheckIfAppIsRunning')) 'force-kill gates absent from online template'
    $uninstall=$template.IndexOf('Section Uninstall')
    Assert-True ($template.IndexOf('!insertmacro NSIS_HOOK_PREUNINSTALL',$uninstall) -lt $template.IndexOf('Delete "$INSTDIR',$uninstall)) 'uninstall safety gate precedes ordinary resource deletion'
    Assert-True ($hooks.Contains('-RegistryOnly') -and $hooks.Contains('-ValidateOnly') -and $hooks.Contains('SetErrorLevel 2')) 'failure and readonly guards are wired'
    $stageScript=Get-Content -LiteralPath (Join-Path $online 'prepare-stage.ps1') -Raw -Encoding UTF8
    Assert-True ($stageScript.IndexOf('foreach($boundary') -lt $stageScript.IndexOf('$manifest=Get-Content') -and $stageScript.IndexOf('foreach($boundary') -lt $stageScript.IndexOf('Remove-Item')) 'stage resource ancestor gate precedes reads/deletion'
    $passed | ForEach-Object {Write-Output ('PASS '+$_)}
    Write-Output ("Passed {0} safety regressions. Temporary fixture directory: {1}" -f $passed.Count,$temporary)
} finally {
    $env:HUB_DATA_DIR=$oldData
    # Retain the tiny owned fixture directory for inspection. No broad recursive
    # cleanup runs in this public safety test; no original directory is touched.
}
