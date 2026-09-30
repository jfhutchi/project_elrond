[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string] $Repository,
    [Parameter(Mandatory=$true)][string] $Directory,
    [Parameter(Mandatory=$true)][string] $Commit,
    [Parameter(Mandatory=$true)][string] $ExpectedOldCommit,
    [Parameter(Mandatory=$true)][string] $EnvironmentFile,
    [Parameter(Mandatory=$true)][string] $BackupDirectory,
    [switch] $Apply,
    [switch] $Rollback
)
$ErrorActionPreference = 'Stop'
$Name = 'Elrond Learner'
$Marker = 'Managed by project_elrond deploy/omen/schedule-learner.ps1.'
$Task = Get-ScheduledTask -TaskName $Name -TaskPath '\' -ErrorAction SilentlyContinue
if ($null -eq $Task -or -not ([string]$Task.Description).StartsWith($Marker) -or
    [string]$Task.State -eq 'Running' -or @($Task.Actions).Count -ne 1 -or
    [string]$Task.Principal.LogonType -ne 'Interactive' -or
    [string]$Task.Principal.RunLevel -ne 'Limited') { throw 'LEARNER_TASK_UPDATE_REFUSED' }
foreach ($Path in @($Repository,$Directory,$EnvironmentFile,$BackupDirectory)) {
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]') {
        throw 'LEARNER_UPDATE_PATH_INVALID'
    }
}
if ($Commit -notmatch '^[a-f0-9]{40}$' -or $ExpectedOldCommit -notmatch '^[a-f0-9]{40}$') {
    throw 'LEARNER_UPDATE_PIN_INVALID'
}
$Deployment = Join-Path $Directory 'deployment.json'
$Database = Join-Path $Directory 'state\learner.db'
$ManifestFile = Join-Path $BackupDirectory 'rollback.json'
$OldAction = [pscustomobject]@{
    Execute=[string]$Task.Actions[0].Execute; Arguments=[string]$Task.Actions[0].Arguments
    WorkingDirectory=[string]$Task.Actions[0].WorkingDirectory
}
$WasEnabled = [string]$Task.State -ne 'Disabled'
$OriginalPrincipal = $Task.Principal
function Write-Deployment([string] $Content) {
    $Temporary = $Deployment + '.update.tmp'
    [IO.File]::WriteAllText($Temporary,$Content,[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $Temporary -Destination $Deployment -Force
}
function Restore-Action($Saved) {
    $Action = New-ScheduledTaskAction -Execute $Saved.Execute -Argument $Saved.Arguments `
        -WorkingDirectory $Saved.WorkingDirectory
    Set-ScheduledTask -TaskName $Name -TaskPath '\' -Action $Action | Out-Null
}
function Get-Definition {
    # Compare every scheduler field except the action/enabled fields changed intentionally.
    # Registration date may be refreshed by Windows when an action is updated.
    [xml]$Definition = [string](Export-ScheduledTask -TaskName $Name -TaskPath '\')
    $Ignored = $Definition.SelectNodes('//*[local-name()="Actions"] | ' +
        '//*[local-name()="Settings"]/*[local-name()="Enabled"] | ' +
        '//*[local-name()="RegistrationInfo"]/*[local-name()="Date"]')
    foreach ($Node in @($Ignored)) { $Node.ParentNode.RemoveChild($Node) | Out-Null }
    return $Definition.OuterXml
}
$OriginalDefinition = Get-Definition
if ($Rollback) {
    $Saved = Get-Content -LiteralPath $ManifestFile -Raw | ConvertFrom-Json
    if ($Saved.Version -ne 1 -or $Saved.TaskName -ne $Name -or
        $Saved.Commit -ne $Commit -or $Saved.OldCommit -ne $ExpectedOldCommit -or
        $Saved.Status -ne 'APPLIED' -or $OldAction.Execute -ne $Saved.NewAction.Execute -or
        $OldAction.Arguments -ne $Saved.NewAction.Arguments -or
        $OldAction.WorkingDirectory -ne $Saved.NewAction.WorkingDirectory -or
        [IO.File]::ReadAllText($Deployment) -ne $Saved.NewDeployment) {
        throw 'LEARNER_ROLLBACK_DRIFT_REFUSED'
    }
    if (-not $Saved.Definition -or $OriginalDefinition -ne $Saved.Definition) {
        if ($Apply) { Disable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null }
        throw 'LEARNER_ROLLBACK_DEFINITION_DRIFT_REFUSED'
    }
    if (-not $Apply) { Write-Output 'LEARNER_ROLLBACK_PLAN_ONLY'; return }
    Disable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null
    $Current = Get-ScheduledTask -TaskName $Name -TaskPath '\'
    if ((Get-Definition) -ne $Saved.Definition -or @($Current.Actions).Count -ne 1 -or
        $Current.Actions[0].Execute -ne $Saved.NewAction.Execute -or
        $Current.Actions[0].Arguments -ne $Saved.NewAction.Arguments -or
        $Current.Actions[0].WorkingDirectory -ne $Saved.NewAction.WorkingDirectory -or
        [IO.File]::ReadAllText($Deployment) -ne $Saved.NewDeployment) {
        throw 'LEARNER_ROLLBACK_DRIFT_TASK_LEFT_DISABLED'
    }
    if ([string](Get-ScheduledTask -TaskName $Name -TaskPath '\').State -eq 'Running') {
        if ($WasEnabled) { Enable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null }
        throw 'LEARNER_TICK_STARTED_RETRY_AFTER_COMPLETION'
    }
    # Metadata/task definitions only. Never restore a database or delete new evidence.
    Restore-Action $Saved.OldAction
    Write-Deployment $Saved.OldDeployment
    $Saved.Status = 'ROLLED_BACK'
    $Saved | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
    if ($Saved.WasEnabled) { Enable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null }
    Write-Output 'LEARNER_TASK_ROLLED_BACK_EVIDENCE_RETAINED'
    return
}
if ($OldAction.Arguments -notmatch ('(?i)(?:^|\s)-Commit\s+' + $ExpectedOldCommit + '(?:\s|$)') -or
    -not (Test-Path -LiteralPath $Database -PathType Leaf) -or
    -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) -or
    (Test-Path -LiteralPath $BackupDirectory)) { throw 'LEARNER_UPDATE_INPUT_REFUSED' }
if ($OldAction.WorkingDirectory -ne $Directory -or
    $OldAction.Arguments -notmatch '-EnvironmentFile\s+"([^"]+)"' -or
    $Matches[1] -ne $EnvironmentFile) { throw 'LEARNER_UPDATE_ENVIRONMENT_CHANGE_REFUSED' }
$OldDeployment = [IO.File]::ReadAllText($Deployment)
$Document = $OldDeployment | ConvertFrom-Json
if ($Document.learner_commit -ne $ExpectedOldCommit) { throw 'LEARNER_DEPLOYMENT_PIN_MISMATCH' }
$Document.learner_commit = $Commit
$NewDeployment = $Document | ConvertTo-Json -Depth 20
$Wrapper = Join-Path $Repository 'deploy\omen\start-learner.ps1'
$Check = Join-Path $Repository 'deploy\omen\start-monitor.ps1'
foreach ($Path in @($Wrapper,$Check,$OldAction.Execute)) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'LEARNER_UPDATE_RUNTIME_MISSING' }
}
$Arguments = '"' + (Join-Path $Directory 'logs\learner-launcher.log') +
    '" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "' +
    $Wrapper + '" -Repository "' + $Repository + '" -Directory "' + $Directory +
    '" -Database "' + $Database + '" -EnvironmentFile "' + $EnvironmentFile + '" -Commit ' + $Commit
$NewAction = New-ScheduledTaskAction -Execute $OldAction.Execute -Argument $Arguments `
    -WorkingDirectory $OldAction.WorkingDirectory
if (-not $Apply) { Write-Output 'LEARNER_UPDATE_PLAN_ONLY'; return }
& $Check -Repository $Repository -Directory $Directory -Commit $Commit -Database $Database `
    -EnvironmentFile $EnvironmentFile -Check
if ($LASTEXITCODE -ne 0) { throw 'LEARNER_CANDIDATE_CHECK_FAILED' }
New-Item -ItemType Directory -Path $BackupDirectory | Out-Null
Export-ScheduledTask -TaskName $Name -TaskPath '\' | Set-Content -LiteralPath (Join-Path $BackupDirectory 'task.xml')
$Saved = [pscustomobject]@{Version=1;TaskName=$Name;Commit=$Commit;OldCommit=$ExpectedOldCommit
    Definition=$OriginalDefinition
    Status='PREPARED';WasEnabled=$WasEnabled;OldAction=$OldAction;NewAction=[pscustomobject]@{
        Execute=[string]$NewAction.Execute;Arguments=[string]$NewAction.Arguments
        WorkingDirectory=[string]$NewAction.WorkingDirectory}
    OldDeployment=$OldDeployment;NewDeployment=$NewDeployment}
$Saved | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
Disable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null
$Restored = $false
$Changed = $false
$SafeToResume = $true
try {
    $Current = Get-ScheduledTask -TaskName $Name -TaskPath '\'
    if (@($Current.Actions).Count -ne 1 -or
        $Current.Actions[0].Execute -ne $OldAction.Execute -or
        $Current.Actions[0].Arguments -ne $OldAction.Arguments -or
        $Current.Actions[0].WorkingDirectory -ne $OldAction.WorkingDirectory -or
        $Current.Principal.UserId -ne $OriginalPrincipal.UserId -or
        $Current.Principal.LogonType -ne $OriginalPrincipal.LogonType -or
        $Current.Principal.RunLevel -ne $OriginalPrincipal.RunLevel -or
        (Get-Definition) -ne $OriginalDefinition -or
        [IO.File]::ReadAllText($Deployment) -ne $OldDeployment) {
        $SafeToResume = $false
        throw 'LEARNER_UPDATE_CONCURRENT_DRIFT'
    }
    if ([string]$Current.State -eq 'Running') {
        throw 'LEARNER_TICK_STARTED_RETRY_AFTER_COMPLETION'
    }
    $Changed = $true
    Write-Deployment $NewDeployment
    Set-ScheduledTask -TaskName $Name -TaskPath '\' -Action $NewAction | Out-Null
    $Saved.Status = 'APPLIED'
    $Saved | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
    $Restored = $true
} catch {
    try {
        if ($Changed) {
            Restore-Action $OldAction
            Write-Deployment $OldDeployment
            $Saved.Status = 'FAILED_RESTORED'
        } else {
            $Saved.Status = 'FAILED_UNCHANGED'
            if (-not $SafeToResume) { $Saved.Status = 'FAILED_DRIFT_DISABLED' }
        }
        $Saved | ConvertTo-Json -Depth 10 | Set-Content -LiteralPath $ManifestFile -Encoding UTF8
        $Restored = $SafeToResume
    } catch { throw 'LEARNER_UPDATE_ROLLBACK_FAILED_TASK_LEFT_DISABLED' }
    if (-not $SafeToResume) { throw 'LEARNER_UPDATE_DRIFT_TASK_LEFT_DISABLED' }
    throw 'LEARNER_UPDATE_FAILED_ORIGINAL_RESTORED'
} finally {
    if ($Restored -and $WasEnabled) { Enable-ScheduledTask -TaskName $Name -TaskPath '\' | Out-Null }
}
Write-Output 'LEARNER_TASK_UPDATED_NO_PROMOTION_AUTHORITY'
