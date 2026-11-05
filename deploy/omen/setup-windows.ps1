[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $EnvironmentFile,

    [Parameter(Mandatory = $true)]
    [string] $Database,

    [ValidateSet('Interactive', 'Boot')]
    [string] $Mode = 'Interactive',

    [System.Management.Automation.PSCredential] $Credential,

    [switch] $UpdateManaged
)

$ErrorActionPreference = 'Stop'
$ManagedMarker = 'Managed by project_elrond deploy/omen/setup-windows.ps1.'
$Repository = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$Python = Join-Path $Repository '.venv\Scripts\python.exe'
$PowerShell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$ServiceWrapper = Join-Path $PSScriptRoot 'start-service.ps1'
$JobHelper = Join-Path $PSScriptRoot 'windows-job.ps1'
$RuntimeHelper = Join-Path $PSScriptRoot 'runtime-bootstrap.ps1'
$VenvConfig = Join-Path $Repository '.venv\pyvenv.cfg'

function Refuse([string] $Code) {
    throw "Elrond installer refused: $Code"
}

function Test-FullyQualifiedWindowsPath([string] $Value) {
    return $Value -match '^[A-Za-z]:[\\/]' -or $Value -match '^\\\\[^\\]+\\[^\\]+'
}

if (-not (Test-FullyQualifiedWindowsPath $EnvironmentFile) -or
    -not (Test-FullyQualifiedWindowsPath $Database)) {
    Refuse 'ABSOLUTE_PATHS_REQUIRED'
}
$EnvironmentFile = [System.IO.Path]::GetFullPath($EnvironmentFile)
$Database = [System.IO.Path]::GetFullPath($Database)
if (-not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf)) { Refuse 'ENV_FILE_NOT_FOUND' }
if (-not (Test-Path -LiteralPath $Database -PathType Leaf)) { Refuse 'DATABASE_NOT_FOUND' }
if (-not (Test-Path -LiteralPath $Python -PathType Leaf) -or
    -not (Test-Path -LiteralPath $VenvConfig -PathType Leaf) -or
    -not (Test-Path -LiteralPath $PowerShell -PathType Leaf) -or
    -not (Test-Path -LiteralPath $ServiceWrapper -PathType Leaf) -or
    -not (Test-Path -LiteralPath $JobHelper -PathType Leaf) -or
    -not (Test-Path -LiteralPath $RuntimeHelper -PathType Leaf)) {
    Refuse 'SERVICE_RUNTIME_NOT_FOUND'
}
$VenvHomes = @(
    Get-Content -LiteralPath $VenvConfig | ForEach-Object {
        if ($_ -match '^home\s*=\s*(.+?)\s*$') { $Matches[1] }
    }
)
if ($VenvHomes.Count -ne 1 -or -not [System.IO.Path]::IsPathRooted($VenvHomes[0]) -or
    $VenvHomes[0].Contains('"')) {
    Refuse 'VENV_HOME_INVALID'
}
$BasePython = Join-Path $VenvHomes[0] 'python.exe'
if (-not (Test-Path -LiteralPath $BasePython -PathType Leaf)) {
    Refuse 'VENV_BASE_PYTHON_NOT_FOUND'
}
foreach ($InvisibleRoot in @($env:APPDATA, (Join-Path $env:LOCALAPPDATA 'Packages'))) {
    if ($VenvHomes[0].StartsWith(
            $InvisibleRoot + [System.IO.Path]::DirectorySeparatorChar,
            [System.StringComparison]::OrdinalIgnoreCase)) {
        Refuse 'VENV_BASE_NOT_HOST_VISIBLE'
    }
}

$Commit = (& git --no-optional-locks -C $Repository rev-parse --verify 'HEAD^{commit}' 2>$null)
if ($LASTEXITCODE -ne 0 -or $Commit -notmatch '^[0-9a-fA-F]{40}$') {
    Refuse 'CHECKOUT_UNAVAILABLE'
}
$Dirty = (& git --no-optional-locks -C $Repository status --porcelain --untracked-files=all 2>$null)
if ($LASTEXITCODE -ne 0) { Refuse 'CHECKOUT_UNAVAILABLE' }
if ($Dirty) { Refuse 'CHECKOUT_NOT_CLEAN' }

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
if ($Mode -eq 'Boot') {
    $Principal = New-Object System.Security.Principal.WindowsPrincipal(
        [System.Security.Principal.WindowsIdentity]::GetCurrent()
    )
    if (-not $Principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Refuse 'BOOT_MODE_REQUIRES_ELEVATION'
    }
    if ($null -eq $Credential) { Refuse 'BOOT_MODE_REQUIRES_CREDENTIAL' }
    $TaskUser = $Credential.UserName
} else {
    if ($null -ne $Credential) { Refuse 'CREDENTIAL_ONLY_VALID_FOR_BOOT_MODE' }
    $TaskUser = $CurrentUser
}

