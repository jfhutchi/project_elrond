# The already commissioned service host owns the process job before launching PowerShell.
# Normal commands invoke only the pinned native Python CLI; no script-file/policy override.
function New-ElrondAdaptiveNativeCommand {
 [CmdletBinding()]
 param(
  [Parameter(Mandatory=$true)][string]$Repository,
  [Parameter(Mandatory=$true)][string]$Config,
  [Parameter(Mandatory=$true)][string]$ConfigHash,
  [switch]$Check
 )
 foreach($Path in @($Repository,$Config)){
  if(-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\r\n]'){
   throw 'ADAPTIVE_INPUT_INVALID'
  }
 }
 if($ConfigHash -notmatch '^[a-f0-9]{64}$'){throw 'ADAPTIVE_CONFIG_PIN_REQUIRED'}
 $Prefix='$Repository=' + "'" + $Repository.Replace("'","''") + "';" +
  '$Config=' + "'" + $Config.Replace("'","''") + "';" +
  '$ConfigHash=' + "'" + $ConfigHash + "';" +
  '$Check=' + $(if($Check){'$true;'}else{'$false;'})
 $Body=@'
$ErrorActionPreference='Stop'
$Python=Join-Path $Repository '.venv\Scripts\python.exe'
$VenvConfig=Join-Path $Repository '.venv\pyvenv.cfg'
foreach($Path in @($Repository,$Config,$Python,$VenvConfig)){
 if(-not (Test-Path -LiteralPath $Path)){throw 'ADAPTIVE_INPUT_INVALID'}
}
$Hasher=[Security.Cryptography.SHA256]::Create()
$Stream=[IO.File]::OpenRead($Config)
try {$ActualHash=[BitConverter]::ToString($Hasher.ComputeHash($Stream)).Replace('-','').ToLowerInvariant()}
finally {$Stream.Dispose();$Hasher.Dispose()}
if($ActualHash -cne $ConfigHash){
 throw 'ADAPTIVE_CONFIG_PIN_MISMATCH'
}
$Homes=@(Get-Content -LiteralPath $VenvConfig | ForEach-Object{
 if($_ -match '^home\s*=\s*(.+?)\s*$'){$Matches[1]}
})
if($Homes.Count -ne 1 -or -not [IO.Path]::IsPathRooted($Homes[0]) -or
 -not (Test-Path -LiteralPath (Join-Path $Homes[0] 'python.exe'))){
 throw 'ADAPTIVE_NATIVE_PYTHON_REQUIRED'
}
$AppData=[Environment]::GetFolderPath('ApplicationData')
$LocalData=[Environment]::GetFolderPath('LocalApplicationData')
if(-not $AppData -or -not $LocalData){throw 'ADAPTIVE_USER_PROFILE_METADATA_REQUIRED'}
foreach($Root in @($AppData,(Join-Path $LocalData 'Packages'))){
 if($Homes[0].StartsWith($Root+[IO.Path]::DirectorySeparatorChar,
  [StringComparison]::OrdinalIgnoreCase)){throw 'ADAPTIVE_HOST_VISIBLE_PYTHON_REQUIRED'}
}
$env:PYTHONPATH=Join-Path $Repository 'src'
$env:PYTHONNOUSERSITE='1'
foreach($Name in @('PYTHONHOME','PYTHONEXECUTABLE','__PYVENV_LAUNCHER__')){
 [Environment]::SetEnvironmentVariable($Name,$null,'Process')
}
$Arguments=@('-m','quantbot.learner.experiment_launch','--config',$Config,'--config-hash',$ConfigHash)
if($Check){$Arguments+='--check'}
$ErrorActionPreference='Continue'
$global:LASTEXITCODE=$null
& $Python @Arguments
if($null -eq $LASTEXITCODE){throw 'ADAPTIVE_NO_EXIT_CODE'}
exit $LASTEXITCODE
'@
 $Command=$Prefix+$Body
 if($Command.Contains('"')){throw 'ADAPTIVE_NATIVE_COMMAND_QUOTING_REQUIRED'}
 return $Command
}
