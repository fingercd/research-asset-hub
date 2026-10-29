[CmdletBinding()]
param([Parameter(Mandatory)][string]$StageDir)
$ErrorActionPreference='Stop'
$desktop=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$stage=[IO.Path]::GetFullPath($StageDir).TrimEnd('\','/')
if ((Split-Path -Parent $stage) -ne $desktop -or (Split-Path -Leaf $stage) -notlike 'stage-*') { throw 'Online stage must be a named stage-* direct child of desktop' }
for ($a=$stage; $a; $a=(Split-Path -Parent $a)) {
    if ((Test-Path -LiteralPath $a) -and ((Get-Item -LiteralPath $a).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Stage cannot use linked ancestors' }
}
$runtime=Join-Path $stage 'runtime'
foreach($boundary in @($runtime,(Join-Path $stage 'app/config'))) {
    for($a=$boundary;$a;$a=(Split-Path -Parent $a)) {
        if((Test-Path -LiteralPath $a) -and ((Get-Item -LiteralPath $a).Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Stage resource ancestors cannot be linked'}
    }
}
$manifest=Get-Content -LiteralPath (Join-Path $runtime 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.profile -ne 'public') { throw 'Only a clean public-profile stage may be converted' }
if (Test-Path -LiteralPath (Join-Path $stage 'app/config/workspace-root.txt')) { throw 'Personal workspace hint is forbidden' }
$python=Join-Path $runtime 'python/python.exe'
& $python -B -c "import sys,yaml;from pathlib import Path;p=Path(sys.argv[1]);assert yaml.safe_load((p/'sources.yaml').read_text('utf-8'))['sources']==[];assert yaml.safe_load((p/'taxonomy.yaml').read_text('utf-8'))['domains']==[]" (Join-Path $stage 'app/config')
if($LASTEXITCODE -ne 0){throw 'Online preview must start with empty sources and domains'}
foreach($name in @('postgres','meilisearch.exe','restic.exe','postgres-utf8-manifests.json')) {
    $target=Join-Path $runtime $name
    if (Test-Path -LiteralPath $target) {
        foreach($item in @((Get-Item -LiteralPath $target)) + @(Get-ChildItem -LiteralPath $target -Recurse -Force -ErrorAction Stop)) {
            if($item.Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Refusing to remove linked vendor stage content'}
        }
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'runtime-lock.json') -Destination (Join-Path $stage 'app/config/online-runtime-lock.json')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'remove-runtime.ps1') -Destination (Join-Path $runtime 'online-remove-runtime.ps1')
$manifest | Add-Member -Force NoteProperty distribution 'public-online-preview'
$manifest | Add-Member -Force NoteProperty external_dependencies 'PostgreSQL, Meilisearch and Restic are fetched directly from suppliers; not redistributed in this installer'
foreach($name in @('postgres','meilisearch','restic')) { $manifest.$name | Add-Member -Force NoteProperty distributed_in_installer $false }
[IO.File]::WriteAllText((Join-Path $runtime 'manifest.json'),($manifest|ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))
Write-Output "Online public stage prepared: $stage. Review dependency notices/source archives separately before distributing."
