function Wait-ElrondRetainedService {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$Name,
        [Parameter(Mandatory=$true)][string]$Repository,
        [Parameter(Mandatory=$true)][string]$Commit,
        [ValidateRange(0,120)][int]$TimeoutSeconds=60
    )
    $deadline=[DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $task=Get-ScheduledTask -TaskName $Name -ErrorAction Stop
        if ($task.Actions.Count -ne 1 -or
            -not $task.Actions.Arguments.Contains($Repository) -or
            -not $task.Actions.Arguments.Contains($Commit)) {
            throw 'RETAINED_SERVICE_SOURCE_CHANGED'
        }
        if ([string]$task.State -eq 'Running') { return }
        if ([string]$task.State -eq 'Disabled') { throw 'RETAINED_SERVICE_DISABLED' }
        if ([DateTime]::UtcNow -ge $deadline) { throw 'RETAINED_SERVICE_NOT_READY' }
        # The scheduler may be between a failed instance and its configured restart.
        # Waiting grants no authority and never enables or starts a disabled service.
        Start-Sleep -Seconds 2
    } while ($true)
}
