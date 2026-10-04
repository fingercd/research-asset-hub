[CmdletBinding()]
param([string]$InstallDir, [string]$MappingScript)
$ErrorActionPreference = 'Stop'

function Get-RahFullPath([string]$Path) {
    if ($Path -notmatch '^(?:[a-zA-Z]:[\\/]|\\\\[^\\]+\\[^\\]+)') {
        throw 'Upgrade paths must be absolute'
    }
    $full = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    # Reject links in any existing ancestor rather than guessing their targets
    # through APIs unavailable in Windows PowerShell 5.
    $ancestor = $full
    while ($ancestor) {
        if (Test-Path -LiteralPath $ancestor) {
            $item = Get-Item -LiteralPath $ancestor
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Upgrade roots cannot contain junctions or symbolic links'
            }
        }
        $ancestor = Split-Path -Parent $ancestor
    }
    return $full
}

function Test-RahWithin([string]$Child, [string]$Parent) {
    return $Child.Equals($Parent, [StringComparison]::OrdinalIgnoreCase) -or
        $Child.StartsWith($Parent + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Get-RahLegacyPython([string]$Root) {
    $bundled = Join-Path $Root 'runtime/python/python.exe'
    if (Test-Path -LiteralPath $bundled -PathType Leaf) { return $bundled }
    $hint = Join-Path $Root 'app/config/python-executable.txt'
    if (Test-Path -LiteralPath $hint -PathType Leaf) {
        $python = [IO.File]::ReadAllText($hint, [Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)
        if (Test-Path -LiteralPath $python -PathType Leaf) { return $python }
    }
    throw 'Existing installation has no usable Python; legacy configuration remains unchanged'
}

function Invoke-RahSourceValidation([string]$Python, [string]$Script, [string]$Root, [string]$Reference, [string]$Candidate) {
    # -c normally prepends cwd. The embedded helper instead prepends the OLD app;
    # it never attempts to locate a source checkout through its temporary path.
    $bootstrap = "import runpy,sys;sys.path.insert(0,sys.argv[1]);args=sys.argv[2:];sys.argv=args;runpy.run_path(args[0],run_name='__main__')"
    $arguments = @('-c', $bootstrap, (Join-Path $Root 'app'), $Script,
        '--install-dir', $Root, '--reference', $Reference)
    if ($Candidate) { $arguments += @('--candidate', $Candidate) }
    $previousErrorAction = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $output = & $Python @arguments 2>&1
    } finally { $ErrorActionPreference = $previousErrorAction }
    if ($LASTEXITCODE -ne 0) { throw "Legacy source mapping validation failed: $output" }
}

function Move-RahResource([string]$Source, [string]$Destination) {
    Move-Item -LiteralPath $Source -Destination $Destination -ErrorAction Stop
}

function Invoke-RahPreinstall([string]$Root, [string]$Validator) {
    $Root = Get-RahFullPath $Root
    if ($Root -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) { throw 'Installation root cannot be a drive root' }
    $resources = @('app', 'runtime') | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) }
    if (-not $resources) { Write-Output 'Fresh installation: no legacy resources to preserve'; return }
    $data = if ($env:HUB_DATA_DIR -and $env:HUB_DATA_DIR.Trim()) {
        $env:HUB_DATA_DIR
    } else {
        $preference = Join-Path $env:LOCALAPPDATA 'ResearchAssetHub/data-directory.txt'
        if (Test-Path -LiteralPath $preference -PathType Leaf) {
            [IO.File]::ReadAllText($preference, [Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)
        } else { Join-Path $env:USERPROFILE 'Documents/ResearchAssetHub' }
    }
    $data = Get-RahFullPath $data
    $config = Join-Path $data 'config'
    foreach ($name in $resources) {
        $resource = Get-RahFullPath (Join-Path $Root $name)
        if ((Test-RahWithin $data $resource) -or (Test-RahWithin $config $resource)) {
            throw 'User data/config lies inside installation resources; upgrade aborted'
        }
    }
    if (Test-Path -LiteralPath $config) {
        $null = Get-RahFullPath $config
        if (-not (Test-Path -LiteralPath (Join-Path $config 'sources.yaml') -PathType Leaf)) {
            throw 'Existing user configuration has no sources.yaml; refusing to overwrite managed-only configuration'
        }
    }
    $legacy = Join-Path $Root 'app/config'
    if (Test-Path -LiteralPath (Join-Path $legacy 'sources.yaml') -PathType Leaf) {
        $python = Get-RahLegacyPython $Root
        $oldPythonPath = $env:PYTHONPATH
        $oldWorkspace = $env:HUB_HOST_WORKSPACE_ROOT
        try {
            $env:PYTHONPATH = Join-Path $Root 'app'
            if (-not ($env:HUB_HOST_WORKSPACE_ROOT -and $env:HUB_HOST_WORKSPACE_ROOT.Trim())) {
                $hint = Join-Path $legacy 'workspace-root.txt'
                if (Test-Path -LiteralPath $hint) {
                    $env:HUB_HOST_WORKSPACE_ROOT = [IO.File]::ReadAllText($hint, [Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)
                }
            }
            Invoke-RahSourceValidation $python $Validator $Root (Join-Path $legacy 'sources.yaml') ''
            if (-not (Test-Path -LiteralPath $config)) {
                foreach ($item in Get-ChildItem -LiteralPath $legacy -Recurse -Force) {
                    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                        throw 'Legacy config contains a link; refusing to copy external original files'
                    }
                }
                New-Item -ItemType Directory -Force -Path $data | Out-Null
                $temporary = Join-Path $data ('.config-upgrade-' + [guid]::NewGuid().ToString('N'))
                Copy-Item -LiteralPath $legacy -Destination $temporary -Recurse -ErrorAction Stop
                Invoke-RahSourceValidation $python $Validator $Root (Join-Path $legacy 'sources.yaml') (Join-Path $temporary 'sources.yaml')
                Move-Item -LiteralPath $temporary -Destination $config -ErrorAction Stop
                Write-Output 'Legacy configuration preserved with immutable source root mappings'
            } else {
                $env:HUB_HOST_WORKSPACE_ROOT = $oldWorkspace
                if (-not ($oldWorkspace -and $oldWorkspace.Trim())) {
                    $userHint = Join-Path $config 'workspace-root.txt'
                    if (Test-Path -LiteralPath $userHint) {
                        $env:HUB_HOST_WORKSPACE_ROOT = [IO.File]::ReadAllText($userHint, [Text.Encoding]::UTF8).Trim().TrimStart([char]0xfeff)
                    }
                }
                Invoke-RahSourceValidation $python $Validator $Root (Join-Path $config 'sources.yaml') ''
                Write-Output 'Existing user configuration reused without overwriting it'
            }
        } finally {
            $env:PYTHONPATH = $oldPythonPath
            $env:HUB_HOST_WORKSPACE_ROOT = $oldWorkspace
        }
    } elseif ((Test-Path -LiteralPath (Join-Path $Root 'app')) -or (Test-Path -LiteralPath $config)) {
        throw 'Existing configuration cannot be validated; preserve it and repair sources.yaml before upgrading'
    }
    $previous = Join-Path $Root ('.previous-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $previous -ErrorAction Stop | Out-Null
    $moved = @()
    try {
        foreach ($name in $resources) {
            Move-RahResource (Join-Path $Root $name) (Join-Path $previous $name)
            $moved += $name
        }
    } catch {
        $failure = $_.Exception.Message
        foreach ($name in $moved) {
            try {
                $rollbackTarget = Join-Path $Root $name
                if (Test-Path -LiteralPath $rollbackTarget) { throw 'Rollback destination is occupied' }
                Move-Item -LiteralPath (Join-Path $previous $name) -Destination $rollbackTarget -ErrorAction Stop
            }
            catch { $failure += "; rollback failed; resource remains in $previous" }
        }
        throw "Resource preservation failed. Exit the application from its tray before retrying. $failure"
    }
    Write-Output "Previous application resources preserved at $previous"
}

if ($MyInvocation.InvocationName -ne '.') {
    try { Invoke-RahPreinstall $InstallDir $MappingScript; exit 0 }
    catch { Write-Output $_.Exception.Message; exit 1 }
}
