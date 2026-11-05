[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $EnvironmentFile,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $Database,
    [Parameter(Mandatory = $true)]
    [ValidateSet('console', 'daemon', 'protection', 'ingestion', 'research', 'report')]
    [string] $Component,
    [string] $ExperimentStore
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
. (Join-Path $PSScriptRoot 'runtime-bootstrap.ps1') `
    -Repository $Repository -ErrorPrefix 'SERVICE_WRAPPER'
$Arguments = @('-m', 'quantbot.operations.service', '--env-file', $EnvironmentFile,
    '--commit', $Commit, '--database', $Database, '--component', $Component)
if ($ExperimentStore) {
    if ($Component -ne 'console' -or -not [IO.Path]::IsPathRooted($ExperimentStore)) {
        throw 'SERVICE_WRAPPER_EXPERIMENT_SOURCE_INVALID'
    }
    $Arguments += @('--experiment-store', $ExperimentStore)
}
& $Python @Arguments
if ($null -eq $LASTEXITCODE) { throw 'SERVICE_WRAPPER_NO_EXIT_CODE' }
exit $LASTEXITCODE
