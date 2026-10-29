[CmdletBinding()]
param([Parameter(Mandatory)][string]$InstallDir,[switch]$ValidateOnly)
$ErrorActionPreference='Stop'
try {
    if ($InstallDir -notmatch '^[A-Za-z]:[\\/]') { throw 'Absolute local installation path required' }
    $root=[IO.Path]::GetFullPath($InstallDir).TrimEnd('\','/')
    if ($root -eq [IO.Path]::GetPathRoot($root).TrimEnd('\')) { throw 'Drive root rejected' }
    $runtime=Join-Path $root 'runtime'
    for ($a=$runtime; $a; $a=(Split-Path -Parent $a)) {
        if ((Test-Path -LiteralPath $a) -and ((Get-Item -LiteralPath $a).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked installation paths rejected' }
    }
    $data=$env:HUB_DATA_DIR
    if(-not $data){
        $preference=Join-Path $env:LOCALAPPDATA 'ResearchAssetHub/data-directory.txt'
        $data=if(Test-Path -LiteralPath $preference -PathType Leaf){[IO.File]::ReadAllText($preference,[Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)}else{Join-Path $env:USERPROFILE 'Documents/ResearchAssetHub'}
    }
    if($data){
        $data=[IO.Path]::GetFullPath($data).TrimEnd('\','/')
        if($root.Equals($data,[StringComparison]::OrdinalIgnoreCase) -or $root.StartsWith($data+'\',[StringComparison]::OrdinalIgnoreCase) -or $data.StartsWith($root+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Installation overlaps selected user library; uninstall refused'}
    }
    # Traverse one level at a time so junctions are rejected before following them.
    $queue=New-Object 'Collections.Generic.Queue[string]'
    foreach($tree in @((Join-Path $root 'app'),$runtime)){
        if(Test-Path -LiteralPath $tree){
            if((Get-Item -LiteralPath $tree).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked app/runtime root; uninstall refused'}
            $queue.Enqueue($tree)
        }
    }
    while($queue.Count){
        $directory=$queue.Dequeue()
        foreach($item in Get-ChildItem -LiteralPath $directory -Force){
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Linked app/runtime content; uninstall refused'}
            if($item.Name -eq 'PG_VERSION' -and (Split-Path -Leaf $directory) -eq 'postgres-data'){throw 'A database library exists inside installation resources; uninstall refused'}
            if($item.PSIsContainer){$queue.Enqueue($item.FullName)}
        }
    }
    $marker=Join-Path $runtime 'online-vendor-runtime.json'
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) { throw 'Online runtime ownership marker missing; files retained for manual inspection' }
    $lock=Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($lock.schema -ne 'research-asset-hub.online-runtime/v1') { throw 'Unknown runtime marker; files retained' }
    if($ValidateOnly){Write-Output 'Uninstall path, link, library and ownership checks passed; nothing deleted';exit 0}
    # This is only the installer-owned runtime. Documents/ResearchAssetHub,
    # LOCALAPPDATA preferences and registered external sources are not addressed.
    Remove-Item -LiteralPath $runtime -Recurse -Force
    Write-Output 'Installer-owned online runtime removed; user library and preferences retained'
    exit 0
} catch { Write-Output $_.Exception.Message; exit 1 }
