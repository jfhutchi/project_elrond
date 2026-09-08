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
$Source = Join-Path $Repository 'src'
$Venv = Join-Path $Repository '.venv'
$VenvConfig = Join-Path $Venv 'pyvenv.cfg'
$Python = Join-Path $Venv 'Scripts\python.exe'
$JobHelper = Join-Path $PSScriptRoot 'windows-job.ps1'
if (-not (Test-Path -LiteralPath $VenvConfig -PathType Leaf) -or
    -not (Test-Path -LiteralPath $Python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $Source -PathType Container) -or
    -not (Test-Path -LiteralPath $JobHelper -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_RUNTIME_NOT_FOUND'
}
$Homes = @(
    Get-Content -LiteralPath $VenvConfig | ForEach-Object {
        if ($_ -match '^home\s*=\s*(.+?)\s*$') { $Matches[1] }
    }
)
if ($Homes.Count -ne 1 -or -not [System.IO.Path]::IsPathRooted($Homes[0]) -or
    $Homes[0].Contains('"')) {
    throw 'SERVICE_WRAPPER_VENV_HOME_INVALID'
}
$BasePython = Join-Path $Homes[0] 'python.exe'
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) {
    throw 'SERVICE_WRAPPER_BASE_PYTHON_NOT_FOUND'
}
$InvisibleRoots = @($env:APPDATA, (Join-Path $env:LOCALAPPDATA 'Packages'))
foreach ($Root in $InvisibleRoots) {
    if ($Homes[0].StartsWith($Root + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'SERVICE_WRAPPER_BASE_NOT_HOST_VISIBLE'
    }
}

# A packaged development environment can virtualize an interpreter under AppData so this process
# sees it while an ordinary Task Scheduler process does not. Refuse those homes above. The venv
# launcher is otherwise retained so sys.prefix and package isolation remain native Python state.
$env:PYTHONPATH = $Source
$env:PYTHONNOUSERSITE = '1'
[Environment]::SetEnvironmentVariable('PYTHONHOME', $null, 'Process')
[Environment]::SetEnvironmentVariable('PYTHONEXECUTABLE', $null, 'Process')
[Environment]::SetEnvironmentVariable('__PYVENV_LAUNCHER__', $null, 'Process')
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
