[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $EnvironmentFile,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $Database,
    [Parameter(Mandatory = $true)]
    [ValidateSet('console', 'daemon', 'protection', 'ingestion', 'research', 'report')]
    [string] $Component
)

$ErrorActionPreference = 'Stop'
foreach ($Path in @($Repository, $EnvironmentFile, $Database)) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw 'SERVICE_WRAPPER_PATH_NOT_ABSOLUTE' }
}
$Repository = [System.IO.Path]::GetFullPath($Repository)
$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)
$Database = [System.IO.Path]::GetFullPath($Database)
if ($Commit -notmatch '^[0-9a-fA-F]{40}$') { throw 'SERVICE_WRAPPER_COMMIT_INVALID' }
if (-not (Test-Path -LiteralPath $Repository -PathType Container)) {
    throw 'SERVICE_WRAPPER_REPOSITORY_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_ENV_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_DATABASE_NOT_FOUND'
}
$Python = Join-Path $Repository '.venv\Scripts\python.exe'
$Source = Join-Path $Repository 'src'
$JobHelper = Join-Path $PSScriptRoot 'windows-job.ps1'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $Source -PathType Container) -or
    -not (Test-Path -LiteralPath $JobHelper -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_RUNTIME_NOT_FOUND'
}

# Do not inherit a caller-controlled module path. The venv supplies third-party packages and this
# fixed source root supplies the pinned application module validated by service.py itself.
$env:PYTHONPATH = $Source
. $JobHelper
$ServiceJob = New-ElrondProcessJob

# Keep the interpreter in the foreground so its exit code is the task result. Windows PowerShell
# wraps native stderr as error records under Stop, even when the child is behaving correctly.
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = $null
& $Python -m quantbot.operations.service --env-file $EnvironmentFile --commit $Commit `
    --database $Database --component $Component
if ($null -eq $LASTEXITCODE) { throw 'SERVICE_WRAPPER_NO_EXIT_CODE' }
exit $LASTEXITCODE
