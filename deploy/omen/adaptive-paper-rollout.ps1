[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Repository,
 [Parameter(Mandatory=$true)][string]$Config,
 [Parameter(Mandatory=$true)][string]$Commit,
 [Parameter(Mandatory=$true)][string]$ServiceHost,
 [Parameter(Mandatory=$true)][string]$BackupDirectory,
 [switch]$Apply
)
$ErrorActionPreference='Stop'
$Name='Elrond Adaptive Paper Shadow'
$Marker='Managed by project_elrond deploy/omen/adaptive-paper-rollout.ps1.'
foreach($Path in @($Repository,$Config,$ServiceHost,$BackupDirectory)){
 if(-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]'){
  throw 'ADAPTIVE_ROLLOUT_PATH_INVALID'
 }
}
if($Commit -notmatch '^[a-f0-9]{40}$'){throw 'ADAPTIVE_PIN_REQUIRED'}
$Existing=Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue
if($null -ne $Existing){throw 'ADAPTIVE_TASK_ALREADY_EXISTS_NO_IMPLICIT_UPDATE'}
$Learner=Get-ScheduledTask -TaskName 'Elrond Learner' -TaskPath '\'
$Description=[string]$Learner.Description
# This exact legacy description is on the previously authorized Omen learner.
# It identifies only that installation, not arbitrary task descriptions or commits.
$KnownLegacyDescription='Native intraday data and guarded learning pass every 10 minutes; '+
 'PAPER entry requires approved deployment. Commit=3fdfce992686342749c5da0ae65f7e58bb3b7d67'
$Managed=$Description.StartsWith('Managed by project_elrond deploy/omen/schedule-learner.ps1.') -or
 $Description -ceq $KnownLegacyDescription
if(-not $Managed -or
 [string]$Learner.Principal.LogonType -ne 'Interactive' -or
 [string]$Learner.Principal.RunLevel -ne 'Limited' -or
 @($Learner.Actions).Count -ne 1 -or $Learner.Actions[0].Execute -ne $ServiceHost){
 throw 'ADAPTIVE_EXISTING_LIMITED_PRINCIPAL_REQUIRED'
}
try {
 $TaskOwner=[string]$Learner.Principal.UserId
 if($TaskOwner -match '^S-1-'){
  $TaskOwnerSid=([System.Security.Principal.SecurityIdentifier]$TaskOwner).Value
 } else {
  $TaskOwnerSid=([System.Security.Principal.NTAccount]$TaskOwner).Translate(
   [System.Security.Principal.SecurityIdentifier]).Value
 }
 $CurrentOwnerSid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
 if($TaskOwnerSid -ne $CurrentOwnerSid){throw 'owner mismatch'}
} catch {throw 'ADAPTIVE_EXISTING_CURRENT_OWNER_REQUIRED'}
$Wrapper=Join-Path $Repository 'deploy\omen\start-adaptive-paper.ps1'
$Python=Join-Path $Repository '.venv\Scripts\python.exe'
foreach($Path in @($Repository,$Config,$ServiceHost,$Wrapper,$Python)){
 if(-not (Test-Path -LiteralPath $Path)){throw 'ADAPTIVE_ROLLOUT_SOURCE_MISSING'}
}
$env:PYTHONPATH=Join-Path $Repository 'src'
$env:PYTHONNOUSERSITE='1'
$Preflight=@('-m','quantbot.operations.experiment_rollout','--config',$Config,
 '--commit',$Commit,'--backup',$BackupDirectory)
if($Apply){$Preflight+='--write-backup'}
& $Python @Preflight
if($LASTEXITCODE -ne 0){throw 'ADAPTIVE_SHADOW_PREFLIGHT_REFUSED'}
if(-not $Apply){Write-Output 'ADAPTIVE_SHADOW_PLAN_ONLY_NO_TASK_CHANGES';return}
# Back up only task definition and validated nonsecret configuration; no credential copy.
Export-ScheduledTask -TaskName 'Elrond Learner' -TaskPath '\' |
 Set-Content -LiteralPath (Join-Path $BackupDirectory 'existing-learner-task.xml') -Encoding utf8
$Root=(Get-Content -LiteralPath $Config -Raw | ConvertFrom-Json).root
$ConfigHash=(Get-Content -LiteralPath (Join-Path $BackupDirectory 'rollback.json') -Raw |
 ConvertFrom-Json).config_hash
if($ConfigHash -notmatch '^[a-f0-9]{64}$'){throw 'ADAPTIVE_BACKUP_PIN_INVALID'}
$Log=Join-Path $Root 'logs\adaptive-launcher.log'
New-Item -ItemType Directory -Path (Split-Path $Log -Parent) -Force | Out-Null
$Arguments='"'+$Log+'" -NoProfile -NonInteractive -WindowStyle Hidden -File "'+$Wrapper+
 '" -Repository "'+$Repository+'" -Config "'+$Config+'" -ConfigHash '+$ConfigHash
$Action=New-ScheduledTaskAction -Execute $ServiceHost -Argument $Arguments -WorkingDirectory $Root
$Once=New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1))
$Logon=New-ScheduledTaskTrigger -AtLogOn -User $Learner.Principal.UserId
$Settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
 -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval ([TimeSpan]::FromMinutes(1)) `
 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $Name -TaskPath '\' -Action $Action -Trigger @($Once,$Logon) `
 -Settings $Settings -Principal $Learner.Principal -Description "$Marker Commit=$Commit" | Out-Null
Export-ScheduledTask -TaskName $Name -TaskPath '\' |
 Set-Content -LiteralPath (Join-Path $BackupDirectory 'shadow-task.xml') -Encoding utf8
Write-Output 'ADAPTIVE_SHADOW_REGISTERED_NO_TRADING_AUTHORITY'
