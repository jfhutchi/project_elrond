"""Bounded disposable Windows service-host log retention behavior."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows service host")
ROOT = Path(__file__).resolve().parents[3]
LIMIT = 512


@pytest.fixture(scope="module")
def rotating_host(tmp_path_factory):
    directory = tmp_path_factory.mktemp("rotating-host")
    source = (ROOT / "deploy/omen/service-host.cs").read_text()
    source = source.replace("32L * 1024 * 1024", str(LIMIT) + "L")
    fixture = directory / "host.cs"
    fixture.write_text(source, encoding="utf-8")
    executable = directory / "host.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    result = subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:winexe",
            f"/out:{executable}",
            str(ROOT / "deploy/omen/process-job.cs"),
            str(fixture),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def invoke(host, log, script, tmp_path):
    path = tmp_path / "child.ps1"
    path.write_text(script, encoding="utf-8-sig")
    return subprocess.run(
        [
            str(host),
            str(log),
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(path),
        ],
        timeout=20,
        check=False,
    )


def retained(log):
    return [
        path for path in [Path(str(log) + f".{i}") for i in (3, 2, 1)] + [log] if path.is_file()
    ]


def test_utf8_lines_preserved_across_rotation(rotating_host, log_probe, tmp_path):
    log = tmp_path / "host.log"
    result = subprocess.run([str(log_probe), str(rotating_host), str(log)], timeout=10)
    assert result.returncode == 37
    files = retained(log)
    assert len(files) > 1
    assert all(path.stat().st_size <= LIMIT for path in files)
    records = [
        line.split(" ", 1)[1]
        for path in files
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [f"REC{i:02d}-é日本語" for i in range(1, 21)]
    assert all(not path.read_bytes().startswith(b"\xef\xbb\xbf") for path in files)


def test_startup_oversize_keeps_recent_complete_lines_and_bounds_archives(rotating_host, tmp_path):
    log = tmp_path / "host.log"
    old = [f"OLD{i:03d}-" + "x" * 40 for i in range(100)]
    log.write_bytes(("\r\n".join(old) + "\r\n").encode())
    result = invoke(rotating_host, log, "Write-Output 'NEW'", tmp_path)
    assert result.returncode == 0
    files = retained(log)
    assert all(path.stat().st_size <= LIMIT for path in files)
    assert Path(str(log) + ".1").read_text().splitlines()[-1] == old[-1]
    assert "NEW" in log.read_text()


def test_stdout_stderr_concurrent_callbacks_preserve_all_records(rotating_host, tmp_path):
    log = tmp_path / "host.log"
    result = invoke(
        rotating_host,
        log,
        "1..18 | ForEach-Object { [Console]::Out.WriteLine(('OUT{0:D2}' -f $_)); "
        "[Console]::Error.WriteLine(('ERR{0:D2}' -f $_)) }",
        tmp_path,
    )
    assert result.returncode == 0
    files = retained(log)
    assert len(files) > 1
    assert len(files) <= 4
    assert all(path.stat().st_size <= LIMIT for path in files)
    records = [line.split(" ", 1)[1] for path in files for line in path.read_text().splitlines()]
    assert sorted(records) == sorted(
        [f"{stream}{i:02d}" for stream in ("OUT", "ERR") for i in range(1, 19)]
    )


def test_rotation_failure_exits_70_and_startup_failure_does_not_launch_child(
    rotating_host, tmp_path
):
    log = tmp_path / "host.log"
    Path(str(log) + ".1").mkdir()
    result = invoke(
        rotating_host, log, "1..50 | ForEach-Object { Write-Output ('x' * 80) }", tmp_path
    )
    assert result.returncode == 70
    marker = tmp_path / "unexpected"
    result = invoke(rotating_host, tmp_path, f"New-Item -ItemType File '{marker}'", tmp_path)
    assert result.returncode == 70
    assert not marker.exists()


@pytest.fixture(scope="module")
def log_probe(rotating_host, tmp_path_factory):
    directory = tmp_path_factory.mktemp("log-probe")
    source = directory / "probe.cs"
    source.write_text(
        r"""
using System;
using System.IO;
using System.Reflection;
class Probe {
    static int Main(string[] args) {
        Type host = Assembly.LoadFile(args[0]).GetType("ElrondServiceHost");
        Type writer = host.GetNestedType("RotatingLog", BindingFlags.NonPublic);
        using (TextWriter log = (TextWriter)Activator.CreateInstance(
                writer, new object[] { args[1] })) {
            MethodInfo write = host.GetMethod("WriteLog",
                BindingFlags.NonPublic | BindingFlags.Static);
            if (args.Length > 2) write.Invoke(null, new object[] {log, new String('\u65e5', 260)});
            else for (int i = 1; i <= 20; i++)
                write.Invoke(null, new object[] {log, "REC" + i.ToString("D2")
                    + "-\u00e9\u65e5\u672c\u8a9e"});
        }
        return 37;
    }
}
""",
        encoding="utf-8",
    )
    executable = directory / "probe.exe"
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    subprocess.run(
        [str(compiler), "/nologo", "/target:winexe", f"/out:{executable}", str(source)],
        capture_output=True,
        timeout=30,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return executable


def test_single_oversized_record_preserves_utf8_bytes_within_bound(
    rotating_host, log_probe, tmp_path
):
    log = tmp_path / "host.log"
    result = subprocess.run([str(log_probe), str(rotating_host), str(log), "long"], timeout=10)
    assert result.returncode == 37
    files = retained(log)
    assert len(files) > 1
    assert all(path.stat().st_size <= LIMIT for path in files)
    # Each chunk is independently valid UTF-8; concatenation preserves the original record.
    text = "".join(path.read_bytes().decode("utf-8") for path in files)
    assert text.split(" ", 1)[1] == "日" * 260 + "\r\n"
