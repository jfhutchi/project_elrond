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
# Resolve only an already installed managed interpreter. uv run can load an ambient
# UV_ENV_FILE before Python gets a chance to sanitize anything, even with --no-project.
$bootstrap = New-Object System.Diagnostics.ProcessStartInfo
$bootstrap.FileName = $uvCommand.Source
$bootstrap.Arguments = '--no-config python find --managed-python --system 3.11'
$bootstrap.UseShellExecute = $false
$bootstrap.CreateNoWindow = $true
$bootstrap.RedirectStandardOutput = $true
$bootstrap.RedirectStandardError = $true
$bootstrap.EnvironmentVariables.Clear()
foreach ($name in @('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP',
                    'APPDATA', 'LOCALAPPDATA')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'Process')
    if ($value) { $bootstrap.EnvironmentVariables[$name] = $value }
}
$bootstrap.EnvironmentVariables['UV_NO_CONFIG'] = '1'
$process = [Diagnostics.Process]::Start($bootstrap)
if (-not $process.WaitForExit(30000)) {
    $process.Kill()
    $process.Dispose()
    Write-Error 'PYTHON_LOOKUP_TIMEOUT: uv could not locate managed Python 3.11 within 30 seconds.'
    exit 1
}
$pythonPath = $process.StandardOutput.ReadToEnd().Trim()
$lookupExit = $process.ExitCode
$process.Dispose()
if ($lookupExit -ne 0 -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    Write-Error 'PYTHON_NOT_FOUND: run uv python install 3.11, then rerun local CI.'
    exit 1
}
$runnerArguments = @('--platform', $Platform)
if ($CheckPrerequisites) { $runnerArguments += '--check-prerequisites' }
& $pythonPath -I -S -B (Join-Path $PSScriptRoot 'local_ci.py') @runnerArguments
exit $LASTEXITCODE
