"""Task Scheduler must not allocate a terminal or orphan the service tree."""

import ctypes
import json
import os
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows service host")


@pytest.fixture(scope="module")
def service_host(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = Path(__file__).resolve().parents[2]
    builder = root / "deploy/omen/build-service-host.ps1"
    assert builder.exists(), "the scheduled action needs a windowless process host"
    result = subprocess.run(
        [
            "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(builder), "-OutputDirectory", str(tmp_path_factory.mktemp("host")),
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return Path(result.stdout.strip().splitlines()[-1])


def _alive(process_id: int) -> bool:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    handle = kernel.OpenProcess(0x1000, False, process_id)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        assert kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        return code.value == 259
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


def test_host_is_a_gui_executable_and_child_has_no_console(
    service_host: Path, tmp_path: Path,
) -> None:
    binary = service_host.read_bytes()
    pe = struct.unpack_from("<I", binary, 0x3C)[0]
    assert struct.unpack_from("<H", binary, pe + 24 + 68)[0] == 2, "must use GUI subsystem"
    result_file = tmp_path / "console.json"
    script = tmp_path / "probe.ps1"
    script.write_text(
        "Add-Type 'using System; using System.Runtime.InteropServices; "
        "public class ConsoleProbe { [DllImport(\"kernel32.dll\")] "
        "public static extern IntPtr GetConsoleWindow(); }'\n"
        "[ConsoleProbe]::GetConsoleWindow().ToInt64() | "
        f"Set-Content -Encoding ASCII -LiteralPath '{result_file}'\n"
        "Write-Output 'HOST_STDOUT'\n[Console]::Error.WriteLine('HOST_STDERR')\nexit 37\n",
        encoding="utf-8",
    )
    log = tmp_path / "launcher.log"
    result = subprocess.run(
        [str(service_host), str(log), "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
         "Bypass", "-File", str(script)],
        timeout=20, check=False,
    )
    assert result.returncode == 37
    assert int(result_file.read_text()) == 0
    assert "HOST_STDOUT" in log.read_text(encoding="utf-8-sig")
    assert "HOST_STDERR" in log.read_text(encoding="utf-8-sig")


def test_host_preserves_quoted_and_empty_arguments(service_host: Path, tmp_path: Path) -> None:
    script = tmp_path / "argument probe.ps1"
    result_file = tmp_path / "arguments.json"
    script.write_text(
        "ConvertTo-Json -InputObject @($args) -Compress | "
        f"Set-Content -Encoding UTF8 '{result_file}'\n",
        encoding="utf-8",
    )
    arguments = ["has spaces", "", 'a"quote', "ends-in-\\", 'slash\\"quote', "日本語"]
    result = subprocess.run(
        [str(service_host), str(tmp_path / "host.log"), "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass",
         "-File", str(script), *arguments], timeout=20, check=False,
    )
    assert result.returncode == 0
    assert json.loads(result_file.read_text(encoding="utf-8-sig")) == arguments


def test_terminating_host_stops_powershell_and_its_descendant(
    service_host: Path, tmp_path: Path,
) -> None:
    ready = tmp_path / "ready.json"
    child_ready = tmp_path / "child.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import json, os, time\nfrom pathlib import Path\n"
        f"Path({str(child_ready)!r}).write_text(json.dumps({{'pid': os.getpid()}}))\n"
        "time.sleep(60)\n", encoding="utf-8",
    )
    script = tmp_path / "parent.ps1"
    script.write_text(
        f"$PID | Set-Content -Encoding ASCII -LiteralPath '{ready}'\n"
        f"& '{sys.executable}' '{child}'\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [str(service_host), str(tmp_path / "host.log"), "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass",
         "-File", str(script)],
    )
    descendants: list[int] = []
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not child_ready.exists():
            time.sleep(0.05)
        assert child_ready.exists(), "host failed to start its descendant"
        descendants = [int(ready.read_text()), json.loads(child_ready.read_text())["pid"]]
        assert all(_alive(pid) for pid in descendants)
        process.kill()
        process.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(_alive(pid) for pid in descendants):
            time.sleep(0.05)
        assert not any(_alive(pid) for pid in descendants)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        for pid in descendants:
            if _alive(pid):
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], check=False)


def test_unwritable_log_refuses_before_starting_child(service_host: Path, tmp_path: Path) -> None:
    marker = tmp_path / "unexpected-child"
    result = subprocess.run(
        [str(service_host), str(tmp_path), "-NoProfile", "-Command",
         f"New-Item -ItemType File -Path '{marker}'"],
        timeout=20, check=False,
    )
    assert result.returncode != 0
    assert not marker.exists()


def test_log_failure_exits_deliberately_without_an_unhandled_callback_exception(
    service_host: Path, tmp_path: Path,
) -> None:
    harness = tmp_path / "log-failure.cs"
    harness.write_text(
        """
using System;
using System.IO;
using System.Reflection;
using System.Text;
class FailingStream : MemoryStream {
    public override void Write(byte[] buffer, int offset, int count) {
        if (count > 0) throw new IOException("simulated full log volume");
    }
}
class Probe {
    static int Main(string[] args) {
        try {
            StreamWriter writer = new StreamWriter(new FailingStream(), new UTF8Encoding(false));
            writer.AutoFlush = true;
            Assembly.LoadFile(args[0]).GetType("ElrondServiceHost")
                .GetMethod("WriteLog", BindingFlags.NonPublic | BindingFlags.Static)
                .Invoke(null, new object[] { writer, "a line" });
            return 0;
        } catch (Exception) { return 99; }
    }
}
""", encoding="utf-8",
    )
    executable = tmp_path / "log-failure.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run(
        [str(compiler), "/nologo", "/target:winexe", f"/out:{executable}", str(harness)],
        capture_output=True, timeout=30, check=True,
    )
    result = subprocess.run([str(executable), str(service_host)], timeout=10, check=False)
    assert result.returncode == 70, "log failure must not escape an asynchronous reader callback"
