"""Shared wrapper bootstrap preserves interpreter and refusal boundaries."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows wrapper bootstrap")


@pytest.mark.parametrize("prefix", ["SERVICE_WRAPPER", "MONITOR_WRAPPER"])
@pytest.mark.parametrize(
    "case",
    [
        "valid",
        "missing-runtime",
        "invalid-home",
        "missing-base",
        "hidden-base",
    ],
)
def test_runtime_bootstrap_preserves_environment_and_refusals(tmp_path, prefix, case):
    scripts = tmp_path / "deploy/omen"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/omen/runtime-bootstrap.ps1", scripts / "runtime-bootstrap.ps1")
    # The bootstrap must keep the returned job rooted in its caller. Native ownership itself
    # is exercised by test_service_wrapper_terminates_its_python_process_tree.
    (scripts / "windows-job.ps1").write_text(
        "function New-ElrondProcessJob { return 'owned-job-fixture' }\n"
    )
    (tmp_path / "src").mkdir()
    venv = tmp_path / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts/python.exe").write_bytes(b"never executed")
    base = tmp_path / ("roaming/python" if case == "hidden-base" else "base")
    base.mkdir(parents=True)
    if case != "missing-base":
        (base / "python.exe").write_bytes(b"never executed")
    home = "relative" if case == "invalid-home" else str(base)
    (venv / "pyvenv.cfg").write_text(f"home = {home}\n")
    if case == "missing-runtime":
        (venv / "Scripts/python.exe").unlink()
    harness = tmp_path / "invoke.ps1"
    harness.write_text(r"""$ErrorActionPreference = 'Stop'
try {
    . $args[0] -Repository $args[1] -ErrorPrefix $args[2]
    @{Python=$Python;Source=$env:PYTHONPATH;NoUser=$env:PYTHONNOUSERSITE;
      Home=$env:PYTHONHOME;Executable=$env:PYTHONEXECUTABLE;Launcher=$env:__PYVENV_LAUNCHER__;
      Job=$ServiceJob;Preference=[string]$ErrorActionPreference;Code=$global:LASTEXITCODE} |
      ConvertTo-Json -Compress
} catch { Write-Output $_.Exception.Message; exit 7 }
""")
    env = dict(
        os.environ,
        APPDATA=str(tmp_path / "roaming"),
        LOCALAPPDATA=str(tmp_path / "local"),
        PYTHONHOME="invalid",
        PYTHONEXECUTABLE="invalid",
        __PYVENV_LAUNCHER__="invalid",
    )
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(harness),
            str(scripts / "runtime-bootstrap.ps1"),
            str(tmp_path),
            prefix,
        ],
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    if case != "valid":
        suffix = {
            "missing-runtime": "RUNTIME_NOT_FOUND",
            "invalid-home": "VENV_HOME_INVALID",
            "missing-base": "BASE_PYTHON_NOT_FOUND",
            "hidden-base": "BASE_NOT_HOST_VISIBLE",
        }
        assert result.returncode == 7, result.stderr
        assert result.stdout.strip() == prefix + "_" + suffix[case]
        return
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["Python"]) == venv / "Scripts/python.exe"
    assert Path(observed["Source"]) == tmp_path / "src"
    assert observed["NoUser"] == "1"
    assert not any(observed[key] for key in ("Home", "Executable", "Launcher"))
    assert observed["Job"] == "owned-job-fixture"
    assert observed["Preference"] == "Continue"
    assert observed["Code"] is None


def test_installer_refuses_incomplete_bootstrap_package_before_scheduler_access(tmp_path):
    scripts = tmp_path / "deploy/omen"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "deploy/omen/setup-windows.ps1", scripts / "setup-windows.ps1")
    for name in ("start-service.ps1", "windows-job.ps1"):
        (scripts / name).write_text("throw 'must never execute'\n")
    venv = tmp_path / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts/python.exe").write_bytes(b"never executed")
    (venv / "pyvenv.cfg").write_text("home = intentionally-invalid\n")
    environment = tmp_path / "fixture.env"
    environment.write_text("TRADING_MODE=PAPER\n")
    database = tmp_path / "fixture.db"
    database.write_bytes(b"never opened")
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(scripts / "setup-windows.ps1"),
            "-EnvironmentFile",
            str(environment),
            "-Database",
            str(database),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    assert "SERVICE_RUNTIME_NOT_FOUND" in result.stderr
    assert "VENV_HOME_INVALID" not in result.stderr
