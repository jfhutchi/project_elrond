$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot '..\..\..\deploy\omen\wait-retained-service.ps1')
$script:states=@('Ready','Running');$script:reads=0
function Get-ScheduledTask { param($TaskName,$ErrorAction) $state=$script:states[[Math]::Min($script:reads,$script:states.Count-1)];$script:reads++; [pscustomobject]@{State=$state;Actions=@([pscustomobject]@{Arguments='C:\pinned 123'})} }
function Start-Sleep { param($Seconds) if($Seconds -ne 2) { throw 'UNBOUNDED_RETRY' } }
Wait-ElrondRetainedService -Name test -Repository C:\pinned -Commit 123
if($script:reads -ne 2) { throw 'RECOVERY_NOT_OBSERVED' }
foreach($case in @(@('Disabled','RETAINED_SERVICE_DISABLED'),@('Ready','RETAINED_SERVICE_NOT_READY'))) {
    $script:states=@($case[0]);$script:reads=0
    try { Wait-ElrondRetainedService -Name test -Repository C:\pinned -Commit 123 -TimeoutSeconds 0;throw 'EXPECTED_REFUSAL' } catch { if($_.Exception.Message -cne $case[1]) { throw } }
}
try { Wait-ElrondRetainedService -Name test -Repository C:\wrong -Commit 123;throw 'EXPECTED_REFUSAL' } catch { if($_.Exception.Message -cne 'RETAINED_SERVICE_SOURCE_CHANGED') { throw } }
'4 retained-service gate cases passed'
