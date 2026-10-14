[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string] $Repository,
    [Parameter(Mandatory=$true)][string] $Config,
    [Parameter(Mandatory=$true)][string] $Manifest,
    [Parameter(Mandatory=$true)][ValidateSet('trader','guardian','outcomes')][string] $Role,
    [switch] $SubmissionDisabled,
    [switch] $EntryDisabled
)
$ErrorActionPreference = 'Stop'
foreach ($TargetPath in @($Repository, $Config, $Manifest)) {
    if (-not [IO.Path]::IsPathRooted($TargetPath)) { throw 'PAPER_CORE_ABSOLUTE_PATH_REQUIRED' }
}
$Python = Join-Path $Repository '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'PAPER_CORE_RUNTIME_MISSING' }
$env:PYTHONPATH = Join-Path $Repository 'src'
$env:PYTHONNOUSERSITE = '1'
[Environment]::SetEnvironmentVariable('PYTHONHOME', $null, 'Process')
. (Join-Path $PSScriptRoot 'windows-job.ps1')
$CoreJob = New-ElrondProcessJob
$ErrorActionPreference = 'Continue'
$CoreArguments = @('-B','-m','quantbot.paper_core','--config',$Config,'--manifest',$Manifest,'--role',$Role)
if ($SubmissionDisabled) { $CoreArguments += '--submission-disabled' }
if ($EntryDisabled) { $CoreArguments += '--entry-disabled' }
& $Python @CoreArguments
if ($null -eq $LASTEXITCODE) { throw 'PAPER_CORE_NO_EXIT_CODE' }
exit $LASTEXITCODE
