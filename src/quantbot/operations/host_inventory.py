"""Bounded OS inventory. Report access failures, never dump command lines/environments."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

_WINDOWS = r"""
$ErrorActionPreference = 'Stop'
$result = @{}
$pattern = 'quantbot|project_elrond|supervisor.py|quantbot-watchdog|kronos'
try {
  $result.processes = @(Get-CimInstance Win32_Process | Where-Object {
    $_.ProcessId -ne $PID -and ($_.Name -match 'python|uv.exe|quantbot' -or
      $_.CommandLine -match $pattern)
  } | ForEach-Object {
    [pscustomobject]@{pid=$_.ProcessId; parent_pid=$_.ParentProcessId;
      name=$_.Name; executable=$_.ExecutablePath;
      started_at=$_.CreationDate.ToUniversalTime().ToString('o');
      elrond_reference=($_.CommandLine -match $pattern);
      command_line_readable=($null -ne $_.CommandLine)}
  })
} catch { $result.processes = 'UNKNOWN' }
try {
  $result.scheduled_tasks = @(Get-ScheduledTask | Where-Object {
    $_.TaskName -match $pattern -or (($_.Actions | Out-String) -match $pattern)
  } | ForEach-Object {
    [pscustomobject]@{name=$_.TaskName; path=$_.TaskPath; state=[string]$_.State;
      enabled=$_.Settings.Enabled; executables=@($_.Actions.Execute)}
  })
} catch { $result.scheduled_tasks = 'UNKNOWN' }
try {
  $result.services = @(Get-CimInstance Win32_Service | Where-Object {
    $_.Name -match $pattern -or $_.PathName -match $pattern
  } | Select-Object Name,State,StartMode)
} catch { $result.services = 'UNKNOWN' }
try {
  $result.registry_startup = @(foreach ($key in @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Run',
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\RunOnce')) {
      if (Test-Path -LiteralPath $key) {
        (Get-ItemProperty -LiteralPath $key).PSObject.Properties |
          Where-Object {$_.Name -notlike 'PS*'} | ForEach-Object {
            [pscustomobject]@{location=$key; name=$_.Name;
              elrond_reference=([string]$_.Value -match $pattern)}
          }
      }
  })
  $result.startup_files = @(foreach ($folder in @(
    "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup",
    "$env:ProgramData\Microsoft\Windows\Start Menu\Programs\Startup")) {
      Get-ChildItem -Force -LiteralPath $folder | Select-Object FullName,Length,LastWriteTimeUtc
  })
} catch { $result.startup_files = 'UNKNOWN'; $result.registry_startup = 'UNKNOWN' }
try {
  $result.persisted_environment = @(foreach ($scope in 'User','Machine') {
    [Environment]::GetEnvironmentVariables($scope).Keys | Where-Object {
      $_ -match '^QUANTBOT_|^ALPACA_|^TRADING_MODE$|^BROKER_ENVIRONMENT$|^EXPECTED_ACCOUNT_ID$'
    } | ForEach-Object {
      $value = '<withheld>'
      if ($_ -in @('QUANTBOT_DB_PATH','QUANTBOT_CONFIG','QUANTBOT_LOCK_PATH',
                    'QUANTBOT_REPORTS_DIR','QUANTBOT_STATE_DIR')) {
        $value = [Environment]::GetEnvironmentVariable($_, $scope)
      }
      [pscustomobject]@{scope=$scope; name=$_; value=$value}
    }
  })
  $lxss = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
  $result.wsl_distributions = @(if (Test-Path $lxss) {
    Get-ChildItem $lxss |
      ForEach-Object { (Get-ItemProperty $_.PSPath).DistributionName }
  })
} catch { $result.persisted_environment = 'UNKNOWN'; $result.wsl_distributions = 'UNKNOWN' }
$result | ConvertTo-Json -Depth 6 -Compress
"""


def _command(arguments: list[str]) -> str | None:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=25, check=False)
        return result.stdout if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return None


def host_inventory() -> dict[str, Any]:
    if sys.platform == "win32":
        return _windows_inventory()
    elif sys.platform == "linux":
        return _linux_inventory()
    else:
        return {"state": "UNKNOWN", "reason": "OS_INVENTORY_UNSUPPORTED"}


def _windows_inventory() -> dict[str, Any]:
    raw = _command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _WINDOWS])
    try:
        result = json.loads(raw) if raw else {}
    except (ValueError, TypeError):
        result = {}
    if not isinstance(result, dict) or not result:
        return {"state": "UNKNOWN", "reason": "OS_INVENTORY_UNAVAILABLE"}
    # Name matching intentionally includes wrappers. Identify the inspector's ancestry
    # instead of letting its own uv/python chain look like deployed coordinators.
    processes = result.get("processes")
    if isinstance(processes, list):
        by_pid = {item.get("pid"): item for item in processes if isinstance(item, dict)}
        process = os.getpid()
        chain: set[int] = set()
        while process in by_pid and process not in chain:
            chain.add(process)
            parent = by_pid[process].get("parent_pid")
            if not isinstance(parent, int):
                break
            process = parent
        for item in by_pid.values():
            item["diagnostic_ancestry"] = item["pid"] in chain
            item["coordinator_authority"] = "UNKNOWN"
    result["scope"] = (
        "current-user visibility; task/service command references; "
        "startup filenames require review; WSL guests not started or inspected"
    )
    return result


def _linux_inventory() -> dict[str, Any]:
    processes: list[dict[str, Any]] = []
    denied = 0
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                command = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", errors="replace")
                )
                if re.search(r"quantbot|project_elrond|supervisor.py|kronos|python", command):
                    processes.append(
                        {
                            "pid": int(entry.name),
                            "executable": str((entry / "exe").resolve()),
                            "authority": "UNKNOWN",
                        }
                    )
            except OSError:
                denied += 1
    except OSError:
        return {"state": "UNKNOWN", "reason": "PROC_UNREADABLE"}
    units = {}
    for scope, flags in (("system", []), ("user", ["--user"])):
        raw = _command(["systemctl", *flags, "list-unit-files", "--no-pager", "--no-legend"])
        units[scope] = (
            "UNKNOWN"
            if raw is None
            else [
                line
                for line in raw.splitlines()
                if re.search(r"quantbot|elrond|kronos", line, re.I)
            ]
        )
    return {
        "processes": processes,
        "unreadable_processes": denied,
        "systemd_unit_files": units,
        "cron_and_other_startup": "UNKNOWN",
        "scope": "visible /proc and named systemd units; custom wrappers require review",
    }
