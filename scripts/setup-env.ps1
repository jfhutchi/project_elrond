<#
.SYNOPSIS
    Prompt for QuantBot's paper-trading environment and set it at Windows User scope.

.DESCRIPTION
    Collects Alpaca paper credentials, verifies them against the live paper endpoint,
    derives the account UUID that every safety gate compares against, and persists the
    configuration for the current user.

    Secrets are read as SecureString so they never appear on screen or in PSReadLine
    history. They are still stored in plaintext in the user environment block, which is
    what the application reads; treat this machine accordingly and revoke the keys from
    the Alpaca dashboard if that is ever not acceptable.

.NOTES
    Windows PowerShell 5.1 compatible. Contains no secrets and is safe to commit.
#>

[CmdletBinding()]
param(
    [switch] $SkipVerification
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$RepoRoot = Split-Path -Parent $PSScriptRoot

function Read-Secret {
    param([string] $Prompt)
    while ($true) {
        $secure = Read-Host -Prompt $Prompt -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        if ([string]::IsNullOrWhiteSpace($plain)) {
            Write-Host "  Value cannot be empty." -ForegroundColor Yellow
            continue
        }
        return $plain.Trim()
    }
}

function Read-WithDefault {
    param([string] $Prompt, [string] $Default)
    $entered = Read-Host -Prompt "$Prompt [$Default]"
    if ([string]::IsNullOrWhiteSpace($entered)) { return $Default }
    return $entered.Trim()
}

function Read-YesNo {
    param([string] $Prompt, [bool] $Default = $false)
    if ($Default) { $hint = 'Y/n' } else { $hint = 'y/N' }
    while ($true) {
        $entered = Read-Host -Prompt "$Prompt [$hint]"
        if ([string]::IsNullOrWhiteSpace($entered)) { return $Default }
        switch -Regex ($entered.Trim()) {
            '^(y|yes)$' { return $true }
            '^(n|no)$'  { return $false }
            default     { Write-Host "  Answer y or n." -ForegroundColor Yellow }
        }
    }
}

function Set-UserVariable {
    param([string] $Name, [string] $Value)
    [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
    Set-Item -Path "env:$Name" -Value $Value
}

Write-Host ''
Write-Host 'QuantBot paper-trading environment setup' -ForegroundColor Cyan
Write-Host "Repository root: $RepoRoot"
Write-Host ''
Write-Host 'Generate keys from the PAPER dashboard at app.alpaca.markets/paper.'
Write-Host 'Live credentials are rejected by the paper endpoint and will fail below.'
Write-Host ''

$apiKey = Read-Secret -Prompt 'Alpaca paper API key ID'
$apiSecret = Read-Secret -Prompt 'Alpaca paper API secret key'

$accountId = $null
if (-not $SkipVerification) {
    Write-Host ''
    Write-Host 'Verifying credentials against https://paper-api.alpaca.markets/v2/account ...'
    $headers = @{
        'APCA-API-KEY-ID'     = $apiKey
        'APCA-API-SECRET-KEY' = $apiSecret
    }
    try {
        $account = Invoke-RestMethod -Uri 'https://paper-api.alpaca.markets/v2/account' `
            -Headers $headers -Method Get -TimeoutSec 30 -ErrorAction Stop
    } catch {
        Write-Host ''
        Write-Host 'Credential verification FAILED.' -ForegroundColor Red
        Write-Host "  $($_.Exception.Message)"
        Write-Host '  403 usually means these are live keys, not paper keys.'
        Write-Host '  Re-run once you have keys from the paper dashboard.'
        exit 1
    }

    $accountId = $account.id
    Write-Host ''
    Write-Host 'Credentials verified.' -ForegroundColor Green
    Write-Host "  Account UUID    : $accountId"
    Write-Host "  Account number  : $($account.account_number)"
    Write-Host "  Status          : $($account.status)"
    Write-Host "  Equity          : $($account.equity) $($account.currency)"
    if ($account.status -ne 'ACTIVE') {
        Write-Host '  Account is not ACTIVE; order entry will be blocked.' -ForegroundColor Yellow
    }
    Write-Host ''
    if (-not (Read-YesNo -Prompt 'Is this the account QuantBot should trade?' -Default $true)) {
        Write-Host 'Aborted; nothing was changed.' -ForegroundColor Yellow
        exit 1
    }
} else {
    Write-Host ''
    Write-Host 'Skipping verification. Enter the account UUID from GET /v2/account,'
    Write-Host 'not the PA-prefixed account number shown in the dashboard.'
    $accountId = Read-Host -Prompt 'Expected account UUID'
    $accountId = $accountId.Trim()
}

if ($accountId -match '^PA') {
    Write-Host ''
    Write-Host 'That looks like an account NUMBER, not the account UUID.' -ForegroundColor Red
    Write-Host 'Every order gate would fail with ACCOUNT_MISMATCH. Aborted.'
    exit 1
}

Write-Host ''
Write-Host 'Durable state locations (absolute paths keep one ledger no matter where you run).'
$databasePath = Read-WithDefault -Prompt 'Database path' -Default (Join-Path $RepoRoot 'quantbot.db')
$lockPath = Read-WithDefault -Prompt 'Single-writer lock path' -Default (Join-Path $RepoRoot 'quantbot.lock')
$reportsDir = Read-WithDefault -Prompt 'Reports directory' -Default (Join-Path $RepoRoot 'reports')
$configPath = Read-WithDefault -Prompt 'Strategy config' -Default (Join-Path $RepoRoot 'config\strategy-v1.yaml')

Write-Host ''
Write-Host 'Readiness assertions' -ForegroundColor Cyan
Write-Host 'These four flags plus KILL_SWITCH gate every order. They are operator'
Write-Host 'assertions, not measurements: answering yes before doctor, sync-data and'
Write-Host 'reconcile have all succeeded defeats the fail-closed design. Answer no now'
Write-Host 're-run this script (or set them individually) once those three pass.'
Write-Host ''
$assertReady = Read-YesNo -Prompt 'Assert broker/data/risk/reconciliation healthy and release the kill switch?' -Default $false

Write-Host ''
Write-Host 'Writing user environment variables ...'

Set-UserVariable 'BROKER_PROVIDER'      'alpaca'
Set-UserVariable 'TRADING_MODE'         'PAPER'
Set-UserVariable 'BROKER_ENVIRONMENT'   'PAPER'
Set-UserVariable 'MARKET_DATA_PROVIDER' 'alpaca'
Set-UserVariable 'LIVE_TRADING_ACKNOWLEDGED' 'false'

Set-UserVariable 'ALPACA_PAPER_API_KEY'    $apiKey
Set-UserVariable 'ALPACA_PAPER_API_SECRET' $apiSecret
Set-UserVariable 'EXPECTED_ACCOUNT_ID'     $accountId

Set-UserVariable 'QUANTBOT_CONFIG'      $configPath
Set-UserVariable 'QUANTBOT_DB_PATH'     $databasePath
Set-UserVariable 'QUANTBOT_LOCK_PATH'   $lockPath
Set-UserVariable 'QUANTBOT_REPORTS_DIR' $reportsDir
# A paper-only Alpaca account is entitled to IEX data only.
Set-UserVariable 'QUANTBOT_MARKET_DATA_FEED'       'iex'
Set-UserVariable 'QUANTBOT_MARKET_DATA_ADJUSTMENT' 'all'
Set-UserVariable 'QUANTBOT_MAX_DATA_AGE_SECONDS'   '86400'

if ($assertReady) {
    Set-UserVariable 'BROKER_HEALTHY'            'true'
    Set-UserVariable 'MARKET_DATA_HEALTHY'       'true'
    Set-UserVariable 'RISK_ENGINE_HEALTHY'       'true'
    Set-UserVariable 'RECONCILIATION_SUCCESSFUL' 'true'
    Set-UserVariable 'KILL_SWITCH'               'false'
} else {
    Set-UserVariable 'BROKER_HEALTHY'            'false'
    Set-UserVariable 'MARKET_DATA_HEALTHY'       'false'
    Set-UserVariable 'RISK_ENGINE_HEALTHY'       'false'
    Set-UserVariable 'RECONCILIATION_SUCCESSFUL' 'false'
    Set-UserVariable 'KILL_SWITCH'               'true'
}

Write-Host ''
Write-Host 'Done. Set for this session and for every new process you start.' -ForegroundColor Green
Write-Host "  EXPECTED_ACCOUNT_ID = $accountId"
Write-Host '  ALPACA_PAPER_API_KEY / ALPACA_PAPER_API_SECRET = (set, not displayed)'
Write-Host "  QUANTBOT_DB_PATH    = $databasePath"
if ($assertReady) {
    Write-Host '  Readiness assertions = TRUE, process KILL_SWITCH released' -ForegroundColor Yellow
} else {
    Write-Host '  Readiness assertions = FALSE, process KILL_SWITCH engaged'
}
Write-Host ''
Write-Host 'Already-running programs keep their old environment. Restart any open'
Write-Host 'terminal, editor, or agent session before it will see these.'
Write-Host ''
Write-Host 'Next:'
Write-Host "  cd $RepoRoot"
Write-Host '  uv run quantbot doctor'
Write-Host '  uv run quantbot sync-data'
Write-Host '  uv run quantbot reconcile'
if (-not $assertReady) {
    Write-Host ''
    Write-Host 'Then re-run this script and answer yes to the readiness question, followed by:'
    Write-Host '  uv run quantbot kill-switch clear --reason "all paper gates verified"'
    Write-Host '  uv run quantbot run-once'
}
Write-Host ''
