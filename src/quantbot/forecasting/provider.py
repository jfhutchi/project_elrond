"""Dependency-isolated provider boundary for untrusted candle forecasting."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from quantbot.forecasting.models import ForecastRequest, WorkerForecast


class ForecastProviderError(RuntimeError):
    """Raised when the isolated forecasting worker cannot return a valid artifact."""

    def __init__(self, message: str, *, code: str = "PROVIDER_ERROR") -> None:
        super().__init__(message)
        self.code = code


class SignalProvider(Protocol):
    """A research signal source with no order, portfolio, or risk methods."""

    def forecast(self, request: ForecastRequest) -> WorkerForecast: ...


_SAFE_PARENT_ENVIRONMENT = frozenset(
    {
        "CUDA_VISIBLE_DEVICES",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "WINDIR",
    }
)


def isolated_worker_environment(
    parent: Mapping[str, str],
    *,
    cache_directory: Path,
) -> dict[str, str]:
    """Build an allowlisted offline environment, excluding every application credential."""
    environment = {key: value for key, value in parent.items() if key in _SAFE_PARENT_ENVIRONMENT}
    environment.update(
        {
            "HF_HOME": str(cache_directory),
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    return environment


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


class KronosSignalProvider:
    """Invoke a one-request JSON worker without importing Torch into the caller."""

    def __init__(
        self,
        *,
        python_executable: str | Path,
        kronos_repository: str | Path,
        cache_directory: str | Path,
        timeout_seconds: int = 300,
    ) -> None:
        if not str(python_executable).strip():
            raise ValueError("python_executable must not be empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        repository = Path(kronos_repository).expanduser().resolve()
        if not repository.is_dir():
            raise ValueError("kronos_repository must be an existing directory")
        cache = Path(cache_directory).expanduser().resolve()
        source_root = Path(__file__).resolve().parents[3]
        # Absolutised but deliberately not symlink-resolved. `python -m venv` symlinks its
        # interpreter by default on POSIX, and `uv venv` always does, so resolving would launch
        # the *base* interpreter -- which has none of the pinned worker packages and fails the
        # attestation on every request. The venv is identified by the path, not by its target.
        worker_python = Path(python_executable).expanduser().absolute()
        if not worker_python.is_file():
            raise ValueError("python_executable must be an existing dedicated interpreter")
        # Containment is still checked against the target: a symlink sitting outside the tree
        # that points into it would otherwise launch Elrond's own interpreter.
        if _inside(worker_python, source_root) or _inside(worker_python.resolve(), source_root):
            raise ValueError("python_executable must be outside the Elrond working tree")
        git_raw = shutil.which("git")
        if git_raw is None:
            raise ValueError("git executable is required to verify the Kronos checkout")
        git_executable = Path(git_raw).resolve()
        if _inside(git_executable, source_root):
            raise ValueError("git executable must be outside the Elrond working tree")
        if _inside(cache, source_root):
            raise ValueError("cache_directory must be outside the Elrond working tree")
        if _inside(repository, source_root):
            raise ValueError("kronos_repository must be outside the Elrond working tree")
        cache.mkdir(parents=True, exist_ok=True)
        worker_source = Path(__file__).with_name("kronos_worker.py").resolve()
        worker_digest = _digest(worker_source)
        worker_entry = cache / f"kronos-worker-{worker_digest}.py"
        if worker_entry.exists():
            if _digest(worker_entry) != worker_digest:
                raise ForecastProviderError(
                    "staged worker artifact has conflicting content",
                    code="WORKER_ARTIFACT_CONFLICT",
                )
        else:
            shutil.copyfile(worker_source, worker_entry)
        self._python_executable = str(worker_python)
        self._git_executable = git_executable
        self._kronos_repository = repository
        self._cache_directory = cache
        self._worker_entry = worker_entry
        self._timeout_seconds = timeout_seconds

    def forecast(self, request: ForecastRequest) -> WorkerForecast:
        command = [
            self._python_executable,
            "-I",
            "-B",
            str(self._worker_entry),
            "--kronos-repository",
            str(self._kronos_repository),
            "--git-executable",
            str(self._git_executable),
        ]
        try:
            completed = subprocess.run(
                command,
                input=request.model_dump_json(),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                check=False,
                shell=False,
                cwd=self._cache_directory,
                env=isolated_worker_environment(
                    os.environ,
                    cache_directory=self._cache_directory,
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise ForecastProviderError("Kronos worker timed out", code="WORKER_TIMEOUT") from exc
        except OSError as exc:
            raise ForecastProviderError(
                "Kronos worker could not launch", code="WORKER_LAUNCH_ERROR"
            ) from exc
        if completed.returncode != 0:
            # Worker stderr is intentionally not reflected into durable records or CLI output.
            raise ForecastProviderError("Kronos worker exited unsuccessfully", code="WORKER_FAILED")
        try:
            result = WorkerForecast.model_validate_json(completed.stdout)
        except ValidationError as exc:
            raise ForecastProviderError(
                "Kronos worker returned an invalid artifact",
                code="WORKER_ARTIFACT_INVALID",
            ) from exc
        if result.request_id != request.request_id:
            raise ForecastProviderError(
                "Kronos worker returned the wrong request identity",
                code="WORKER_IDENTITY_MISMATCH",
            )
        return result
