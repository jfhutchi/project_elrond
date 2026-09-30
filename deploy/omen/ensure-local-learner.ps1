[CmdletBinding()]
param(
    [string] $LearnerTaskName = 'Elrond Learner',
    [string] $ConsoleTaskName = 'Elrond Console',
    [string] $LearnerDirectory = 'D:\Elrond\learner',
    [string] $DashboardUrl = 'http://127.0.0.1:8787/api/learner',
    [ValidateRange(5, 300)][int] $WaitSeconds = 45,
    [ValidateRange(10, 240)][int] $MaximumReceiptAgeMinutes = 30,
    [switch] $CheckOnly
)

$ErrorActionPreference = 'Stop'
$Failures = [System.Collections.Generic.List[string]]::new()
$Notes = [System.Collections.Generic.List[string]]::new()

function Add-Failure([string] $Code) {
    $script:Failures.Add($Code)
}

function Get-Task([string] $Name) {
    $Task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if ($null -eq $Task) {
        Add-Failure "TASK_MISSING:$Name"
    }
    return $Task
}

function Get-QuotedArgument([string] $Arguments, [string] $Name) {
    $Pattern = '(?i)(?:^|\s)-' + [regex]::Escape($Name) + '\s+"([^"]+)"'
    $Match = [regex]::Match($Arguments, $Pattern)
    if ($Match.Success) { return $Match.Groups[1].Value }
    return $null
}

function Get-PlainArgument([string] $Arguments, [string] $Name) {
    $Pattern = '(?i)(?:^|\s)-' + [regex]::Escape($Name) + '\s+([^\s"]+)'
    $Match = [regex]::Match($Arguments, $Pattern)
    if ($Match.Success) { return $Match.Groups[1].Value }
    return $null
}

function Test-SamePath([string] $Left, [string] $Right) {
    if (-not $Left -or -not $Right) { return $false }
    $LeftFull = [IO.Path]::GetFullPath($Left).TrimEnd('\', '/')
    $RightFull = [IO.Path]::GetFullPath($Right).TrimEnd('\', '/')
    return $LeftFull.Equals($RightFull, [StringComparison]::OrdinalIgnoreCase)
}

function Wait-ForDashboard([int] $Seconds) {
    $Deadline = (Get-Date).AddSeconds($Seconds)
    do {
        try {
            return Invoke-RestMethod -Uri $DashboardUrl -TimeoutSec 5
        } catch {
            Start-Sleep -Milliseconds 500
        }
    } while ((Get-Date) -lt $Deadline)
    return $null
}

$LearnerTask = Get-Task $LearnerTaskName
$ConsoleTask = Get-Task $ConsoleTaskName
$Repository = $null
$Database = Join-Path $LearnerDirectory 'state\learner.db'
$EnvironmentFile = $null
$PinnedCommit = $null
$ScheduleInterval = $null

if ($null -ne $LearnerTask) {
    $LearnerAction = @($LearnerTask.Actions)[0]
    if (-not (Test-Path -LiteralPath $LearnerAction.Execute -PathType Leaf)) {
        Add-Failure 'LEARNER_SERVICE_HOST_MISSING'
    }
    $Repository = Get-QuotedArgument $LearnerAction.Arguments 'Repository'
    $ConfiguredDirectory = Get-QuotedArgument $LearnerAction.Arguments 'Directory'
    $ConfiguredDatabase = Get-QuotedArgument $LearnerAction.Arguments 'Database'
    $EnvironmentFile = Get-QuotedArgument $LearnerAction.Arguments 'EnvironmentFile'
    $PinnedCommit = Get-PlainArgument $LearnerAction.Arguments 'Commit'
    $ScheduleInterval = @($LearnerTask.Triggers | ForEach-Object { $_.Repetition.Interval } |
        Where-Object { $_ } | Select-Object -First 1)

    if (-not (Test-SamePath $ConfiguredDirectory $LearnerDirectory)) {
        Add-Failure 'LEARNER_DIRECTORY_MISMATCH'
    }
    if (-not (Test-SamePath $ConfiguredDatabase $Database)) {
        Add-Failure 'LEARNER_DATABASE_MISMATCH'
    }
    if ([string]$ScheduleInterval -ne 'PT10M') { Add-Failure 'LEARNER_INTERVAL_NOT_TEN_MINUTES' }
    if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) {
        Add-Failure 'LEARNER_DATABASE_MISSING'
    }
    if (-not $EnvironmentFile -or -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) {
        Add-Failure 'LEARNER_ENVIRONMENT_FILE_MISSING'
    }
    if (-not $Repository -or -not (Test-Path -LiteralPath $Repository -PathType Container)) {
        Add-Failure 'LEARNER_RUNTIME_MISSING'
    } else {
        $Python = Join-Path $Repository '.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
            Add-Failure 'LEARNER_PYTHON_MISSING'
        }
        $RuntimeCommit = (& git --no-optional-locks -C $Repository rev-parse HEAD 2>$null)
        if ($LASTEXITCODE -ne 0 -or $RuntimeCommit -notmatch '^[0-9a-fA-F]{40}$') {
            Add-Failure 'LEARNER_RUNTIME_NOT_GIT_CHECKOUT'
        } elseif ($PinnedCommit -ne $RuntimeCommit) {
            Add-Failure 'LEARNER_RUNTIME_COMMIT_MISMATCH'
        }
        $Dirty = (& git --no-optional-locks -C $Repository status --porcelain 2>$null)
        if ($LASTEXITCODE -ne 0 -or $Dirty) { Add-Failure 'LEARNER_RUNTIME_NOT_CLEAN' }
    }
}

