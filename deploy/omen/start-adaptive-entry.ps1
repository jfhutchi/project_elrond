[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Repository,
 [Parameter(Mandatory=$true)][string]$Config,
 [Parameter(Mandatory=$true)][string]$ConfigHash,
 [switch]$Check,
 [switch]$Exploration
)
$ErrorActionPreference='Stop'
if($ConfigHash -notmatch '^[a-f0-9]{64}$'){throw 'ADAPTIVE_ENTRY_CONFIG_PIN_REQUIRED'}
foreach($Path in @($Repository,$Config)){
 if(-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]' -or
    -not (Test-Path -LiteralPath $Path)){throw 'ADAPTIVE_ENTRY_INPUT_INVALID'}
}
$Python=Join-Path $Repository '.venv\Scripts\python.exe'
$VenvConfig=Join-Path $Repository '.venv\pyvenv.cfg'
$Homes=@(Get-Content -LiteralPath $VenvConfig | ForEach-Object{
 if($_ -match '^home\s*=\s*(.+?)\s*$'){$Matches[1]}
})
if($Homes.Count -ne 1 -or -not [IO.Path]::IsPathRooted($Homes[0]) -or
   -not (Test-Path -LiteralPath (Join-Path $Homes[0] 'python.exe'))){
 throw 'ADAPTIVE_ENTRY_NATIVE_PYTHON_REQUIRED'
}
foreach($Root in @($env:APPDATA,(Join-Path $env:LOCALAPPDATA 'Packages'))){
 if($Homes[0].StartsWith($Root+[IO.Path]::DirectorySeparatorChar,
   [StringComparison]::OrdinalIgnoreCase)){throw 'ADAPTIVE_ENTRY_HOST_VISIBLE_PYTHON_REQUIRED'}
}
$env:PYTHONPATH=Join-Path $Repository 'src'
$env:PYTHONNOUSERSITE='1'
foreach($Name in @('PYTHONHOME','PYTHONEXECUTABLE','__PYVENV_LAUNCHER__')){
 [Environment]::SetEnvironmentVariable($Name,$null,'Process')
}
. (Join-Path $PSScriptRoot 'windows-job.ps1')
$ServiceJob=New-ElrondProcessJob
$Module=if($Exploration){'quantbot.learner.exploration_launch'}else{'quantbot.learner.experiment_entry'}
$Arguments=@('-m',$Module,'--config',$Config,'--config-hash',$ConfigHash)
if($Check){$Arguments+='--check'}
$ErrorActionPreference='Continue'
$global:LASTEXITCODE=$null
& $Python @Arguments
if($null -eq $LASTEXITCODE){throw 'ADAPTIVE_ENTRY_NO_EXIT_CODE'}
exit $LASTEXITCODE
