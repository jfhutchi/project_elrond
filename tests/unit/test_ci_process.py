"""Real processes exercise CI's deadline and descendant cleanup boundary."""

from __future__ import annotations

import ctypes
import importlib.util
import io
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def runner() -> ModuleType:
    path = Path(__file__).resolve().parents[2] / "scripts" / "ci_process.py"
    spec = importlib.util.spec_from_file_location("ci_process", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.WaitForSingleObject.restype = wintypes.DWORD
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x00100000, False, pid)
        if not handle:
            assert ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER: PID is gone.
            return False
        try:
            return bool(api.WaitForSingleObject(handle, 0) == 258)
        finally:
            api.CloseHandle(handle)
    else:
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split(")", 1)[1].split()[0] == "Z":
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        return True


def command_with_child(pid_file: Path, *, wait: bool) -> list[str]:
    code = (
        "import subprocess,sys,time,pathlib; "
        "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(60)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "print('READY',flush=True); "
        + ("time.sleep(60)" if wait else "")
    )
    return [sys.executable, "-I", "-S", "-c", code]


def assert_child_stopped(pid_file: Path) -> None:
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not alive(pid), f"CI leaked descendant PID {pid}"


def test_success_streams_console_and_log(
    runner: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    log = io.StringIO()
    runner.run_command(
        [sys.executable, "-I", "-S", "-c", "print('stream-sentinel')"],
        tmp_path, os.environ, log, timeout=5,
    )
    assert "stream-sentinel\n" in log.getvalue()
    assert "stream-sentinel\n" in capsys.readouterr().out


def test_nonzero_propagates(runner: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(runner.CIProcessError, match="exit 7"):
        runner.run_command(
            [sys.executable, "-I", "-S", "-c", "raise SystemExit(7)"],
            tmp_path, os.environ, io.StringIO(), timeout=5,
        )


def test_capture_contains_only_child_output(runner: ModuleType, tmp_path: Path) -> None:
    capture = io.StringIO()
    runner.run_command(
        [sys.executable, "-I", "-S", "-c", "print('capture-sentinel')"],
        tmp_path, os.environ, io.StringIO(), timeout=5, capture=capture,
    )
    assert capture.getvalue() == "capture-sentinel\n"


def test_exited_parent_cannot_leave_stdout_open(runner: ModuleType, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    runner.run_command(
        command_with_child(pid_file, wait=False), tmp_path, os.environ, io.StringIO(), timeout=3,
    )
    assert time.monotonic() - started < 3
    assert_child_stopped(pid_file)


def test_timeout_stops_parent_and_descendant(runner: ModuleType, tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    with pytest.raises(runner.CIProcessTimeout, match="COMMAND_TIMEOUT"):
        runner.run_command(
            command_with_child(pid_file, wait=True), tmp_path, os.environ, io.StringIO(), timeout=1,
        )
    assert time.monotonic() - started < 4
    assert_child_stopped(pid_file)


def test_interrupt_stops_descendant(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pid_file = tmp_path / "child.pid"
    original = runner.queue.Queue.get

    def interrupt_after_ready(self: object, **kwargs: object) -> object:
        item = original(self, **kwargs)
        if item == "READY\n":
            raise KeyboardInterrupt
        return item

    monkeypatch.setattr(runner.queue.Queue, "get", interrupt_after_ready)
    with pytest.raises(KeyboardInterrupt):
        runner.run_command(
            command_with_child(pid_file, wait=True), tmp_path, os.environ, io.StringIO(), timeout=5,
        )
    assert_child_stopped(pid_file)
