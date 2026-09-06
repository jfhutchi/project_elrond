"""Run committed CI snapshots without inheriting the operator's runtime configuration."""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TextIO

# Isolated Python excludes the script path; add only this trusted checkout for its CI helper.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import ci_process  # noqa: E402

CIError = ci_process.CIProcessError
run_command = ci_process.run_command


class PrerequisiteError(CIError):
    """The requested platform did not execute any verification."""


def clean_environment(parent: Mapping[str, str], home: Path) -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
        "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    }
    env = {key: value for key, value in parent.items() if key.upper() in allowed}
    home.mkdir(parents=True, exist_ok=True)
    temp = home / "tmp"
    temp.mkdir(exist_ok=True)
    env.update(
        HOME=str(home), USERPROFILE=str(home), APPDATA=str(home), LOCALAPPDATA=str(home),
        TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp),
        XDG_CONFIG_HOME=str(home / "config"), XDG_CACHE_HOME=str(home / "cache"),
        UV_CACHE_DIR=str(home / "uv-cache"), UV_NO_CONFIG="1", UV_LINK_MODE="copy",
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
        TRADING_MODE="PAPER", BROKER_ENVIRONMENT="PAPER",
        LIVE_TRADING_ACKNOWLEDGED="false", KILL_SWITCH="true", PYTHONUNBUFFERED="1",
    )
    return env


def git_output(root: Path, env: Mapping[str, str], *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], env=env,
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise CIError(f"GIT_FAILED: {args[0]} (exit {result.returncode})")
    return result.stdout.strip()


def export_snapshot(source: Path, destination: Path, env: Mapping[str, str]) -> str:
    # Read just this non-secret setting using the source's normal Git configuration. Removing
    # global autocrlf before inspecting a Windows checkout can make every clean file look dirty.
    setting = subprocess.run(
        ["git", "-C", str(source), "config", "--get", "core.autocrlf"],
        capture_output=True, text=True, check=False,
    )
    if setting.returncode not in (0, 1):
        raise CIError("GIT_CONFIG_FAILED: cannot determine source line-ending normalization")
    autocrlf = setting.stdout.strip() or "false"
    if autocrlf not in {"true", "false", "input"}:
        raise CIError("GIT_CONFIG_FAILED: invalid core.autocrlf")
    if git_output(
        source, env, "-c", f"core.autocrlf={autocrlf}", "-c", "core.fsmonitor=false",
        "status", "--porcelain", "--untracked-files=all",
    ):
        raise CIError("SOURCE_DIRTY: commit tracked/untracked changes before running local CI")
    sha = git_output(source, env, "rev-parse", "HEAD")
    if sha != env.get("ELROND_CI_COMMIT", sha):
        raise CIError("SOURCE_CHANGED: HEAD changed after this verification run started")
    archive = subprocess.run(
        ["git", "-C", str(source), "-c", "core.autocrlf=false", "archive", sha],
        env=env, capture_output=True, check=False,
    )
    if archive.returncode:
        raise CIError("SOURCE_EXPORT_FAILED")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            forbidden = (
                path.is_absolute() or ".." in path.parts or ".git" in path.parts
                or "\\" in member.name or ":" in member.name
                or not (member.isfile() or member.isdir())
                or any(part == ".env" or part.startswith(".env.")
                       and part != ".env.example" for part in path.parts)
                or path.suffix.lower() in {".db", ".sqlite", ".sqlite3", ".pem", ".key"}
            )
            if forbidden:
                raise CIError("UNSAFE_SNAPSHOT: tracked operational file or link refused")
        bundle.extractall(destination, filter="data")
    for args in (
        ("init", "-q"), ("config", "core.autocrlf", "false"),
        ("config", "user.email", "local-ci@example.invalid"),
        ("config", "user.name", "Elrond Local CI"), ("add", "."),
        ("commit", "-qm", f"CI snapshot of {sha}"),
    ):
        git_output(destination, env, *args)
    return sha


def linux_prerequisite(cwd: Path, env: Mapping[str, str], log: TextIO) -> str:
    docker = shutil.which("docker")
    if not docker:
        raise PrerequisiteError(
            "DOCKER_NOT_FOUND: Linux NOT RUN. Operator setup: install/enable WSL2 and Docker "
            "Desktop Linux containers (docs/local-ci.md), then rerun. No install was attempted."
        )
    result = subprocess.run(
        [docker, "info", "--format", "{{.OSType}}"], cwd=cwd, env=env,
        capture_output=True, text=True, check=False, timeout=30,
    )
    if result.returncode or result.stdout.strip() != "linux":
        raise PrerequisiteError(
            "DOCKER_LINUX_UNAVAILABLE: start a local Docker Linux engine; Linux NOT RUN"
        )
    log.write("Docker Linux engine ready\n")
    return docker


def docker_run_command(docker: str, name: str, image: str) -> list[str]:
    return [
        docker, "run", "--rm", "--name", name, "--network=none", "--cap-drop=ALL",
        "--security-opt=no-new-privileges", "--user=10001:10001", "--pids-limit=512",
        "--memory=8g", "--cpus=4", image,
        "python", "scripts/ci_checks.py", "--offline",
    ]


