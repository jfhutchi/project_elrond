"""Win32 primitives the Windows sandbox is built from, and nothing else (#21).

Kept apart from `windows.py` so the policy decisions and the `ctypes` surface can be read
separately. Everything here is a thin binding: it translates a call and raises `OSError` on
failure. Whether a failure should stop a run is decided one level up, where the contract lives.

Two primitives carry the isolation, both available to an unelevated user on Windows 10/11 with no
Docker, no WSL and no pywin32:

* **AppContainer.** A token with an AppContainer SID and no capabilities fails the access check
  against any object whose ACL does not name it. That makes the filesystem default-deny and makes
  network access a kernel refusal rather than an in-process monkeypatch.
* **Job Object.** Memory, CPU and process-count ceilings the kernel imposes at the moment the
  child asks, plus termination that reaches the whole process tree at once.

`pywin32` is deliberately not a dependency. It would be a second way to do this and a third thing
to keep installed on a host whose whole problem was missing prerequisites.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_WINDOWS = sys.platform == "win32"

if _WINDOWS:
    from ctypes import wintypes
else:  # pragma: no cover - exercised by every non-Windows collection of the test suite
    # `ctypes.wintypes` refuses to import off Windows, which would make this module -- and so
    # every test that names it -- uncollectable on the Linux CI runner. The structures below are
    # plain `ctypes` once the type aliases exist, so supplying the aliases keeps the whole file
    # importable and leaves only the actual syscalls unavailable. Tests then skip on a measured
    # `windows_backend_available()` rather than on an import error, which is the difference
    # between a suite that reports the backend absent and one that never mentions it.
    wintypes = types.SimpleNamespace(  # type: ignore[assignment]
        BOOL=ctypes.c_int,
        DWORD=ctypes.c_uint32,
        HANDLE=ctypes.c_void_p,
        LARGE_INTEGER=ctypes.c_longlong,
        LPCWSTR=ctypes.c_wchar_p,
        LPWSTR=ctypes.c_wchar_p,
        ULONG=ctypes.c_ulong,
        WORD=ctypes.c_uint16,
    )


class _Unavailable:
    """Stands in for a Windows DLL off Windows, and refuses rather than pretending.

    Every entry point raises `OSError`, which is what the callers above already treat as "this
    host cannot provide the isolation". A stub that returned plausible zeros would let
    `windows_backend_available()` answer True on a machine with no AppContainer at all.
    """

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attribute: str) -> Any:
        raise OSError(f"{self._name}.{attribute} is not available on {sys.platform}")


if _WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _userenv = ctypes.WinDLL("userenv", use_last_error=True)
else:  # pragma: no cover - see above
    _kernel32 = _Unavailable("kernel32")  # type: ignore[assignment]
    _advapi32 = _Unavailable("advapi32")  # type: ignore[assignment]
    _userenv = _Unavailable("userenv")  # type: ignore[assignment]

# --- process creation -------------------------------------------------------------------------

EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
#: The child is created suspended so it can be put in the job before it runs a single
#: instruction. Without this there is a window in which it could spawn a grandchild that the job
#: never contains, which would make `ActiveProcessLimit` and tree termination advisory.
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400

STARTF_USESTDHANDLES = 0x00000100

PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
CREATE_ALWAYS = 2
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102

ERROR_ALREADY_EXISTS_HRESULT = 0x800700B7
#: What the kernel reports when a job ceiling refuses the request rather than the process failing
#: on its own terms: a CPU end-of-job termination and a blocked `CreateProcess` both land here.
ERROR_NOT_ENOUGH_QUOTA = 1816

# --- job object -------------------------------------------------------------------------------

JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_JobObjectExtendedLimitInformation = 9

PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", ctypes.c_void_p),
        ("bInheritHandle", wintypes.BOOL),
    ]


class SECURITY_CAPABILITIES(ctypes.Structure):
    _fields_ = [
        ("AppContainerSid", ctypes.c_void_p),
        ("Capabilities", ctypes.c_void_p),
        ("CapabilityCount", wintypes.DWORD),
        ("Reserved", wintypes.DWORD),
    ]


class STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


# Prototypes, declared only where the DLLs exist. `ctypes` defaults every argument to int-
# sized and every return to `int`, which silently truncates a 64-bit HANDLE; declaring them
# is correctness on this platform rather than documentation.
if _WINDOWS:  # pragma: no branch - the alternative is covered by the stub above
    _kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOEXW),
        ctypes.POINTER(PROCESS_INFORMATION),
    ]
    _kernel32.CreateProcessW.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _kernel32.WaitForSingleObject.restype = wintypes.DWORD
    _kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    _kernel32.ResumeThread.restype = wintypes.DWORD
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _userenv.CreateAppContainerProfile.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _userenv.CreateAppContainerProfile.restype = ctypes.c_long
    _userenv.DeriveAppContainerSidFromAppContainerName.argtypes = [
        wintypes.LPCWSTR,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _userenv.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
    _userenv.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
    _userenv.DeleteAppContainerProfile.restype = ctypes.c_long

    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL


def _raise(operation: str) -> None:
    raise ctypes.WinError(ctypes.get_last_error(), f"{operation} failed")


@dataclass(frozen=True, slots=True)
class AppContainer:
    """A named AppContainer identity, and the SID an ACL must name to let it in."""

    name: str
    sid_string: str
    #: The raw `PSID`. Held for the process lifetime rather than freed per use: it is handed to
    #: `CreateProcessW` through a structure the kernel reads at launch, and a SID freed while a
    #: launch is in flight is a use-after-free in somebody else's address space.
    sid: int


#: Created once and reused. A fresh profile per run would leave one registry entry per experiment
#: and would change the SID every time, so the interpreter cache ACL could never be granted ahead
#: of the run it is needed for.
DEFAULT_CONTAINER_NAME = "elrond-research-sandbox"

_containers: dict[str, AppContainer] = {}


def app_container(name: str = DEFAULT_CONTAINER_NAME) -> AppContainer:
    """Create the profile if it is absent, derive its SID either way.

    Idempotent on purpose. `CreateAppContainerProfile` returns `ALREADY_EXISTS` on every run after
    the first, which is the normal path rather than an error.
    """
    cached = _containers.get(name)
    if cached is not None:
        return cached
    sid = ctypes.c_void_p()
    result = _userenv.CreateAppContainerProfile(
        name, name, "Elrond research sandbox", None, 0, ctypes.byref(sid)
    ) & 0xFFFFFFFF
    if result == ERROR_ALREADY_EXISTS_HRESULT:
        result = _userenv.DeriveAppContainerSidFromAppContainerName(
            name, ctypes.byref(sid)
        ) & 0xFFFFFFFF
    if result != 0 or not sid:
        raise OSError(f"could not obtain an AppContainer SID for {name!r} (hr=0x{result:08X})")
    text = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        _raise("ConvertSidToStringSid")
    container = AppContainer(name=name, sid_string=str(text.value), sid=int(sid.value or 0))
    _containers[name] = container
    return container


def delete_app_container_profile(name: str) -> None:
    """Remove the profile. Used by tests that create a throwaway identity, not by `run`."""
    _containers.pop(name, None)
    _userenv.DeleteAppContainerProfile(name)


def grant_path(path: Path, sid_string: str, rights: str) -> None:
    """Add an inheritable ACE for `sid_string` on `path`.

    Uses `icacls` rather than building a DACL by hand. The alternative is roughly two hundred
    lines of `SetEntriesInAclW`/`SetNamedSecurityInfoW` for a result the shipped tool already
    produces correctly, and an ACL assembled by hand is exactly the kind of code whose bugs are
    silent grants.

    Raises on failure, because a workspace the child cannot reach and a workspace with no ACE look
    identical until the run fails somewhere less obvious.
    """
    completed = subprocess.run(  # noqa: S603 - fixed executable, arguments are not shell-parsed
        ["icacls", str(path), "/grant", f"*{sid_string}:(OI)(CI)({rights})", "/T", "/Q"],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if completed.returncode != 0:
        raise OSError(
            f"could not grant {rights} on {path} to {sid_string}: "
            f"{completed.stdout.strip()} {completed.stderr.strip()}"
        )


class JobObject:
    """A kernel-enforced resource boundary around a process and everything it starts."""

    def __init__(
        self,
        *,
        memory_bytes: int,
        active_process_limit: int,
        cpu_seconds: float | None = None,
    ) -> None:
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            _raise("CreateJobObject")
        self._handle: int | None = handle
        self._memory_bytes = memory_bytes
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        flags = (
            JOB_OBJECT_LIMIT_PROCESS_MEMORY
            | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
        )
        info.ProcessMemoryLimit = memory_bytes
        info.BasicLimitInformation.ActiveProcessLimit = active_process_limit
        if cpu_seconds is not None:
            flags |= JOB_OBJECT_LIMIT_JOB_TIME
            # 100-nanosecond units, which is what every time field in this API speaks.
            info.BasicLimitInformation.PerJobUserTimeLimit = int(cpu_seconds * 10_000_000)
        info.BasicLimitInformation.LimitFlags = flags
        if not _kernel32.SetInformationJobObject(
            handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            self.close()
            _raise("SetInformationJobObject")

    @property
    def handle(self) -> int:
        if self._handle is None:
            raise OSError("job object is closed")
        return self._handle

    def assign(self, pid: int) -> None:
        """Put a process in the job. Called while the process is still suspended."""
        process = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not process:
            _raise("OpenProcess")
        try:
            if not _kernel32.AssignProcessToJobObject(self.handle, process):
                _raise("AssignProcessToJobObject")
        finally:
            _kernel32.CloseHandle(process)

    def terminate(self, exit_code: int = 1) -> None:
        """Kill every process in the job at once, which is the whole tree by construction."""
        if self._handle is not None:
            _kernel32.TerminateJobObject(self._handle, exit_code)

    def peak_memory_bytes(self) -> int | None:
        """Highest memory the kernel charged to a process in this job.

        Measured rather than polled, so a child that lived entirely between two polls is still
        accounted for. Returns None when the query fails, on the same reasoning `runner.py` uses:
        a gap is more honest than a zero nobody measured.
        """
        if self._handle is None:
            return None
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        returned = wintypes.DWORD(0)
        if not _kernel32.QueryInformationJobObject(
            self._handle,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
            ctypes.byref(returned),
        ):
            return None
        return int(info.PeakProcessMemoryUsed)

    def close(self) -> None:
        """Close the handle, which kills anything still running under `KILL_ON_JOB_CLOSE`."""
        if self._handle is not None:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def __enter__(self) -> JobObject:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _inheritable_file(path: Path, access: int, disposition: int) -> int:
    attributes = SECURITY_ATTRIBUTES()
    attributes.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
    attributes.lpSecurityDescriptor = None
    attributes.bInheritHandle = True
    handle = _kernel32.CreateFileW(
        str(path),
        access,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        ctypes.byref(attributes),
        disposition,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        _raise(f"CreateFile({path})")
    return int(handle)


@dataclass(frozen=True, slots=True)
class LaunchedProcess:
    handle: int
    pid: int


def launch(
    command: str,
    *,
    cwd: Path,
    environment: dict[str, str],
    container: AppContainer,
    job: JobObject,
    stdout_path: Path,
    stderr_path: Path,
) -> LaunchedProcess:
    """Start `command` inside `container`, inside `job`, with output redirected to files.

    The order is load-bearing: created suspended, assigned to the job, then resumed. A process
    that ran before assignment could have spawned a grandchild outside every ceiling.

    Output goes to files rather than pipes because a pipe needs a draining thread to avoid
    deadlocking on a child that writes more than the buffer, and the extra machinery buys nothing
    here: the child can write whatever it likes into either, and the facts that matter -- exit
    code, termination, peak memory -- come from the kernel rather than from what the child said.

    Handle inheritance is restricted to exactly the three standard handles. Without that, every
    inheritable handle in the parent crosses into the sandbox, and the parent of a research run is
    a process that may well be holding the durable store open. An inherited handle is usable
    without passing an access check, so this closes a route the ACL cannot see.
    """
    stdin_handle = _inheritable_file(Path("NUL"), GENERIC_READ, OPEN_EXISTING)
    stdout_handle = _inheritable_file(stdout_path, GENERIC_WRITE, CREATE_ALWAYS)
    stderr_handle = _inheritable_file(stderr_path, GENERIC_WRITE, CREATE_ALWAYS)

    attribute_count = 2
    size = ctypes.c_size_t(0)
    _kernel32.InitializeProcThreadAttributeList(None, attribute_count, 0, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    attributes = ctypes.cast(buffer, ctypes.c_void_p)
    if not _kernel32.InitializeProcThreadAttributeList(
        attributes, attribute_count, 0, ctypes.byref(size)
    ):
        _raise("InitializeProcThreadAttributeList")

    capabilities = SECURITY_CAPABILITIES()
    capabilities.AppContainerSid = ctypes.c_void_p(container.sid)
    capabilities.Capabilities = None
    capabilities.CapabilityCount = 0
    capabilities.Reserved = 0

    handle_list = (wintypes.HANDLE * 3)(
        wintypes.HANDLE(stdin_handle),
        wintypes.HANDLE(stdout_handle),
        wintypes.HANDLE(stderr_handle),
    )

    try:
        if not _kernel32.UpdateProcThreadAttribute(
            attributes,
            0,
            ctypes.c_size_t(PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES),
            ctypes.byref(capabilities),
            ctypes.sizeof(capabilities),
            None,
            None,
        ):
            _raise("UpdateProcThreadAttribute(SECURITY_CAPABILITIES)")
        if not _kernel32.UpdateProcThreadAttribute(
            attributes,
            0,
            ctypes.c_size_t(PROC_THREAD_ATTRIBUTE_HANDLE_LIST),
            ctypes.byref(handle_list),
            ctypes.sizeof(handle_list),
            None,
            None,
        ):
            _raise("UpdateProcThreadAttribute(HANDLE_LIST)")

        startup = STARTUPINFOEXW()
        startup.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
        startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput = stdin_handle
        startup.StartupInfo.hStdOutput = stdout_handle
        startup.StartupInfo.hStdError = stderr_handle
        startup.lpAttributeList = attributes

        block = "".join(f"{key}={value}\0" for key, value in environment.items()) + "\0"
        environment_block = ctypes.create_unicode_buffer(block)

        information = PROCESS_INFORMATION()
        created = _kernel32.CreateProcessW(
            None,
            ctypes.create_unicode_buffer(command),
            None,
            None,
            True,
            EXTENDED_STARTUPINFO_PRESENT
            | CREATE_NO_WINDOW
            | CREATE_SUSPENDED
            | CREATE_UNICODE_ENVIRONMENT,
            ctypes.byref(environment_block),
            str(cwd),
            ctypes.byref(startup),
            ctypes.byref(information),
        )
        if not created:
            _raise("CreateProcess")
        try:
            job.assign(information.dwProcessId)
            if _kernel32.ResumeThread(information.hThread) == 0xFFFFFFFF:
                _raise("ResumeThread")
        except OSError:
            _kernel32.TerminateProcess(information.hProcess, 1)
            _kernel32.CloseHandle(information.hThread)
            _kernel32.CloseHandle(information.hProcess)
            raise
        _kernel32.CloseHandle(information.hThread)
        return LaunchedProcess(handle=int(information.hProcess), pid=int(information.dwProcessId))
    finally:
        _kernel32.DeleteProcThreadAttributeList(attributes)
        for handle in (stdin_handle, stdout_handle, stderr_handle):
            _kernel32.CloseHandle(handle)


def wait(handle: int, timeout_seconds: float) -> bool:
    """Wait for a process. True if it exited, False if the deadline arrived first."""
    milliseconds = max(0, int(timeout_seconds * 1000))
    status = _kernel32.WaitForSingleObject(handle, milliseconds)
    if status == WAIT_OBJECT_0:
        return True
    if status == WAIT_TIMEOUT:
        return False
    _raise("WaitForSingleObject")
    raise AssertionError("unreachable")


def exit_code(handle: int) -> int:
    code = wintypes.DWORD()
    if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
        _raise("GetExitCodeProcess")
    # Exit codes cross the Win32 boundary unsigned; Python callers expect the signed reading a
    # process actually returned, so 0xC0000005 arrives as a negative number rather than as four
    # billion. Values below 2^31 are unaffected.
    value = int(code.value)
    return value - 0x100000000 if value >= 0x80000000 else value


def close_handle(handle: int) -> None:
    _kernel32.CloseHandle(handle)


__all__ = [
    "AppContainer",
    "DEFAULT_CONTAINER_NAME",
    "ERROR_NOT_ENOUGH_QUOTA",
    "JobObject",
    "LaunchedProcess",
    "app_container",
    "close_handle",
    "delete_app_container_profile",
    "exit_code",
    "grant_path",
    "launch",
    "wait",
]
