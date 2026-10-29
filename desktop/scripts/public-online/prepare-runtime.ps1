[CmdletBinding()]
param([string]$InstallDir, [string]$CacheDir, [string]$LockFile, [switch]$ValidateOnly,[switch]$RegistryOnly)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Get-SafeRoot([string]$Path) {
    if ($Path -notmatch '^[A-Za-z]:[\\/]') { throw 'A local absolute drive path is required' }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    if ($full -eq [IO.Path]::GetPathRoot($full).TrimEnd('\')) { throw 'Drive roots are not allowed' }
    for ($a=$full; $a; $a=(Split-Path -Parent $a)) {
        if ((Test-Path -LiteralPath $a) -and ((Get-Item -LiteralPath $a).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked directories are not allowed' }
    }
    return $full
}
function Assert-FreshTarget([string]$Root) {
    if ((Test-Path -LiteralPath $Root) -and (Get-ChildItem -LiteralPath $Root -Force | Select-Object -First 1)) {
        throw 'ONLINE PREVIEW IS FRESH-INSTALL ONLY. Choose an empty directory. Existing installations and data are unchanged.'
    }
    $data=$env:HUB_DATA_DIR
    if (-not $data) {
        $preference=Join-Path $env:LOCALAPPDATA 'ResearchAssetHub/data-directory.txt'
        $data=if (Test-Path -LiteralPath $preference -PathType Leaf) {
            [IO.File]::ReadAllText($preference,[Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)
        } else { Join-Path $env:USERPROFILE 'Documents/ResearchAssetHub' }
    }
    if ($data) {
        $data=[IO.Path]::GetFullPath($data).TrimEnd('\','/')
        if ($Root.Equals($data,[StringComparison]::OrdinalIgnoreCase) -or $Root.StartsWith($data+'\',[StringComparison]::OrdinalIgnoreCase) -or $data.StartsWith($Root+'\',[StringComparison]::OrdinalIgnoreCase)) {
            throw 'Installation directory must not overlap the user library directory'
        }
    }
}
function Assert-NoRegisteredInstallation {
    foreach($hive in @([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryHive]::CurrentUser)) {
        foreach($view in @([Microsoft.Win32.RegistryView]::Registry64,[Microsoft.Win32.RegistryView]::Registry32)) {
            $base=[Microsoft.Win32.RegistryKey]::OpenBaseKey($hive,$view)
            $uninstall=$base.OpenSubKey('Software\Microsoft\Windows\CurrentVersion\Uninstall')
            try {
                if ($uninstall) {
                    foreach($name in $uninstall.GetSubKeyNames()) {
                        $key=$uninstall.OpenSubKey($name)
                        try {
                            if ($key -and ($name -eq 'ResearchAssetHub' -or $key.GetValue('DisplayName') -eq 'ResearchAssetHub')) { throw 'An existing ResearchAssetHub installation is registered. This online preview does not upgrade or replace it.' }
                        } finally { if($key){$key.Dispose()} }
                    }
                }
            } finally { if($uninstall){$uninstall.Dispose()};$base.Dispose() }
        }
    }
}
function Get-VerifiedDownload($Item,[string]$Destination) {
    $uri=[Uri]$Item.url
    if ($uri.Scheme -ne 'https' -or $uri.Host -notin @('get.enterprisedb.com','github.com')) { throw 'Unexpected vendor host' }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $request=[Net.HttpWebRequest]::Create($uri)
    $request.Timeout=30000
    $request.ReadWriteTimeout=60000
    $response=$request.GetResponse()
    $stream=$response.GetResponseStream()
    $output=[IO.File]::Create($Destination)
    try {
        $buffer=New-Object byte[] 65536
        $total=[long]0
        while (($read=$stream.Read($buffer,0,$buffer.Length)) -gt 0) {
            $total+=$read
            if ($total -gt [long]$Item.bytes) { throw 'Vendor download exceeds locked size' }
            $output.Write($buffer,0,$read)
        }
        if ($total -ne [long]$Item.bytes) { throw 'Incomplete vendor download' }
    } finally { $output.Dispose(); $stream.Dispose(); $response.Dispose() }
    if ((Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash -ne $Item.sha256) { throw "Vendor SHA-256 mismatch: $($Item.id)" }
}
function Expand-VerifiedZip([string]$Archive,[string]$Destination,[string]$StripPrefix='',[string[]]$ExcludeTopLevel=@()) {
    $dest=[IO.Path]::GetFullPath($Destination)
    New-Item -ItemType Directory -Path $dest -Force | Out-Null
    $zip=[IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        $total=[long]0
        foreach($entry in $zip.Entries) {
            $name=$entry.FullName.Replace('\','/')
            if ($name -match '(^/|:|(^|/)\.\.(/|$)|(^|/)\.(/|$)|\x00|//)' -or (($entry.ExternalAttributes -shr 16) -band 0xf000) -eq 0xa000) { throw 'Unsafe archive entry' }
            if ($StripPrefix) {
                if (-not $name.StartsWith($StripPrefix,[StringComparison]::Ordinal)) { throw 'Unexpected vendor archive root' }
                $name=$name.Substring($StripPrefix.Length)
            }
            if (-not $name) { continue }
            # The app never launches pgAdmin or StackBuilder. Their vendor ZIP
            # payloads need not be installed alongside the server tools/docs.
            if ($name.Split('/')[0] -in $ExcludeTopLevel) { continue }
            $target=[IO.Path]::GetFullPath((Join-Path $dest $name))
            if (-not $target.StartsWith($dest+[IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Archive path escaped extraction directory' }
            $total+=$entry.Length
            if ($total -gt 4GB) { throw 'Vendor archive exceeds extraction bound' }
            if ($entry.FullName.EndsWith('/')) { New-Item -ItemType Directory -Path $target -Force | Out-Null; continue }
            New-Item -ItemType Directory -Path (Split-Path -Parent $target) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry,$target,$false)
        }
    } finally { $zip.Dispose() }
}
function Invoke-OnlinePreparation {
    if($RegistryOnly){Assert-NoRegisteredInstallation;Write-Output 'No existing installation registered';return}
    $root=Get-SafeRoot $InstallDir
    $cache=Get-SafeRoot $CacheDir
    if ($cache.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase) -or $root.StartsWith($cache+'\',[StringComparison]::OrdinalIgnoreCase) -or $cache -eq $root) { throw 'Cache and installation roots must be separate' }
    Assert-FreshTarget $root
    Assert-NoRegisteredInstallation
    $lock=Get-Content -LiteralPath $LockFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema -ne 'research-asset-hub.online-runtime/v1') { throw 'Unsupported dependency lock' }
    if ($ValidateOnly) { Write-Output 'Validated; no network or installation performed';return }
    # This preview never executes downloaded scripts or dependency installers.
    # NSIS itself is 32-bit. Avoid mistaking the x86 VC runtime for the required
    # x64 runtime when its helper runs under WOW64 Windows PowerShell.
    $nativeSystem=Join-Path $env:WINDIR $(if ([Environment]::Is64BitProcess) { 'System32' } else { 'Sysnative' })
    if (-not (Test-Path -LiteralPath (Join-Path $nativeSystem 'msvcp140.dll')) -or -not (Test-Path -LiteralPath (Join-Path $nativeSystem 'vcruntime140.dll'))) {
        throw 'Microsoft Visual C++ 2015-2022 x64 Runtime is required. Install from https://aka.ms/vs/17/release/vc_redist.x64.exe then rerun. Existing files/data are unchanged.'
    }
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $prepared=Join-Path $cache 'prepared-runtime'
    New-Item -ItemType Directory -Path $prepared | Out-Null
    foreach($item in $lock.downloads) {
        Write-Output "Downloading directly from supplier: $($item.id) $($item.version)"
        $file=Join-Path $cache $item.filename
        Get-VerifiedDownload $item $file
        switch($item.id) {
            'postgresql' { Expand-VerifiedZip $file (Join-Path $prepared 'postgres') 'pgsql/' @('pgAdmin 4','StackBuilder') }
            'meilisearch' { Copy-Item -LiteralPath $file -Destination (Join-Path $prepared 'meilisearch.exe') }
            'restic' {
                $restic=Join-Path $cache 'restic-extracted'
                Expand-VerifiedZip $file $restic
                $exe=Join-Path $restic $item.executable
                if ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ne $item.executable_sha256) { throw 'Restic executable SHA-256 mismatch' }
                Copy-Item -LiteralPath $exe -Destination (Join-Path $prepared 'restic.exe')
            }
            default { throw 'Unexpected runtime dependency id' }
        }
    }
    foreach($file in @('postgres/bin/pg_ctl.exe','postgres/bin/initdb.exe','postgres/bin/psql.exe','postgres/bin/postgres.exe','meilisearch.exe','restic.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $prepared $file) -PathType Leaf)) { throw "Downloaded runtime incomplete: $file" }
    }
    # Recheck ancestors after network activity, before any installation writes.
    if ((Get-SafeRoot $InstallDir) -ne $root -or (Get-SafeRoot $CacheDir) -ne $cache) { throw 'Path changed during vendor download' }
    Assert-FreshTarget $root
    Assert-NoRegisteredInstallation
    $runtime=Join-Path $root 'runtime'
    Copy-Item -LiteralPath $LockFile -Destination (Join-Path $prepared 'online-vendor-runtime.json')
    try {
        New-Item -ItemType Directory -Path $root -Force | Out-Null
        Copy-Item -LiteralPath $prepared -Destination $runtime -Recurse -ErrorAction Stop
    } catch {
        if (Test-Path -LiteralPath $runtime) {
            $resolved=Get-SafeRoot $runtime
            if ($resolved -ne (Join-Path $root 'runtime')) { throw 'Cleanup boundary mismatch' }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
        throw
    }
    Write-Output 'External runtimes verified before application files are copied'
}
if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-OnlinePreparation; exit 0 } catch { Write-Output $_.Exception.Message; exit 1 }
}