if ($null -ne $ConsoleTask) {
    $ConsoleAction = @($ConsoleTask.Actions)[0]
    if (-not (Test-Path -LiteralPath $ConsoleAction.Execute -PathType Leaf)) {
        Add-Failure 'CONSOLE_SERVICE_HOST_MISSING'
    }
}

if ($Failures.Count -eq 0 -and -not $CheckOnly) {
    if ([string]$ConsoleTask.State -ne 'Running') {
        Start-ScheduledTask -TaskName $ConsoleTaskName
        $Notes.Add('Started the dashboard task.')
    }

    $Before = (Get-ScheduledTaskInfo -TaskName $LearnerTaskName).LastRunTime
    if ([string]$LearnerTask.State -ne 'Running') {
        Start-ScheduledTask -TaskName $LearnerTaskName
        $Notes.Add('Started an immediate learner pass; the ten-minute schedule remains active.')
    } else {
        $Notes.Add('The learner was already running, so no duplicate pass was started.')
    }

    $Deadline = (Get-Date).AddSeconds($WaitSeconds)
    do {
        Start-Sleep -Milliseconds 500
        $LearnerTask = Get-ScheduledTask -TaskName $LearnerTaskName
        $LearnerInfo = Get-ScheduledTaskInfo -TaskName $LearnerTaskName
    } while ([string]$LearnerTask.State -eq 'Running' -and (Get-Date) -lt $Deadline)

    if ([string]$LearnerTask.State -eq 'Running') {
        Add-Failure 'LEARNER_PASS_DID_NOT_FINISH_WITHIN_WAIT'
    } elseif ($LearnerInfo.LastRunTime -le $Before) {
        Add-Failure 'LEARNER_PASS_DID_NOT_START'
    } elseif ($LearnerInfo.LastTaskResult -ne 0) {
        Add-Failure "LEARNER_PASS_EXITED:$($LearnerInfo.LastTaskResult)"
    }
}

$ReceiptPath = Join-Path $LearnerDirectory 'worker.json'
$Receipt = $null
if (-not (Test-Path -LiteralPath $ReceiptPath -PathType Leaf)) {
    Add-Failure 'LEARNER_RECEIPT_MISSING'
} else {
    try {
        $Receipt = Get-Content -LiteralPath $ReceiptPath -Raw | ConvertFrom-Json
        $FinishedAt = [DateTimeOffset]::Parse([string]$Receipt.finished_at)
        $ReceiptAge = [DateTimeOffset]::UtcNow - $FinishedAt.ToUniversalTime()
        if ($ReceiptAge.TotalMinutes -gt $MaximumReceiptAgeMinutes) {
            Add-Failure ('LEARNER_RECEIPT_STALE:{0:N1}_MINUTES' -f $ReceiptAge.TotalMinutes)
        }
        if ($Receipt.code_commit -ne $PinnedCommit) { Add-Failure 'LEARNER_RECEIPT_COMMIT_MISMATCH' }
    } catch {
        Add-Failure 'LEARNER_RECEIPT_INVALID'
    }
}

$Dashboard = Wait-ForDashboard $WaitSeconds
if ($null -eq $Dashboard) { Add-Failure 'LEARNER_DASHBOARD_UNAVAILABLE' }

$LearnerInfo = if ($null -ne $LearnerTask) {
    Get-ScheduledTaskInfo -TaskName $LearnerTaskName
} else { $null }
$ConsoleState = if ($null -ne $ConsoleTask) {
    [string](Get-ScheduledTask -TaskName $ConsoleTaskName).State
} else { 'MISSING' }

$Result = [pscustomobject]@{
    healthy = ($Failures.Count -eq 0)
    learner_task = if ($null -ne $LearnerTask) { [string]$LearnerTask.State } else { 'MISSING' }
    learner_last_run = if ($null -ne $LearnerInfo) { $LearnerInfo.LastRunTime } else { $null }
    learner_last_result = if ($null -ne $LearnerInfo) { $LearnerInfo.LastTaskResult } else { $null }
    learner_next_run = if ($null -ne $LearnerInfo) { $LearnerInfo.NextRunTime } else { $null }
    interval = [string]$ScheduleInterval
    runtime = $Repository
    commit = $PinnedCommit
    receipt_status = if ($null -ne $Receipt) { $Receipt.status } else { $null }
    captured_bars = if ($null -ne $Receipt) { $Receipt.intraday.captured_bars } else { $null }
    model_count = if ($null -ne $Receipt) { $Receipt.intraday.model_count } else { $null }
    predictions_this_run = if ($null -ne $Receipt) { $Receipt.intraday.predictions_this_run } else { $null }
    trading_enabled = if ($null -ne $Receipt) { $Receipt.trading_enabled } else { $null }
    blockers = if ($null -ne $Receipt) { @($Receipt.blockers) } else { @() }
    dashboard_task = $ConsoleState
    dashboard_status = if ($null -ne $Dashboard) { $Dashboard.status } else { 'UNAVAILABLE' }
    dashboard_url = $DashboardUrl -replace '/api/learner$', '/learner'
    notes = @($Notes)
    failures = @($Failures)
}

$Result | ConvertTo-Json -Depth 6
if ($Failures.Count -gt 0) { exit 1 }
