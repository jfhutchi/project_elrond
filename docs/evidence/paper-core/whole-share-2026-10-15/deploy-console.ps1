$ErrorActionPreference = 'Stop'
$Evidence = 'D:\Elrond\review\whole-share-20261015'
$NewRepository = 'D:\Elrond\releases\dashboard-whole-share-381787e-20261015'
$NewCommit = '381787e28a7f02843c6f1df233934e9b58e065ee'
$Task = Get-ScheduledTask -TaskName 'Elrond Console'
if ($Task.Actions.Count -ne 1) {throw 'Unexpected console task action count'}
$Action = $Task.Actions[0]
$OldRepository = 'D:\Elrond\releases\dashboard-bfe6e47-20261015'
$OldCommit = '53866617274a205c164325eb3578f8d830b4d719'
if (-not $Action.Arguments.Contains($OldRepository) -or -not $Action.Arguments.Contains($OldCommit)) {
    throw 'Console predecessor mismatch'
}
$Head = git -C $NewRepository rev-parse HEAD
$Dirty = git -C $NewRepository status --porcelain --untracked-files=all
if ($Head -ne $NewCommit -or $Dirty) {throw 'Console source not clean at expected commit'}
Export-ScheduledTask -TaskName 'Elrond Console' | Set-Content -LiteralPath (Join-Path $Evidence 'private-console-task-before.xml') -Encoding utf8
$NewArguments = $Action.Arguments.Replace($OldRepository,$NewRepository).Replace($OldCommit,$NewCommit)
$ActionParameters = @{Execute=$Action.Execute; Argument=$NewArguments}
if ($Action.WorkingDirectory) {$ActionParameters.WorkingDirectory=$Action.WorkingDirectory}
$NewAction = New-ScheduledTaskAction @ActionParameters
Stop-ScheduledTask -TaskName 'Elrond Console'
$Until = [DateTime]::UtcNow.AddSeconds(20)
do {
    $Remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'quantbot\.operations\.service' -and
        $_.CommandLine.Contains($OldCommit) -and $_.CommandLine -match '-+component console'
    })
    if ($Remaining.Count -eq 0) {break}
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $Until)
if ($Remaining.Count) {throw 'Old console process remains'}
Set-ScheduledTask -TaskName 'Elrond Console' -Action $NewAction | Out-Null
Start-ScheduledTask -TaskName 'Elrond Console'
[ordered]@{at=[DateTime]::UtcNow.ToString('o'); old_repository=$OldRepository; new_repository=$NewRepository;
    source_commit=$NewCommit; clean_source=$true; environment_unchanged=$true; database_unchanged=$true;
    old_processes_remaining=0} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Evidence 'console-deployment.json') -Encoding utf8
Write-Output 'Compatible console started; environment and database unchanged.'