$Definitions = @(
    [pscustomobject]@{ Name = 'Elrond Console'; Component = 'console'; Minutes = 5; Daemon = $true },
    [pscustomobject]@{ Name = 'Elrond Daemon'; Component = 'daemon'; Minutes = 5; Daemon = $true },
    [pscustomobject]@{ Name = 'Elrond Protection'; Component = 'protection'; Minutes = 5; Daemon = $true },
    [pscustomobject]@{ Name = 'Elrond Ingestion'; Component = 'ingestion'; Minutes = 30; Daemon = $false },
    [pscustomobject]@{ Name = 'Elrond Research'; Component = 'research'; Minutes = 360; Daemon = $false },
    [pscustomobject]@{ Name = 'Elrond Report'; Component = 'report'; Minutes = 1440; Daemon = $false }
)

# Validate every exact action before creating any task. Exit 3 means the common configuration is
# valid but this broker-dependent component has no PAPER credential pair, so it is installed
# disabled rather than being made to retry a condition only the operator can resolve.
$Enabled = @{}
foreach ($Definition in $Definitions) {
    $SavedErrorPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $null = (& $Python -m quantbot.operations.service `
        --env-file $EnvironmentFile --commit $Commit --database $Database `
        --component $Definition.Component --check 2>$null)
    $CheckExitCode = $LASTEXITCODE
    $ErrorActionPreference = $SavedErrorPreference
    if ($CheckExitCode -eq 0) {
        $Enabled[$Definition.Component] = $true
    } elseif ($CheckExitCode -eq 3) {
        $Enabled[$Definition.Component] = $false
    } else {
        Refuse "SERVICE_CHECK_FAILED_$($Definition.Component.ToUpperInvariant())"
    }
}

$Existing = @{}
foreach ($Definition in $Definitions) {
    $Task = Get-ScheduledTask -TaskName $Definition.Name -ErrorAction SilentlyContinue
    if ($null -eq $Task) { continue }
    if (-not $UpdateManaged) { Refuse "TASK_EXISTS_$($Definition.Component.ToUpperInvariant())" }
    if (-not ([string]$Task.Description).StartsWith($ManagedMarker)) {
        Refuse "TASK_NOT_MANAGED_$($Definition.Component.ToUpperInvariant())"
    }
    if ([string]$Task.State -eq 'Running') {
        Refuse "RUNNING_TASK_MUST_STOP_$($Definition.Component.ToUpperInvariant())"
    }
    $Existing[$Definition.Name] = $true
}

function Quote-TaskArgument([string] $Value) {
    if ($Value.Contains('"')) { Refuse 'PATH_CONTAINS_QUOTE' }
    return '"' + $Value + '"'
}

$StateDirectory = Split-Path -Parent $Database
$ServiceHost = & (Join-Path $PSScriptRoot 'build-service-host.ps1') `
    -OutputDirectory (Join-Path $StateDirectory 'service-host')
foreach ($Definition in $Definitions) {
    $LauncherLog = Join-Path $StateDirectory ('launcher-logs\' + $Definition.Component + '.log')
    $Arguments = (Quote-TaskArgument $LauncherLog) +
        ' -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File ' +
        (Quote-TaskArgument $ServiceWrapper) + ' -Repository ' +
        (Quote-TaskArgument $Repository) + ' -EnvironmentFile ' +
        (Quote-TaskArgument $EnvironmentFile) + ' -Commit ' + $Commit + ' -Database ' +
        (Quote-TaskArgument $Database) + ' -Component ' + $Definition.Component
    $Action = New-ScheduledTaskAction -Execute $ServiceHost -Argument $Arguments `
        -WorkingDirectory $StateDirectory
    if ($Mode -eq 'Boot') {
        $PrimaryTrigger = New-ScheduledTaskTrigger -AtStartup
    } else {
        $PrimaryTrigger = New-ScheduledTaskTrigger -AtLogOn -User $TaskUser
    }
    $RetryTrigger = New-ScheduledTaskTrigger -Once -At ((Get-Date).AddMinutes(1)) `
        -RepetitionInterval ([TimeSpan]::FromMinutes($Definition.Minutes)) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
    $Settings = New-ScheduledTaskSettingsSet -RestartCount 3 `
        -RestartInterval ([TimeSpan]::FromMinutes(1)) -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $Description = "$ManagedMarker Component=$($Definition.Component); commit=$Commit"
    $Force = [bool]$Existing[$Definition.Name]
    if ($Mode -eq 'Boot') {
        $Password = $Credential.GetNetworkCredential().Password
        Register-ScheduledTask -TaskName $Definition.Name -Description $Description `
            -Action $Action -Trigger @($PrimaryTrigger, $RetryTrigger) -Settings $Settings `
            -User $TaskUser -Password $Password -RunLevel Limited -Force:$Force | Out-Null
        $Password = $null
    } else {
        $TaskPrincipal = New-ScheduledTaskPrincipal -UserId $TaskUser `
            -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $Definition.Name -Description $Description `
            -Action $Action -Trigger @($PrimaryTrigger, $RetryTrigger) -Settings $Settings `
            -Principal $TaskPrincipal -Force:$Force | Out-Null
    }
    if (-not $Enabled[$Definition.Component]) {
        Disable-ScheduledTask -TaskName $Definition.Name | Out-Null
    }
}

if ($Mode -eq 'Interactive') {
    Write-Output 'Registered current-user Interactive tasks. They start at sign-in; unattended reboot recovery is not claimed.'
} else {
    Write-Output 'Registered password-backed startup tasks at Limited run level.'
}
