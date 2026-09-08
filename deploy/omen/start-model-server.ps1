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
# children. Put this process and everything it launches in a job whose process tree is killed
# when this wrapper's only non-inheritable job handle closes.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class ElrondModelJob
{
    private const int JobObjectExtendedLimitInformation = 9;
    private const uint JobObjectLimitKillOnJobClose = 0x00002000;

    [StructLayout(LayoutKind.Sequential)]
    private struct BasicLimitInformation
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoCounters
    {
        public ulong ReadOperationCount;
        public ulong WriteOperationCount;
        public ulong OtherOperationCount;
        public ulong ReadTransferCount;
        public ulong WriteTransferCount;
        public ulong OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ExtendedLimitInformation
    {
        public BasicLimitInformation BasicLimitInformation;
        public IoCounters IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string name);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(
        IntPtr job,
        int informationClass,
        ref ExtendedLimitInformation information,
        uint informationLength);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    public static IntPtr OwnCurrentProcess()
    {
        IntPtr job = CreateJobObject(IntPtr.Zero, null);
        if (job == IntPtr.Zero)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Unable to create model job");

        ExtendedLimitInformation information = new ExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
        if (!SetInformationJobObject(
                job,
                JobObjectExtendedLimitInformation,
                ref information,
                (uint)Marshal.SizeOf(typeof(ExtendedLimitInformation))))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Unable to configure model job");

        if (!AssignProcessToJobObject(job, GetCurrentProcess()))
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Unable to own model process tree");
        return job;
    }
}
'@
$ModelJob = [ElrondModelJob]::OwnCurrentProcess()

# Windows PowerShell 5.1 wraps ordinary native stderr (including INFO logs) as
# NativeCommandError. It must not turn a healthy foreground server into a failed task.
$ErrorActionPreference = 'Continue'
$global:LASTEXITCODE = $null
& $Executable serve 1>> (Join-Path $LogDirectory 'model-server.stdout.log') 2>> (Join-Path $LogDirectory 'model-server.stderr.log')
if ($null -eq $LASTEXITCODE) { throw 'Model executable did not return a native exit code' }
exit $LASTEXITCODE
