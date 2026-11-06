"""Bounded CI subprocesses, including descendants which inherit the output pipe."""

from __future__ import annotations

import ctypes
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, TextIO


class CIProcessError(RuntimeError):
    """A command failed, timed out, or could not be contained."""


class CIProcessTimeout(CIProcessError):
    """The contained command reached its deadline; cleanup runs before propagation."""


class _Job(Protocol):
    def contain_and_resume(self, pid: int) -> None: ...
    def close(self) -> None: ...


if sys.platform == "win32":
    from ctypes import wintypes

    class _BasicLimits(ctypes.Structure):
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

    class _Limits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD), ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG), ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    class _WindowsJob:
        def __init__(self) -> None:
            self.api = ctypes.WinDLL("kernel32", use_last_error=True)
            signatures = {
                "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
                "SetInformationJobObject": (
                    [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL,
                ),
                "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
                "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
                "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
                "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(_ThreadEntry)], wintypes.BOOL),
                "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
                "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
                "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
            }
            for name, (arguments, result) in signatures.items():
                function = getattr(self.api, name)
                function.argtypes = arguments
                function.restype = result
            self.handle = self.api.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            limits = _Limits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
            if not self.api.SetInformationJobObject(
                self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits),
            ):
                error = ctypes.WinError(ctypes.get_last_error())
                self.close()
                raise error

        def contain_and_resume(self, pid: int) -> None:
            # The process is suspended before user code runs, so no child can escape assignment.
            handle = self.api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                if not self.api.AssignProcessToJobObject(self.handle, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
            finally:
                self.api.CloseHandle(handle)
            snapshot = self.api.CreateToolhelp32Snapshot(0x00000004, 0)
            if snapshot == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                entry = _ThreadEntry()
                entry.dwSize = ctypes.sizeof(entry)
                available = self.api.Thread32First(snapshot, ctypes.byref(entry))
                while available:
                    if entry.th32OwnerProcessID == pid:
                        thread = self.api.OpenThread(0x0002, False, entry.th32ThreadID)
                        if not thread:
                            raise ctypes.WinError(ctypes.get_last_error())
                        try:
                            if self.api.ResumeThread(thread) == 0xFFFFFFFF:
                                raise ctypes.WinError(ctypes.get_last_error())
                            return
                        finally:
                            self.api.CloseHandle(thread)
                    available = self.api.Thread32Next(snapshot, ctypes.byref(entry))
                raise CIProcessError("COMMAND_CONTAINMENT_FAILED: suspended thread not found")
            finally:
                self.api.CloseHandle(snapshot)

        def close(self) -> None:
            if self.handle:
                self.api.CloseHandle(self.handle)
                self.handle = None


def _kill_group(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        # Also handles a process whose job assignment failed while it was still suspended.
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The complete process group has already exited.


def _create_job() -> _Job | None:
    if sys.platform == "win32":
        return _WindowsJob()
    else:
        return None


def run_command(
    command: Sequence[str], cwd: Path, env: Mapping[str, str], log: TextIO,
    *, timeout: float = 1800, capture: TextIO | None = None,
) -> None:
    line = "+ " + " ".join(command)
    print(line, flush=True)
    log.write(line + "\n")
    log.flush()
    deadline = time.monotonic() + timeout
    job = _create_job()
    process: subprocess.Popen[str] | None = None
    reader: threading.Thread | None = None
    output: queue.Queue[str | OSError | None] = queue.Queue()

    def terminate() -> None:
        if job is not None:
            job.close()
        if process is not None:
            _kill_group(process)

    try:
        process = subprocess.Popen(
            command, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", start_new_session=os.name != "nt",
            creationflags=0x00000004 if sys.platform == "win32" else 0,  # CREATE_SUSPENDED
        )
        if job is not None:
            job.contain_and_resume(process.pid)
        assert process.stdout is not None
        pipe = process.stdout

        def stream() -> None:
            try:
                for item in pipe:
                    output.put(item)
            except OSError as error:
                output.put(error)
            finally:
                output.put(None)

        reader = threading.Thread(target=stream, daemon=True)
        reader.start()
        drained = False
        terminated = False
        while not drained or process.poll() is None:
            if process.poll() is not None and not terminated:
                terminate()
                terminated = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CIProcessTimeout(f"COMMAND_TIMEOUT: {command[0]} after {timeout:g}s")
            try:
                item = output.get(timeout=min(0.05, remaining))
            except queue.Empty:
                continue
            if item is None:
                drained = True
            elif isinstance(item, OSError):
                raise item
            else:
                print(item, end="", flush=True)
                log.write(item)
                log.flush()
                if capture is not None:
                    capture.write(item)
                    capture.flush()
        if process.returncode:
            raise CIProcessError(f"COMMAND_FAILED: {command[0]} exit {process.returncode}")
    except OSError as error:
        raise CIProcessError(f"COMMAND_UNAVAILABLE: {command[0]}: {error}") from error
    finally:
        terminate()
        if process is not None:
            process.wait(timeout=5)
            if reader is not None:
                reader.join(timeout=5)
            # Never close a buffered pipe while its reader holds the lock. Detached POSIX
            # sessions are outside this process-group boundary; hostile code needs a VM.
            if process.stdout is not None and (reader is None or not reader.is_alive()):
                process.stdout.close()
