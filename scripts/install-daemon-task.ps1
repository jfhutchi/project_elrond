<#
.SYNOPSIS
    Register (or remove) a Windows scheduled task that keeps the QuantBot daemon running.

.DESCRIPTION
    The daemon schedules itself from the broker's own market clock, waking five minutes
    after each close, so it skips weekends and holidays without any calendar logic here.
    What it cannot do is survive a reboot, which is what this task provides.

    Deliberately triggered at logon rather than on a clock schedule. A time-based task
    firing on a market holiday would find data older than the staleness threshold, halt,
    and engage the durable kill switch, requiring a manual clear. Letting the daemon own
    the timing avoids that entirely.

    Credentials are read from the user environment, so the task must run as the same user
    who set them. No secrets are stored in this file or in the task definition.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-daemon-task.ps1
    powershell -ExecutionPolicy Bypass -File scripts\install-daemon-task.ps1 -Uninstall
#>

[CmdletBinding()]
param(
    [string] $TaskName = "QuantBotDaemon",
    [switch] $Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$LogDirectory = Join-Path $RepoRoot 'logs'
$LogFile = Join-Path $LogDirectory 'daemon.log'

$existing = $null
try {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    $existing = $null
}

if ($Uninstall) {
    if ($null -eq $existing) {
        Write-Host "No task named '$TaskName' is registered." -ForegroundColor Yellow
        exit 0
    }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    Write-Host "A daemon already running continues until it is stopped or the machine reboots."
    exit 0
}

foreach ($name in @('ALPACA_PAPER_API_KEY', 'ALPACA_PAPER_API_SECRET', 'EXPECTED_ACCOUNT_ID')) {
    $value = [Environment]::GetEnvironmentVariable($name, 'User')
    if ([string]::IsNullOrWhiteSpace($value)) {
        Write-Host "User environment variable $name is not set." -ForegroundColor Red
        Write-Host "The task would start and immediately fail closed. Set it, then re-run."
        exit 1
    }
}

$uv = (Get-Command uv -ErrorAction SilentlyContinue)
if ($null -eq $uv) {
    Write-Host "uv was not found on PATH; the task would have nothing to run." -ForegroundColor Red
    exit 1
}

if (-not (Test-Path $LogDirectory)) {
    New-Item -ItemType Directory -Path $LogDirectory | Out-Null
}

# -u keeps Python unbuffered so the log is useful while the daemon is still running.
$command = "Set-Location '$RepoRoot'; & '$($uv.Source)' run quantbot daemon *>> '$LogFile'"
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -WindowStyle Hidden -Command `"$command`"" `
    -WorkingDirectory $RepoRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Days 0)

if ($null -ne $existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Replaced the existing '$TaskName' task."
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "QuantBot paper-trading daemon (broker-clock scheduled)" | Out-Null

Write-Host "Registered scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "  runs as        $env:USERNAME at logon"
Write-Host "  working dir    $RepoRoot"
Write-Host "  log            $LogFile"
Write-Host ""
Write-Host "Start it now without waiting for a logon:"
Write-Host "  Start-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Only one writer is permitted. Do not run 'quantbot run-once' while this is"
Write-Host "active; the single-writer lock will refuse the second process."
