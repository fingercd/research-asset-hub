[CmdletBinding()]
param([switch]$Json)

# Keep the compatibility command, not a second Python/environment resolver.
& (Join-Path $PSScriptRoot 'hub.ps1') preflight -Json:$Json
exit $LASTEXITCODE
