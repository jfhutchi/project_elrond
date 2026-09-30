[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string] $Repository,
    [Parameter(Mandatory=$true)][string] $Directory,
    [Parameter(Mandatory=$true)][string] $Commit,
    [Parameter(Mandatory=$true)][string] $ServiceHost,
    [Parameter(Mandatory=$true)][string] $QueueDatabase,
    [Parameter(Mandatory=$true)][string] $OperationsDatabase,
    [string] $ExpectedLearnerDescription,
    [string] $ObserverDatabase,
    [string] $ObserverQueueDatabase,
    [switch] $Apply,
    [switch] $Rollback
)
$ErrorActionPreference = 'Stop'
$Name = 'Elrond Alert Dispatcher'
$Marker = 'Managed by project_elrond deploy/omen/schedule-alert-dispatch.ps1.'
foreach ($Path in @($Repository,$Directory,$ServiceHost,$QueueDatabase,$OperationsDatabase)) {
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]' -or
        -not (Test-Path -LiteralPath $Path)) { throw 'DISPATCH_INPUT_INVALID' }
}
if ($Commit -notmatch '^[a-f0-9]{40}$') { throw 'DISPATCH_PIN_INVALID' }
$Wrapper = Join-Path $Repository 'deploy\omen\start-alert-dispatch.ps1'
if (-not (Test-Path -LiteralPath $Wrapper -PathType Leaf)) { throw 'DISPATCH_WRAPPER_MISSING' }
$Learner = Get-ScheduledTask -TaskName 'Elrond Learner' -TaskPath '\' -ErrorAction SilentlyContinue
$DescriptionMatches = $null -ne $Learner -and (
    ([string]::IsNullOrEmpty($ExpectedLearnerDescription) -and
        ([string]$Learner.Description).StartsWith('Managed by project_elrond deploy/omen/schedule-learner.ps1.')) -or
    (-not [string]::IsNullOrEmpty($ExpectedLearnerDescription) -and
        [string]$Learner.Description -ceq $ExpectedLearnerDescription))
if ($null -eq $Learner -or -not $DescriptionMatches -or
    @($Learner.Actions).Count -ne 1 -or $Learner.Actions[0].Execute -ne $ServiceHost -or
    [string]$Learner.Principal.LogonType -ne 'Interactive' -or
    [string]$Learner.Principal.RunLevel -ne 'Limited') { throw 'DISPATCH_PRINCIPAL_NOT_SUPPORTED' }
$Arguments = '"' + (Join-Path $Directory 'logs\alert-dispatch-launcher.log') +
    '" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $Wrapper + '" -Repository "' + $Repository + '" -Directory "' + $Directory +
    '" -Database "' + $OperationsDatabase + '" -QueueDatabase "' + $QueueDatabase +
    '" -Commit ' + $Commit
$ObserverParameters = @{}
if ($ObserverDatabase -or $ObserverQueueDatabase) {
    foreach ($Path in @($ObserverDatabase,$ObserverQueueDatabase)) {
        if (-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]' -or
            -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'DISPATCH_OBSERVER_INPUT_INVALID' }
    }
    $Arguments += ' -ObserverDatabase "' + $ObserverDatabase + '" -ObserverQueueDatabase "' +
        $ObserverQueueDatabase + '"'
    $ObserverParameters = @{ObserverDatabase=$ObserverDatabase;ObserverQueueDatabase=$ObserverQueueDatabase}
}
$Existing = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue
if ($Rollback) {
    if ($null -eq $Existing -or [string]$Existing.State -eq 'Running' -or
        [string]$Existing.Description -ne "$Marker Commit=$Commit" -or
        @($Existing.Actions).Count -ne 1 -or $Existing.Actions[0].Execute -ne $ServiceHost -or
        $Existing.Actions[0].Arguments -ne $Arguments) { throw 'DISPATCH_ROLLBACK_REFUSED' }
    if ($Apply) { Unregister-ScheduledTask -TaskName $Name -TaskPath '\' -Confirm:$false }
    else { Write-Output 'DISPATCH_ROLLBACK_PLAN_ONLY' }
    return
}
if ($null -ne $Existing) { throw 'DISPATCH_TASK_ALREADY_EXISTS' }
if (-not $Apply) { Write-Output 'DISPATCH_INSTALL_PLAN_ONLY'; return }
& $Wrapper -Repository $Repository -Directory $Directory -Commit $Commit `
    -Database $OperationsDatabase -QueueDatabase $QueueDatabase @ObserverParameters -Check
if ($LASTEXITCODE -ne 0) { throw 'DISPATCH_LAUNCH_CHECK_FAILED' }
$Action = New-ScheduledTaskAction -Execute $ServiceHost -Argument $Arguments -WorkingDirectory $Directory
$Repeat = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(2)) `
    -RepetitionInterval ([TimeSpan]::FromMinutes(2))
$Logon = New-ScheduledTaskTrigger -AtLogOn -User $Learner.Principal.UserId
$Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::FromMinutes(1)) -StartWhenAvailable `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Name -TaskPath '\' -Action $Action -Trigger @($Repeat,$Logon) `
    -Settings $Settings -Principal $Learner.Principal -Description "$Marker Commit=$Commit" | Out-Null
Write-Output 'DISPATCH_REGISTERED_NO_EXTERNAL_DELIVERY'
