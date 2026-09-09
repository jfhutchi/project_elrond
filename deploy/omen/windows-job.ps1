# Shared Windows process-tree ownership for Task Scheduler wrappers.
# The returned handle stays rooted in the caller. When Task Scheduler terminates the PowerShell
# action, Windows closes the handle and kills every descendant still in the job.
if (-not ('ElrondProcessJob' -as [type])) {
    Add-Type -Path (Join-Path $PSScriptRoot 'process-job.cs')
}

function New-ElrondProcessJob {
    return [ElrondProcessJob]::OwnCurrentProcess()
}
