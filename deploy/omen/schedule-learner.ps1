[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string] $Repository,
    [Parameter(Mandatory = $true)][string] $Directory,
    [Parameter(Mandatory = $true)][string] $Commit,
    [Parameter(Mandatory = $true)][string] $ServiceHost,
    [string] $EnvironmentFile,
    [string] $TaskUser = $env:USERNAME
)
$ErrorActionPreference = 'Stop'
# Dedicated task only; never run the shared runtime installer to schedule research.
if (Get-ScheduledTask -TaskName 'Elrond Learner' -ErrorAction SilentlyContinue) {
    throw 'LEARNER_TASK_ALREADY_EXISTS_REVIEW_BEFORE_UPDATE'
}
foreach ($Path in @($Repository, $Directory, $ServiceHost)) {
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path.Contains('"') -or
        -not (Test-Path -LiteralPath $Path)) { throw 'LEARNER_TASK_PATH_INVALID' }
}
if ($Commit -notmatch '^[0-9a-f]{40}$') { throw 'LEARNER_TASK_COMMIT_INVALID' }
$Wrapper = Join-Path $Repository 'deploy\omen\start-learner.ps1'
$Database = Join-Path $Directory 'state\learner.db'
foreach ($Path in @($Wrapper, $Database)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'LEARNER_TASK_INPUT_MISSING' }
}
$Log = Join-Path $Directory 'logs\learner-launcher.log'
$Arguments = '"' + $Log + '" -NoProfile -NonInteractive -WindowStyle Hidden ' +
    '-ExecutionPolicy Bypass -File "' + $Wrapper + '" -Repository "' + $Repository +
    '" -Directory "' + $Directory + '" -Database "' + $Database + '" -Commit ' + $Commit
if ($EnvironmentFile) {
    if (-not [IO.Path]::IsPathRooted($EnvironmentFile) -or $EnvironmentFile.Contains('"') -or
        -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
        throw 'LEARNER_ENVIRONMENT_FILE_INVALID'
    }
    $Arguments += ' -EnvironmentFile "' + $EnvironmentFile + '"'
}
$Action = New-ScheduledTaskAction -Execute $ServiceHost -Argument $Arguments -WorkingDirectory $Directory
$Trigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(10)) `
    -RepetitionInterval ([TimeSpan]::FromMinutes(10))
$Logon = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(5)) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$Principal = New-ScheduledTaskPrincipal -UserId $TaskUser -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'Elrond Learner' -Action $Action -Trigger @($Trigger, $Logon) `
    -Settings $Settings -Principal $Principal -Description (
        'Managed by project_elrond deploy/omen/schedule-learner.ps1. ' +
        'Data, forecasts and outcome learning every 10 minutes; PAPER entry remains gated. Commit=' + $Commit
    ) | Out-Null
