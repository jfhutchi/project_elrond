[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $Directory,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $Database,
    [Parameter(Mandatory = $true)][string] $QueueDatabase,
    [string] $ObserverDatabase,
    [string] $ObserverQueueDatabase,
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
$WorkerArguments = @('-m', 'quantbot.operations.alert_dispatch', '--queue', $QueueDatabase, '--commit', $Commit)
if (-not [IO.Path]::IsPathRooted($QueueDatabase)) { throw 'DISPATCH_QUEUE_PATH_REFUSED' }
if ($Check) { $WorkerArguments += '--check' }
else { $WorkerArguments += @('--events-database', $Database, '--send') }
& $Python @WorkerArguments
if ($null -eq $LASTEXITCODE) { throw 'DISPATCH_WRAPPER_NO_EXIT_CODE' }
$FirstExitCode = $LASTEXITCODE
if ($ObserverDatabase -or $ObserverQueueDatabase) {
    if (-not $ObserverDatabase -or -not $ObserverQueueDatabase -or
        -not [IO.Path]::IsPathRooted($ObserverDatabase) -or
        -not [IO.Path]::IsPathRooted($ObserverQueueDatabase) -or
        -not (Test-Path -LiteralPath $ObserverDatabase -PathType Leaf)) {
        throw 'DISPATCH_OBSERVER_SOURCE_REFUSED'
    }
    $ObserverArguments = @('-m', 'quantbot.operations.alert_dispatch', '--queue',
        $ObserverQueueDatabase, '--commit', $Commit)
    if ($Check) { $ObserverArguments += '--check' }
    else { $ObserverArguments += @('--events-database', $ObserverDatabase, '--send') }
    & $Python @ObserverArguments
    if ($null -eq $LASTEXITCODE) { throw 'DISPATCH_WRAPPER_NO_EXIT_CODE' }
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
if ($FirstExitCode -ne 0) { exit $FirstExitCode }
exit $LASTEXITCODE
