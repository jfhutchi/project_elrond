# Read-only local Omen metadata. No task/process mutation; no command-line or secret dump.
# Redirect stdout to a NEW file, then name it explicitly in the inventory manifest.
$ErrorActionPreference = 'Stop'
$tasks = @(Get-ScheduledTask | Where-Object TaskName -Like 'Elrond*' | Sort-Object TaskName | ForEach-Object {
    $arguments = ($_.Actions | ForEach-Object Arguments) -join ' '
    $pins = [regex]::Matches($arguments, '\b[a-f0-9]{40}\b') | ForEach-Object Value | Sort-Object -Unique
    [ordered]@{name=$_.TaskName;state=[string]$_.State;release=($pins -join ',')}
})
$processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'python.exe' -and $_.CommandLine -match 'quantbot'
} | Sort-Object ProcessId | ForEach-Object {
    [ordered]@{pid=$_.ProcessId;created_at=$_.CreationDate.ToUniversalTime().ToString('o');executable=$_.ExecutablePath}
})
[ordered]@{observed_at=[DateTime]::UtcNow.ToString('o');tasks=$tasks;processes=$processes} |
    ConvertTo-Json -Depth 6
