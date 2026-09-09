[CmdletBinding()]
param([Parameter(Mandatory = $true)][string] $OutputDirectory)

$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) { throw 'SERVICE_HOST_OUTPUT_NOT_ABSOLUTE' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
$Repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if ($OutputDirectory -eq $Repository -or $OutputDirectory.StartsWith(
        $Repository + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'SERVICE_HOST_OUTPUT_INSIDE_CHECKOUT'
}
$Compiler = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $Compiler -PathType Leaf)) { throw 'SERVICE_HOST_COMPILER_MISSING' }
$Sources = @((Join-Path $PSScriptRoot 'process-job.cs'), (Join-Path $PSScriptRoot 'service-host.cs'))
function Get-SourceDigest([string] $Path) {
    $Stream = [IO.File]::OpenRead($Path)
    $Digest = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($Digest.ComputeHash($Stream)).Replace('-', '') }
    finally { $Stream.Dispose(); $Digest.Dispose() }
}
$Hashes = ($Sources | ForEach-Object { Get-SourceDigest $_ }) -join ''
$Hasher = [Security.Cryptography.SHA256]::Create()
try {
    $Fingerprint = [BitConverter]::ToString($Hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($Hashes))).Replace('-', '').ToLowerInvariant()
} finally { $Hasher.Dispose() }
$BuildDirectory = Join-Path $OutputDirectory $Fingerprint.Substring(0, 24)
New-Item -ItemType Directory -Path $BuildDirectory -Force | Out-Null
$Executable = Join-Path $BuildDirectory 'elrond-service-host.exe'
$DigestFile = $Executable + '.sha256'
if (Test-Path -LiteralPath $Executable -PathType Leaf) {
    if (-not (Test-Path -LiteralPath $DigestFile -PathType Leaf) -or
        (Get-Content -LiteralPath $DigestFile -Raw).Trim() -ne (Get-SourceDigest $Executable)) {
        throw 'SERVICE_HOST_BINARY_CHANGED'
    }
    Write-Output $Executable
    return
}
$Temporary = Join-Path $BuildDirectory ([Guid]::NewGuid().ToString('N') + '.exe')
$CompilerOutput = & $Compiler /nologo /target:winexe /optimize+ "/out:$Temporary" $Sources 2>&1
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Temporary -PathType Leaf)) {
    throw ('SERVICE_HOST_BUILD_FAILED: ' + ($CompilerOutput -join ' '))
}
Move-Item -LiteralPath $Temporary -Destination $Executable
(Get-SourceDigest $Executable) | Set-Content -LiteralPath $DigestFile
Write-Output $Executable
