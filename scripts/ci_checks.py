"""The shared local/hosted CI command contract; no application imports."""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def commands() -> list[list[str]]:
    helpers = ["scripts/ci_checks.py", "scripts/local_ci.py", "scripts/ci_process.py"]
    return [
        ["uv", "lock", "--check"],
        ["uv", "sync", "--frozen", "--group", "dev"],
        ["uv", "run", "--frozen", "ruff", "check", "src", "tests"],
        ["uv", "run", "--frozen", "mypy", "--platform", "linux", "src"],
        ["uv", "run", "--frozen", "mypy", "--platform", "win32", "src"],
        ["uv", "run", "--frozen", "ruff", "check", *helpers],
        ["uv", "run", "--frozen", "mypy", "--platform", "linux", *helpers],
        ["uv", "run", "--frozen", "mypy", "--platform", "win32", *helpers],
        ["uv", "run", "--frozen", "pytest"],
    ]


def check_tree(root: Path) -> int:
    diff = subprocess.run(["git", "diff", "--check"], cwd=root, check=False)
    staged = subprocess.run(["git", "diff", "--cached", "--check"], cwd=root, check=False)
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root, capture_output=True, text=True, check=False,
    )
    print("+ git status --short", flush=True)
    if status.returncode:
        print("GIT_STATUS_FAILED", flush=True)
        return status.returncode
    if status.stdout:
        print("CHECKOUT_CHANGED:\n" + status.stdout, flush=True)
        return 1
    return diff.returncode or staged.returncode


def run_suite(root: Path, *, offline: bool = False) -> int:
    env = dict(os.environ)
    env.update(
        TRADING_MODE="PAPER", BROKER_ENVIRONMENT="PAPER",
        LIVE_TRADING_ACKNOWLEDGED="false", KILL_SWITCH="true", UV_LINK_MODE="copy",
        UV_NO_CONFIG="1", PYTHONUNBUFFERED="1",
    )
    if offline:
        env["UV_OFFLINE"] = "1"
        env["UV_PYTHON_DOWNLOADS"] = "never"
    first_failure = 0
    try:
        for command in commands():
            print("+ " + " ".join(command), flush=True)
            result = subprocess.run(command, cwd=root, env=env, check=False)
            if result.returncode:
                first_failure = result.returncode
                break
    finally:
        tree_failure = check_tree(root)
    return first_failure or tree_failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    raise SystemExit(run_suite(Path(__file__).resolve().parents[1], offline=args.offline))
