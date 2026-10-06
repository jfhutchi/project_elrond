[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Commit,
 [Parameter(Mandatory=$true)][string]$BackupDirectory,
 [switch]$Apply
)
$ErrorActionPreference='Stop'
$Name='Elrond Adaptive Paper Shadow'
if($Commit -notmatch '^[a-f0-9]{40}$' -or -not [IO.Path]::IsPathRooted($BackupDirectory)){
 throw 'ADAPTIVE_ROLLBACK_INPUT_INVALID'
}
$Saved=Join-Path $BackupDirectory 'shadow-task.xml'
if(-not (Test-Path -LiteralPath $Saved -PathType Leaf)){throw 'ADAPTIVE_TASK_BACKUP_MISSING'}
$Task=Get-ScheduledTask -TaskName $Name -TaskPath '\'
$Marker='Managed by project_elrond deploy/omen/adaptive-paper-rollout.ps1.'
if([string]$Task.Description -ne "$Marker Commit=$Commit" -or
 [string]$Task.Principal.LogonType -ne 'Interactive' -or
 [string]$Task.Principal.RunLevel -ne 'Limited') {throw 'ADAPTIVE_ROLLBACK_IDENTITY_MISMATCH'}
[xml]$Before=Get-Content -LiteralPath $Saved -Raw
[xml]$Current=Export-ScheduledTask -TaskName $Name -TaskPath '\'
if($Before.Task.Actions.OuterXml -ne $Current.Task.Actions.OuterXml){
 throw 'ADAPTIVE_ROLLBACK_ACTION_MISMATCH'
}
if(-not $Apply){Write-Output 'ADAPTIVE_ROLLBACK_PLAN_ONLY_PRESERVE_EVIDENCE';return}
# Only this new shadow task is stopped. Core protection and all databases are untouched.
if([string]$Task.State -eq 'Running'){Stop-ScheduledTask -TaskName $Name -TaskPath '\'}
Unregister-ScheduledTask -TaskName $Name -TaskPath '\' -Confirm:$false
Write-Output 'ADAPTIVE_SHADOW_REMOVED_ALL_DATABASES_AND_NEW_EVIDENCE_PRESERVED'
