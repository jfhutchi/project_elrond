$ErrorActionPreference = 'Stop'
$Evidence = 'D:\Elrond\review\whole-share-20261015'
$NewRepository = 'D:\Elrond\releases\support-whole-share-20261015'
$NewCommit = '383c54867d1643e3d591762f14db266054c54cad'
$Task = Get-ScheduledTask -TaskName 'Elrond Protection'
if ($Task.Actions.Count -ne 1) {throw 'Unexpected protection task action count'}
$Action = $Task.Actions[0]
$OldRepository = 'D:\Elrond\releases\support-readiness-20261015'
$OldCommit = 'ab6f6109b7a0c133571025a0d532568de133ee76'
if (-not $Action.Arguments.Contains($OldRepository) -or -not $Action.Arguments.Contains($OldCommit)) {
    throw 'Protection predecessor mismatch'
}
$Head = git -C $NewRepository rev-parse HEAD
$Dirty = git -C $NewRepository status --porcelain --untracked-files=all
if ($Head -ne $NewCommit -or $Dirty) {throw 'Support source not clean at expected commit'}
if ((Get-ScheduledTask -TaskName 'Elrond Paper Core Guardian').State -ne 'Running' -or
    (Get-ScheduledTask -TaskName 'Elrond Paper Deadline').State -ne 'Running') {
    throw 'Core guardian and retained deadline must remain running'
}
Export-ScheduledTask -TaskName 'Elrond Protection' | Set-Content -LiteralPath (Join-Path $Evidence 'private-protection-task-before.xml') -Encoding utf8
$NewArguments = $Action.Arguments.Replace($OldRepository,$NewRepository).Replace($OldCommit,$NewCommit)
$NewAction = New-ScheduledTaskAction -Execute $Action.Execute -Argument $NewArguments -WorkingDirectory $NewRepository
Stop-ScheduledTask -TaskName 'Elrond Protection'
$Until = [DateTime]::UtcNow.AddSeconds(20)
do {
    $Remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'quantbot\.operations\.service' -and
        $_.CommandLine.Contains($OldCommit) -and $_.CommandLine -match '-+component protection'
    })
    if ($Remaining.Count -eq 0) {break}
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $Until)
if ($Remaining.Count) {throw 'Old protection process remains'}
Set-ScheduledTask -TaskName 'Elrond Protection' -Action $NewAction | Out-Null
Start-ScheduledTask -TaskName 'Elrond Protection'
[ordered]@{at=[DateTime]::UtcNow.ToString('o');old_repository=$OldRepository;new_repository=$NewRepository;
    source_commit=$NewCommit;clean_source=$true;environment_unchanged=$true;database_unchanged=$true;
    old_processes_remaining=0;core_guardian_retained=$true;retained_deadline_running=$true} |
    ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Evidence 'protection-deployment.json') -Encoding utf8
Write-Output 'Retained protection started with position-certification bridge; core guardian and deadline retained.'
