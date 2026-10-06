[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Repository,
 [Parameter(Mandatory=$true)][string]$Directory,
 [Parameter(Mandatory=$true)][string]$LearnerDirectory,
 [Parameter(Mandatory=$true)][string]$Commit,
 [Parameter(Mandatory=$true)][string]$EnvironmentFile,
 [switch]$Check
)
$ErrorActionPreference='Stop'
foreach($Path in @($Repository,$Directory,$LearnerDirectory,$EnvironmentFile)){
 if(-not [IO.Path]::IsPathRooted($Path) -or -not (Test-Path -LiteralPath $Path)){
  throw 'POSITION_RESEARCH_ABSOLUTE_EXISTING_PATH_REQUIRED'
 }
}
if($Commit -notmatch '^[a-f0-9]{40}$'){throw 'POSITION_RESEARCH_PIN_REQUIRED'}
$Source=Join-Path $Repository 'src'
$Python=Join-Path $Repository '.venv\Scripts\python.exe'
$VenvConfig=Join-Path $Repository '.venv\pyvenv.cfg'
$Homes=@(Get-Content -LiteralPath $VenvConfig | ForEach-Object{
 if($_ -match '^home\s*=\s*(.+?)\s*$'){$Matches[1]}
})
if($Homes.Count -ne 1 -or -not [IO.Path]::IsPathRooted($Homes[0]) -or
 -not (Test-Path -LiteralPath (Join-Path $Homes[0] 'python.exe'))){
 throw 'POSITION_RESEARCH_NATIVE_PYTHON_REQUIRED'
}
foreach($Root in @($env:APPDATA,(Join-Path $env:LOCALAPPDATA 'Packages'))){
 if($Homes[0].StartsWith($Root+[IO.Path]::DirectorySeparatorChar,
   [StringComparison]::OrdinalIgnoreCase)){throw 'POSITION_RESEARCH_HOST_VISIBLE_PYTHON_REQUIRED'}
}
$env:PYTHONPATH=$Source
$env:PYTHONNOUSERSITE='1'
[Environment]::SetEnvironmentVariable('PYTHONHOME',$null,'Process')
[Environment]::SetEnvironmentVariable('PYTHONEXECUTABLE',$null,'Process')
[Environment]::SetEnvironmentVariable('__PYVENV_LAUNCHER__',$null,'Process')
. (Join-Path $PSScriptRoot 'windows-job.ps1')
$ServiceJob=New-ElrondProcessJob
$WorkerArguments=@('-m','quantbot.operations.position_research_launch','--directory',$Directory,
 '--learner-directory',$LearnerDirectory,'--env-file',$EnvironmentFile,'--commit',$Commit)
if($Check){$WorkerArguments+='--check'}
$ErrorActionPreference='Continue'
$global:LASTEXITCODE=$null
& $Python @WorkerArguments
if($null -eq $LASTEXITCODE){throw 'POSITION_RESEARCH_NO_EXIT_CODE'}
exit $LASTEXITCODE
