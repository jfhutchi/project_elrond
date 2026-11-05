[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $Directory,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $Database,
    [string] $EnvironmentFile,
    [switch] $DeadlineSupervisor,
    [switch] $CloseCapture,
    [switch] $InitializeCapture,
    [string] $OperationsDatabase,
    [switch] $Check
)

$ErrorActionPreference = 'Stop'
foreach ($Path in @($Repository, $Directory, $Database)) {
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw 'SERVICE_WRAPPER_PATH_NOT_ABSOLUTE' }
}
$Repository = [System.IO.Path]::GetFullPath($Repository)
$Directory = [System.IO.Path]::GetFullPath($Directory)
$Database = [System.IO.Path]::GetFullPath($Database)
if ($Database -ne (Join-Path $Directory 'state\learner.db')) { throw 'LEARNER_DATABASE_MISMATCH' }
if ($Commit -notmatch '^[0-9a-fA-F]{40}$') { throw 'SERVICE_WRAPPER_COMMIT_INVALID' }
if (-not (Test-Path -LiteralPath $Repository -PathType Container)) {
    throw 'SERVICE_WRAPPER_REPOSITORY_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
    throw 'LEARNER_WRAPPER_DIRECTORY_NOT_FOUND'
}
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_DATABASE_NOT_FOUND'
}
. (Join-Path $PSScriptRoot 'runtime-bootstrap.ps1') `
    -Repository $Repository -ErrorPrefix 'SERVICE_WRAPPER'
$WorkerArguments = @('-m', 'quantbot.learner.worker', '--directory', $Directory, '--commit', $Commit)
if ($CloseCapture) {
    if ($DeadlineSupervisor -or $Check -or -not $EnvironmentFile -or
        -not [System.IO.Path]::IsPathRooted($OperationsDatabase) -or
        -not (Test-Path -LiteralPath $OperationsDatabase -PathType Leaf)) {
        throw 'PAPER_CLOSE_INPUT_INVALID'
    }
    $WorkerArguments = @('-m', 'quantbot.operations.close_capture', '--commit', $Commit,
        '--database', $OperationsDatabase, '--directory', (Join-Path $Directory 'performance'))
    if ($InitializeCapture) { $WorkerArguments += '--initialize' }
} elseif ($InitializeCapture) { throw 'PAPER_CLOSE_MODE_REQUIRED' }
elseif ($DeadlineSupervisor) {
    if (-not $EnvironmentFile -or -not [System.IO.Path]::IsPathRooted($OperationsDatabase) -or
        -not (Test-Path -LiteralPath $OperationsDatabase -PathType Leaf)) {
        throw 'PAPER_DEADLINE_INPUT_INVALID'
    }
    $WorkerArguments = @('-m', 'quantbot.learner.execution_worker', '--commit', $Commit,
        '--database', $OperationsDatabase)
    if ($Check) { $WorkerArguments += '--check' }
} elseif ($OperationsDatabase -or $Check) { throw 'PAPER_DEADLINE_MODE_REQUIRED' }
if ($EnvironmentFile) {
    if (-not [System.IO.Path]::IsPathRooted($EnvironmentFile) -or
        -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
        throw 'LEARNER_ENVIRONMENT_FILE_INVALID'
    }
    $WorkerArguments += @('--env-file', $EnvironmentFile)
}
& $Python @WorkerArguments
if ($null -eq $LASTEXITCODE) { throw 'SERVICE_WRAPPER_NO_EXIT_CODE' }
exit $LASTEXITCODE