def linux_job(source: Path, workspace: Path, env: dict[str, str], log: TextIO) -> str:
    # Force local daemon selection; no ambient context, registry login, socket mount or TLS key.
    env["DOCKER_HOST"] = (
        "npipe:////./pipe/docker_engine" if os.name == "nt" else "unix:///var/run/docker.sock"
    )
    docker = linux_prerequisite(source, env, log)
    job = workspace / "linux"
    sha = export_snapshot(source, job, env)
    name = "elrond-ci-" + uuid.uuid4().hex
    image = "elrond-local-ci:" + name
    log.write(f"Owned container: {name}\nOwned image: {image}\n")
    try:
        run_command(
            [docker, "build", "--progress=plain", "-t", image, "-f", "scripts/ci/Dockerfile", "."],
            job, env, log,
        )
        run_command(docker_run_command(docker, name, image), job, env, log)
    finally:
        # These names were generated for this invocation; never prune unrelated resources.
        for kind, identifier in (("container", name), ("image", image)):
            selection = (
                f"name=^/{identifier}$" if kind == "container" else f"reference={identifier}"
            )
            exists = subprocess.run(
                [docker, kind, "ls", "--all", "--quiet", "--filter", selection], env=env,
                capture_output=True, text=True, check=False, timeout=30,
            )
            if exists.returncode:
                raise CIError(
                    f"CLEANUP_UNVERIFIED: Docker unavailable; inspect owned {kind} {identifier}"
                )
            if exists.stdout.strip():
                run_command([docker, kind, "rm", "--force", identifier], job, env, log, timeout=60)
    return sha


def windows_job(source: Path, workspace: Path, env: dict[str, str], log: TextIO) -> str:
    if platform.system() != "Windows":
        raise PrerequisiteError("WINDOWS_UNAVAILABLE: native Windows NOT RUN on this OS")
    uv = shutil.which("uv")
    if not uv:
        raise PrerequisiteError("UV_NOT_FOUND: install uv and put it on PATH (docs/local-ci.md)")
    job = workspace / "windows"
    sha = export_snapshot(source, job, env)
    # Reuse only the interpreter executable, never the operator's project environment/config.
    python = Path(sys.base_prefix) / "python.exe"
    if not python.is_file():
        raise PrerequisiteError("PYTHON_NOT_FOUND: install a managed Python 3.11 with uv")
    env["UV_PYTHON"] = str(python)
    run_command([uv, "--version"], job, env, log)
    run_command(
        [str(python), "-I", "-S", "-B", str(job / "scripts/ci_checks.py")], job, env, log
    )
    return sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("all", "linux", "windows"), default="all")
    parser.add_argument("--check-prerequisites", action="store_true")
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    output = source / ".local-ci" / run_id
    output.mkdir(parents=True)
    statuses: dict[str, dict[str, str]] = {
        "linux": {"status": "NOT RUN", "reason": "not requested"},
        "windows": {"status": "NOT RUN", "reason": "not requested"},
    }
    print(f"Local CI logs: {output}", flush=True)
    print("Local command reproduction; no GitHub check status or hosted job is submitted.")
    failed = False
    source_commit = "UNKNOWN"
    try:
        source_commit = git_output(source, os.environ, "rev-parse", "HEAD")
        for target, job in (("linux", linux_job), ("windows", windows_job)):
            if args.platform not in ("all", target):
                continue
            with tempfile.TemporaryDirectory(prefix="elrond-local-ci-") as scratch:
                workspace = Path(scratch).resolve()
                env = clean_environment(os.environ, workspace / "home")
                env["ELROND_CI_COMMIT"] = source_commit
                with (output / f"{target}.log").open("w", encoding="utf-8") as log:
                    log.write(f"Selected source commit: {source_commit}\n")
                    try:
                        if args.check_prerequisites:
                            if target == "linux":
                                linux_prerequisite(source, env, log)
                            elif sys.platform != "win32" or not shutil.which("uv"):
                                raise PrerequisiteError(
                                    "WINDOWS_PREREQUISITES_MISSING: requires Windows and uv"
                                )
                            statuses[target] = {
                                "status": "NOT RUN", "reason": "prerequisites ready"
                            }
                        else:
                            sha = job(source, workspace, env, log)
                            statuses[target] = {"status": "PASS", "source_commit": sha}
                    except (CIError, OSError, subprocess.TimeoutExpired) as error:
                        # These are boundary failures. Their explicit report still fails the run.
                        failed = True
                        statuses[target] = {
                            "status": "NOT RUN" if isinstance(error, PrerequisiteError) else "FAIL",
                            "reason": str(error),
                        }
                        print(str(error), flush=True)
                        log.write(str(error) + "\n")
    except KeyboardInterrupt:
        failed = True
        print("INTERRUPTED: unfinished platforms are NOT RUN", flush=True)
    except CIError as error:
        failed = True
        print(str(error), flush=True)
    finally:
        report = {
            "run_id": run_id, "host_platform": platform.platform(),
            "selected_source_commit": source_commit,
            "scope": "local commands, not GitHub-hosted image parity or independent approval",
            "jobs": statuses,
        }
        (output / "summary.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        for target, status in statuses.items():
            print(f"{target}: {status['status']} {status.get('reason', '')}", flush=True)
        print(f"Report: {output / 'summary.json'}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
