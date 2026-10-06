"""Native observation bootstrap works under unchanged Windows script policy."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.skipif(sys.platform != "win32", reason="Native Windows launch contract")
@pytest.mark.parametrize("case", ["valid", "wrong-hash", "bad-home"])
def test_native_shadow_bootstrap_keeps_policy_and_checks_pins(tmp_path, case):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    windows_shell = Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    helper = Path(__file__).resolve().parents[3] / "deploy/omen/adaptive-native-command.ps1"
    repository = tmp_path / "repo ' quoted $literal"
    package = repository / "src/quantbot/learner"
    package.mkdir(parents=True)
    for folder in (package.parent, package):
        (folder / "__init__.py").write_text("")
    (package / "experiment_launch.py").write_text(
        "import json,os,sys\nfrom pathlib import Path\n"
        "Path(__file__).resolve().parents[3].joinpath('observed.json').write_text("
        "json.dumps({'argv':sys.argv[1:],'pythonpath':os.environ['PYTHONPATH'],"
        "'isolated':os.environ.get('PYTHONNOUSERSITE')=='1',"
        "'pythonhome_cleared':'PYTHONHOME' not in os.environ}))\n"
    )
    venv = repository / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    shutil.copyfile(sys.executable, venv / "Scripts/python.exe")
    home = str(sys.base_prefix) if case != "bad-home" else str(tmp_path / "missing-python")
    (venv / "pyvenv.cfg").write_text(f"home = {home}\ninclude-system-site-packages = false\n")
    config = tmp_path / "config ' $literal.json"
    config.write_text("{}")
    pin = hashlib.sha256(config.read_bytes()).hexdigest() if case != "wrong-hash" else "a" * 64

    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"

    built = subprocess.run(
        [
            shell,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f". {quote(helper)}; New-ElrondAdaptiveNativeCommand -Repository {quote(repository)} "
            f"-Config {quote(config)} -ConfigHash {pin} -Check",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert built.returncode == 0, built.stderr
    env = dict(os.environ, PYTHONHOME="INVALID_INHERITED_HOME")
    # Scheduler starts Windows PowerShell, not an inherited PowerShell7 module tree.
    env.pop("PSModulePath", None)
    result = subprocess.run(
        [
            str(windows_shell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            built.stdout,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if case != "valid":
        assert result.returncode != 0
        assert not (repository / "observed.json").exists()
        assert (
            "ADAPTIVE_CONFIG_PIN_MISMATCH"
            if case == "wrong-hash"
            else "ADAPTIVE_NATIVE_PYTHON_REQUIRED"
        ) in result.stderr
        return
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads((repository / "observed.json").read_text())
    assert observed["argv"] == ["--config", str(config), "--config-hash", pin, "--check"]
    assert observed["pythonpath"] == str(repository / "src")
    assert observed["isolated"] and observed["pythonhome_cleared"]
