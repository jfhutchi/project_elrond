from __future__ import annotations

import io
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.forecasting import kronos_worker as worker
from quantbot.forecasting.models import KronosConfig
from quantbot.forecasting.snapshots import build_forecast_request


def test_worker_module_import_does_not_load_heavy_model_dependencies() -> None:
    assert "torch" not in sys.modules
    assert "pandas" not in sys.modules
    assert "huggingface_hub" not in sys.modules


def _raw_request() -> dict[str, object]:
    as_of = datetime(2026, 8, 14, 20, tzinfo=UTC)
    request = build_forecast_request(
        bars=[
            Bar(
                symbol="AAPL",
                timestamp=as_of,
                open="99",
                high="101",
                low="98",
                close="100",
                volume="1000",
                adjustment="1",
            )
        ],
        symbol="AAPL",
        as_of=as_of,
        provider="alpaca:iex:all:1day",
        vintage_id="worker-contract-v1",
        available_at=as_of,
        adjustment_metadata={
            "adjustment": "all",
            "feed": "iex",
            "provider": "alpaca",
            "timeframe": "1Day",
        },
        lookback=1,
        horizon=1,
        config=KronosConfig(sample_count=1),
    )
    return request.model_dump(mode="json")


def test_standalone_contract_rejects_artifact_pin_override() -> None:
    payload = _raw_request()
    config = payload["config"]
    assert isinstance(config, dict)
    config["model_revision"] = "unreviewed-revision"

    with pytest.raises(worker.WorkerContractError, match="not the pinned shadow value"):
        worker._validate_request(payload)


def test_worker_environment_attestation_enforces_python_and_exact_packages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker.sys, "version_info", (3, 11, 9))
    monkeypatch.setattr(
        worker.importlib.metadata,
        "version",
        lambda distribution: worker.EXPECTED_PACKAGE_VERSIONS[distribution],
    )
    assert worker._verify_worker_environment() == {
        name.replace("-", "_"): version
        for name, version in worker.EXPECTED_PACKAGE_VERSIONS.items()
    }

    def wrong_torch_version(distribution: str) -> str:
        if distribution == "torch":
            return "0.0"
        return worker.EXPECTED_PACKAGE_VERSIONS[distribution]

    monkeypatch.setattr(worker.importlib.metadata, "version", wrong_torch_version)
    with pytest.raises(worker.WorkerContractError, match="version is not pinned"):
        worker._verify_worker_environment()


def test_application_interpreter_is_rejected_as_worker_environment() -> None:
    with pytest.raises(worker.WorkerContractError, match="must not be able to import Elrond"):
        worker._ensure_elrond_is_unavailable()


def test_standalone_requirements_match_worker_attestation_manifest() -> None:
    requirements = Path(__file__).parents[3] / "requirements-kronos-worker.txt"
    declared = dict(
        line.split("==", maxsplit=1)
        for raw in requirements.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )
    assert declared == worker.EXPECTED_PACKAGE_VERSIONS


def test_checkout_verification_rejects_git_ignored_import_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    responses = iter(
        (
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout=worker.KRONOS_CODE_REVISION + "\n",
                stderr="",
            ),
            subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="!! model/__pycache__/\n",
                stderr="",
            ),
        )
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    with pytest.raises(worker.WorkerContractError, match="must be clean"):
        worker._verify_kronos_checkout(
            Path("Kronos"),
            worker.KRONOS_CODE_REVISION,
            git_executable=Path("git"),
        )

    assert "--ignored" in calls[1]


def test_worker_materializes_source_from_the_pinned_git_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive_bytes = io.BytesIO()
    source_bytes = b"PINNED = True\n"
    with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
        member = tarfile.TarInfo("model.py")
        member.size = len(source_bytes)
        archive.addfile(member, io.BytesIO(source_bytes))
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout=archive_bytes.getvalue(),
            stderr=b"",
        )

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    staged = worker._extract_pinned_revision(
        Path("Kronos"),
        worker.KRONOS_CODE_REVISION,
        git_executable=Path("git"),
    )
    try:
        assert (Path(staged.name) / "model.py").read_bytes() == source_bytes
    finally:
        staged.cleanup()

    assert calls == [
        [
            "git",
            "-C",
            "Kronos",
            "archive",
            "--format=tar",
            worker.KRONOS_CODE_REVISION,
        ]
    ]


def test_worker_suppresses_upstream_stdout_before_protocol_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    expected = {"request_id": "request-1"}

    def noisy_run(
        _payload: object, *, kronos_repository: Path, git_executable: Path
    ) -> dict[str, object]:
        del kronos_repository, git_executable
        print("Loading weights from local directory")
        return expected

    monkeypatch.setattr(worker, "_run_forecast", noisy_run)
    assert worker.run({}, kronos_repository=Path("Kronos"), git_executable=Path("git")) == expected
    assert capsys.readouterr().out == ""


def test_network_denial_preserves_socket_type_and_late_ssl_consumers() -> None:
    code = """
import importlib.util
import socket
import sys
spec = importlib.util.spec_from_file_location("standalone_worker", sys.argv[1])
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module._deny_external_authority()
import urllib.request
assert isinstance(socket.socket, type)
sock = socket.socket()
try:
    try:
        sock.connect(("127.0.0.1", 1))
    except PermissionError:
        pass
    else:
        raise AssertionError("network connect was not denied")
finally:
    sock.close()
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code, str(Path(worker.__file__).resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
