<# Local OpenAI-compatible transport. Model weights load per request and unload afterwards.
   Task Scheduler owns this foreground process; it holds no broker environment or credentials.
   Register with the same user/session or protected boot credentials as the Elrond tasks.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string] $Executable,
    [Parameter(Mandatory=$true)][string] $Models,
    [Parameter(Mandatory=$true)][string] $LogDirectory
)
$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { throw 'Model executable missing' }
foreach ($path in @($Executable, $Models, $LogDirectory)) {
    if (-not [IO.Path]::IsPathRooted($path)) { throw 'Model runtime paths must be absolute' }
}
Get-ChildItem Env: | Where-Object {
    $_.Name -match '^(ALPACA_|QUANTBOT_|BROKER_|MARKET_DATA_|EXPECTED_ACCOUNT_ID$|KILL_SWITCH$|LIVE_TRADING_ACKNOWLEDGED$|TRADING_MODE$|OLLAMA_)'
} | ForEach-Object { [Environment]::SetEnvironmentVariable($_.Name, $null, 'Process') }
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_MODELS = $Models
$env:OLLAMA_KEEP_ALIVE = '0'
$env:OLLAMA_MAX_LOADED_MODELS = '1'
$env:OLLAMA_NUM_PARALLEL = '1'
$env:OLLAMA_CONTEXT_LENGTH = '8192'
$env:OLLAMA_NO_CLOUD = '1'
New-Item -ItemType Directory -Path $Models,$LogDirectory -Force | Out-Null

# Task Scheduler terminates its PowerShell action without necessarily terminating native
# children. The shared helper puts the complete process tree in a kill-on-close Job Object.
. (Join-Path $PSScriptRoot 'windows-job.ps1')
$ModelJob = New-ElrondProcessJob

# Windows PowerShell 5.1 wraps ordinary native stderr (including INFO logs) as
# NativeCommandError. It must not turn a healthy foreground server into a failed task.
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = $null
& $Executable serve 1>> (Join-Path $LogDirectory 'model-server.stdout.log') 2>> (Join-Path $LogDirectory 'model-server.stderr.log')
if ($null -eq $LASTEXITCODE) { throw 'Model executable did not return a native exit code' }
exit $LASTEXITCODE
