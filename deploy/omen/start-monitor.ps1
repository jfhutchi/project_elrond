[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $Directory,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $Database,
    [Parameter(Mandatory = $true)][string] $EnvironmentFile,
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
foreach ($Path in @($Repository, $Directory, $Database)) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw 'MONITOR_WRAPPER_PATH_NOT_ABSOLUTE' }
}
$Repository = [System.IO.Path]::GetFullPath($Repository)
$Directory = [System.IO.Path]::GetFullPath($Directory)
$Database = [System.IO.Path]::GetFullPath($Database)

if ($Commit -notmatch '^[0-9a-fA-F]{40}$') { throw 'MONITOR_WRAPPER_COMMIT_INVALID' }
if (-not (Test-Path -LiteralPath $Repository -PathType Container)) {
    throw 'MONITOR_WRAPPER_REPOSITORY_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
    throw 'MONITOR_WRAPPER_DIRECTORY_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw 'MONITOR_WRAPPER_DATABASE_NOT_FOUND'
}
. (Join-Path $PSScriptRoot 'runtime-bootstrap.ps1') `
    -Repository $Repository -ErrorPrefix 'MONITOR_WRAPPER'
$WorkerArguments = @('-m', 'quantbot.operations.monitor_launch', '--learner-directory', $Directory, '--operations-database', $Database, '--commit', $Commit)
if ($EnvironmentFile) {
    if (-not [System.IO.Path]::IsPathRooted($EnvironmentFile) -or
        -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
        throw 'LEARNER_ENVIRONMENT_FILE_INVALID'
    }
    $WorkerArguments += @('--env-file', $EnvironmentFile)
}
if ($Check) { $WorkerArguments += '--check' }
& $Python @WorkerArguments
if ($null -eq $LASTEXITCODE) { throw 'MONITOR_WRAPPER_NO_EXIT_CODE' }
exit $LASTEXITCODE
