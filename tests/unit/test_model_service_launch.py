"""Windows model task must survive informational stderr and discard broker configuration."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows task launcher")


def _cmd_processes_running_script(powershell: Path, script: Path) -> list[int]:
    needle = str(script).replace("'", "''")
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "$needle = '" + needle + "'; "
            "@(Get-CimInstance Win32_Process | Where-Object { "
            "$_.Name -eq 'cmd.exe' -and $_.CommandLine -like ('*' + $needle + '*') "
            "} | ForEach-Object ProcessId) -join ','",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    return [int(value) for value in result.stdout.strip().split(",") if value]


def test_native_stderr_does_not_stop_model_task_and_broker_secrets_are_removed(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "model.cmd"
    executable.write_text(
        "@echo off\necho SERVER_INFO 1>&2\n"
        "echo alive\necho keepalive=%OLLAMA_KEEP_ALIVE%\n"
        "echo host=%OLLAMA_HOST%\necho clouds=%OLLAMA_NO_CLOUD%\n"
        "echo broker=%ALPACA_PAPER_API_SECRET%\nexit /b 0\n",
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[2] / "deploy/omen/start-model-server.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    logs = tmp_path / "logs"
    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Executable",
            str(executable),
            "-Models",
            str(tmp_path / "models"),
            "-LogDirectory",
            str(logs),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "ALPACA_PAPER_API_SECRET": "TEST_SECRET_MUST_NOT_SURVIVE",
            "OLLAMA_KEEP_ALIVE": "-1",
            "OLLAMA_HOST": "0.0.0.0:11434",
        },
    )
    assert result.returncode == 0, result.stderr
    stdout = (logs / "model-server.stdout.log").read_text(encoding="utf-16")
    stderr = (logs / "model-server.stderr.log").read_text(encoding="utf-16")
    assert "SERVER_INFO" in stderr
    assert "alive" in stdout
    assert "keepalive=0" in stdout
    assert "host=127.0.0.1:11434" in stdout
    assert "clouds=1" in stdout
    assert "TEST_SECRET_MUST_NOT_SURVIVE" not in stdout + stderr


def test_an_existing_file_that_cannot_launch_is_not_reported_as_success(tmp_path: Path) -> None:
    executable = tmp_path / "not-an-executable.txt"
    executable.write_text("this is not a Windows executable", encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "deploy/omen/start-model-server.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Executable",
            str(executable),
            "-Models",
            str(tmp_path / "models"),
            "-LogDirectory",
            str(tmp_path / "logs"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0


def test_a_native_server_failure_is_the_task_exit_code(tmp_path: Path) -> None:
    executable = tmp_path / "model.cmd"
    executable.write_text("@echo off\necho failed 1>&2\nexit /b 37\n", encoding="utf-8")
    script = Path(__file__).resolve().parents[2] / "deploy/omen/start-model-server.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"

    result = subprocess.run(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Executable",
            str(executable),
            "-Models",
            str(tmp_path / "models"),
            "-LogDirectory",
            str(tmp_path / "logs"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 37


def test_terminating_the_task_wrapper_terminates_its_server_process(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    executable = tmp_path / "long-running-model.cmd"
    executable.write_text(
        f'@echo off\necho ready>"{ready}"\n:loop\nping -n 2 127.0.0.1 > nul\ngoto loop\n',
        encoding="utf-8",
    )
    script = Path(__file__).resolve().parents[2] / "deploy/omen/start-model-server.ps1"
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    wrapper = subprocess.Popen(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Executable",
            str(executable),
            "-Models",
            str(tmp_path / "models"),
            "-LogDirectory",
            str(tmp_path / "logs"),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_ids: list[int] = []
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.05)
        assert ready.exists(), "dummy server did not start"
        child_ids = _cmd_processes_running_script(powershell, executable)
        assert child_ids, "wrapper did not own the dummy server"

        wrapper.kill()
        wrapper.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _cmd_processes_running_script(powershell, executable):
            time.sleep(0.1)

        assert not _cmd_processes_running_script(powershell, executable)
    finally:
        if wrapper.poll() is None:
            wrapper.kill()
            wrapper.wait(timeout=10)
        for process_id in _cmd_processes_running_script(powershell, executable):
            subprocess.run(
                [
                    str(powershell),
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    f"Stop-Process -Id {process_id} -Force",
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
