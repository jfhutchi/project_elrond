"""Use an existing permitted test shell; never change or override its policy."""

import shutil
import subprocess
from functools import cache


@cache
def permitted_powershell() -> str:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        raise RuntimeError("WINDOWS_TEST_POWERSHELL_UNAVAILABLE")
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    if result.stdout.strip() not in {"RemoteSigned", "Unrestricted"}:
        # Do not retry another engine after its actual policy refuses these fixtures.
        raise RuntimeError("WINDOWS_TEST_SCRIPT_POLICY_REFUSES_UNSIGNED_FIXTURES")
    return shell
