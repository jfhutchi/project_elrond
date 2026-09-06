<#
.SYNOPSIS
Run local CI. Docker/WSL installation is an explicit operator prerequisite.
#>
[CmdletBinding()]
param(
    [ValidateSet('all', 'linux', 'windows')][string] $Platform = 'all',
    [switch] $CheckPrerequisites
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$uvCommand = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uvCommand) {
    Write-Error 'UV_NOT_FOUND: install uv, reopen PowerShell, then rerun. See docs/local-ci.md.'
    exit 1
}
# Bootstrap only; --no-project/--no-config prevent the operator project from being loaded.
$runnerArguments = @('--platform', $Platform)
if ($CheckPrerequisites) { $runnerArguments += '--check-prerequisites' }
& $uvCommand.Source --no-config run --no-project --python 3.11 python -I -S -B `
    (Join-Path $PSScriptRoot 'local_ci.py') @runnerArguments
exit $LASTEXITCODE
