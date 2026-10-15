$ErrorActionPreference = 'Stop'
$Evidence = 'D:\Elrond\review\submission-20261015'
$Names = @('Elrond Paper Core Trader', 'Elrond Paper Core Outcomes', 'Elrond Paper Core Guardian')
$States = @()
foreach ($Name in $Names) {
    $Task = Get-ScheduledTask -TaskName $Name
    $States += [ordered]@{name=$Name; state=[string]$Task.State; enabled=$Task.Settings.Enabled}
    Disable-ScheduledTask -TaskName $Name | Out-Null
    Stop-ScheduledTask -TaskName $Name
}
$Deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    $Remaining = @(Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq 'python.exe' -and $_.CommandLine -match 'quantbot\.paper_core' -and
        $_.CommandLine -match 'paper-core-14352cc-20261015' -and $_.CommandLine -match '--role (trader|guardian|outcomes)'
    } | Select-Object ProcessId,ParentProcessId,CommandLine)
    if ($Remaining.Count -eq 0) {break}
    Start-Sleep -Milliseconds 250
} while ([DateTime]::UtcNow -lt $Deadline)
if ($Remaining.Count -ne 0) {throw 'Old core process remains; refuse executable replacement'}
$Support = @(Get-ScheduledTask -TaskName 'Elrond Protection','Elrond Paper Deadline' | Select-Object TaskName,@{n='State';e={[string]$_.State}})
if (@($Support | Where-Object {$_.State -ne 'Running'}).Count) {throw 'Retained protection must remain running'}
[ordered]@{at=[DateTime]::UtcNow.ToString('o'); prior_roles=$States; remaining_core_processes=@(); retained_support=$Support} |
    ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 (Join-Path $Evidence 'old-role-fence.json')
Write-Output 'Three core roles fenced; retained protection running.'
