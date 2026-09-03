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
from quantbot.forecasting.models import ForecastRequest, KronosConfig
from quantbot.forecasting.snapshots import build_forecast_request


def test_worker_module_import_does_not_load_heavy_model_dependencies() -> None:
    assert "torch" not in sys.modules
    assert "pandas" not in sys.modules
    assert "huggingface_hub" not in sys.modules


def _raw_request(config: KronosConfig | None = None) -> dict[str, object]:
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
        config=config or KronosConfig(sample_count=1),
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


def test_dependabot_cannot_propose_bumps_the_worker_contract_would_reject() -> None:
    """The recurring failure this config exists to stop, guarded rather than remembered.

    `requirements-kronos-worker.txt` is an attestation manifest, not a dependency list. It is
    mirrored by `EXPECTED_PACKAGE_VERSIONS`, checked at runtime, and versioned alongside
    `WORKER_ENVIRONMENT_ID` and the pinned model hashes -- because a shadow forecast is
    attributable to the environment that produced it.

    Dependabot cannot update both files, so its proposals here arrive red and stay red. A package
    added to the manifest without a matching ignore would restart that cycle silently, so the
    coverage is asserted rather than trusted.

    Security advisories are deliberately still allowed through: the ignores cover
    `version-update:*` only.
    """
    import yaml

    root = Path(__file__).parents[3]
    pinned = (root / "requirements-kronos-worker.txt").read_text(encoding="utf-8")
    manifest = {
        line.split("==", maxsplit=1)[0]
        for raw in pinned.splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    }
    config = yaml.safe_load((root / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    uv_updates = next(
        entry for entry in config["updates"] if entry["package-ecosystem"] == "uv"
    )
    ignored = {entry["dependency-name"] for entry in uv_updates["ignore"]}

    uncovered = manifest - ignored
    assert not uncovered, (
        f"{sorted(uncovered)} are pinned in the worker attestation manifest but Dependabot may "
        "still propose version bumps for them, which the contract test will reject every time"
    )

    for entry in uv_updates["ignore"]:
        assert all(kind.startswith("version-update:") for kind in entry["update-types"]), (
            f"{entry['dependency-name']} suppresses more than version updates; a security "
            "advisory here is the one case worth interrupting for"
        )


# --- the pinned artifact is what stops a downloaded model executing code ------------------------
#
# CVE-2025-32434 is a remote-code-execution in `torch.load`, and this worker is a thing that loads
# a model someone else published. Nine open advisories against the pinned `torch==2.5.1` are
# unreachable here, but "unreachable" was an untested claim until these tests existed, which is
# the weakest possible basis for dismissing a critical alert. Each test below is one leg of that
# argument, and deleting the corresponding check turns it red.


def _weight(directory: Path, name: str, payload: bytes = b"weights") -> Path:
    path = directory / name
    path.write_bytes(payload)
    return path


def _sha(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_an_artifact_without_safetensors_is_refused_rather_than_loaded(tmp_path: Path) -> None:
    """The CVE's precondition, refused at the door.

    `torch.load` reaching a pickle is what CVE-2025-32434 needs. A snapshot offering only
    `pytorch_model.bin` is exactly that, and it never gets as far as `from_pretrained`.
    """
    _weight(tmp_path, "pytorch_model.bin")

    with pytest.raises(worker.WorkerContractError, match="exactly one safetensors"):
        worker._verify_single_weight(tmp_path, "irrelevant")


def test_an_ambiguous_artifact_is_refused_rather_than_resolved(tmp_path: Path) -> None:
    """Two weights mean the hash below verifies one file and the loader may read the other.

    Picking the first would make the verification decorative -- it would prove something true
    about a file nobody loaded.
    """
    _weight(tmp_path, "model-00001.safetensors")
    _weight(tmp_path, "model-00002.safetensors", b"different")

    with pytest.raises(worker.WorkerContractError, match="exactly one safetensors"):
        worker._verify_single_weight(tmp_path, "irrelevant")


def test_a_weight_that_is_not_the_pinned_one_is_refused(tmp_path: Path) -> None:
    """Otherwise the pin records an intention rather than enforcing one."""
    path = _weight(tmp_path, "model.safetensors")

    worker._verify_single_weight(tmp_path, _sha(path))  # the pinned artifact loads

    path.write_bytes(b"substituted")
    with pytest.raises(worker.WorkerContractError, match="weight hash does not match"):
        worker._verify_single_weight(tmp_path, _sha(_weight(tmp_path, "other.tmp", b"weights")))


def test_a_substituted_config_is_refused(tmp_path: Path) -> None:
    """`config.json` chooses the architecture that interprets the weights, so it is as
    load-bearing as the weights themselves and is pinned the same way.
    """
    config = tmp_path / "config.json"
    config.write_text('{"model_type": "kronos"}', encoding="utf-8")

    worker._verify_config(tmp_path, _sha(config))

    config.write_text('{"model_type": "something-else"}', encoding="utf-8")
    with pytest.raises(worker.WorkerContractError, match="config hash does not match"):
        worker._verify_config(tmp_path, _sha(tmp_path / "config.json") + "x")


def test_a_missing_config_is_refused_rather_than_defaulted(tmp_path: Path) -> None:
    """A default architecture would be an unpinned one."""
    with pytest.raises(worker.WorkerContractError, match="config hash does not match"):
        worker._verify_config(tmp_path, "0" * 64)


def test_every_artifact_is_verified_before_anything_is_loaded() -> None:
    """The ordering *is* the protection, and it is the one a refactor would quietly lose.

    Verification after `from_pretrained` would be a report on code that had already run. All four
    checks must therefore precede both loads in the source, asserted structurally because no
    unit test can observe an RCE that already happened.
    """
    import ast

    source = Path(worker.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            isinstance(inner, ast.Attribute) and inner.attr == "from_pretrained"
            for inner in ast.walk(node)
        )
    )

    verifications = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"_verify_single_weight", "_verify_config"}
    ]
    loads = [
        node.lineno
        for node in ast.walk(function)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "from_pretrained"
    ]

    assert len(verifications) == 4, "model and tokenizer, weight and config"
    assert len(loads) == 2, "model and tokenizer"
    assert max(verifications) < min(loads), (
        "an artifact is verified before it is loaded, never after"
    )


def test_the_two_copies_of_the_pinned_contract_agree() -> None:
    """The worker's pins and `models.py`'s pins are the same nine values, or attribution lies.

    `kronos_worker.py` cannot import from `quantbot` -- it runs in an isolated venv, and that
    isolation is what makes its attestation mean anything. So every pinned constant exists twice,
    and nothing asserted the copies agreed.

    They did agree when this was written (2026-08-30), so this closes a latent risk rather than a
    live defect. Had they drifted, `KronosConfig` would claim one set of pins while the worker
    verified another, and a forecast would be attributed to an environment identity that did not
    describe what verified it. Provenance is the one thing here with no independent check: a
    result whose recorded environment is wrong cannot be caught by looking at the result.

    The duplication itself is correct and stays. See `docs/worker-environment-identity.md` for the
    #137 decision on how this identity behaves across execution backends.
    """
    from quantbot.forecasting import models

    config = KronosConfig()
    assert (worker.MODEL_ID, worker.TOKENIZER_ID) == (config.model_id, config.tokenizer_id)
    assert worker.MODEL_REVISION == models.KRONOS_MODEL_REVISION
    assert worker.MODEL_WEIGHT_SHA256 == models.KRONOS_MODEL_WEIGHT_SHA256
    assert worker.MODEL_CONFIG_SHA256 == models.KRONOS_MODEL_CONFIG_SHA256
    assert worker.TOKENIZER_REVISION == models.KRONOS_TOKENIZER_REVISION
    assert worker.TOKENIZER_WEIGHT_SHA256 == models.KRONOS_TOKENIZER_WEIGHT_SHA256
    assert worker.TOKENIZER_CONFIG_SHA256 == models.KRONOS_TOKENIZER_CONFIG_SHA256
    assert worker.WORKER_ENVIRONMENT_ID == models.KRONOS_WORKER_ENVIRONMENT_ID


def test_the_retired_v1_environment_record_is_never_rewritten() -> None:
    """Historical attribution is the whole reason a version bump mints a new identity (#200).

    Every shadow forecast produced before this change ran under `torch==2.5.1`. If a later edit
    "tidied" the v1 entry to the current package set, those rows would silently become forecasts
    attributed to an environment that never ran them -- and provenance is the one thing here with
    no independent check, because a result whose recorded environment is wrong cannot be caught by
    looking at the result.

    So the v1 record is asserted literally rather than derived. A future v3 adds a key; it does
    not touch this one.
    """
    assert worker.WORKER_ENVIRONMENTS["kronos-worker-py311-requirements-v1"] == {
        "einops": "0.8.1",
        "huggingface-hub": "0.33.1",
        "numpy": "1.26.4",
        "pandas": "2.2.2",
        "safetensors": "0.6.2",
        "torch": "2.5.1",
        "tqdm": "4.67.1",
    }


def test_the_running_attestation_is_derived_from_the_declared_identity() -> None:
    """`EXPECTED_PACKAGE_VERSIONS` cannot drift from `WORKER_ENVIRONMENT_ID`, by construction."""
    assert worker.WORKER_ENVIRONMENT_ID in worker.WORKER_ENVIRONMENTS
    assert (
        worker.EXPECTED_PACKAGE_VERSIONS
        is worker.WORKER_ENVIRONMENTS[worker.WORKER_ENVIRONMENT_ID]
    )
    assert worker.EXPECTED_PACKAGE_VERSIONS["torch"] == "2.13.0", (
        "the patched release #200 qualified; a change here is a new environment identity"
    )


def test_a_retired_environment_can_be_read_but_never_run() -> None:
    """v1 rows must keep deserialising; nothing may be produced under a retired identity.

    Both halves matter. Dropping v1 from the `Literal` would make every historical forecast
    unreadable, which destroys the evidence the versioning exists to preserve. Leaving it
    *runnable* would let a caller quietly keep producing forecasts on the vulnerable stack.

    The second half is enforced by the worker rather than by the model: `_validate_request`
    refuses any request whose `worker_environment_id` is not the running worker's own, so a v1
    config cannot reach a v2 worker whatever constructs it.
    """
    from quantbot.forecasting import models

    assert models.KRONOS_WORKER_ENVIRONMENT_ID == "kronos-worker-py311-requirements-v2"
    assert KronosConfig().worker_environment_id == models.KRONOS_WORKER_ENVIRONMENT_ID
    assert set(models.RETIRED_KRONOS_WORKER_ENVIRONMENT_IDS) == (
        set(worker.WORKER_ENVIRONMENTS) - {worker.WORKER_ENVIRONMENT_ID}
    )

    historical = KronosConfig(worker_environment_id="kronos-worker-py311-requirements-v1")
    assert historical.worker_environment_id == "kronos-worker-py311-requirements-v1"

    # The property that actually matters for a stored row: a whole v1 request survives a
    # round-trip through JSON, including its `request_id`. That id hashes the config, so the v1
    # identity is part of it -- a v1 row cannot be re-derived under the v2 default, and if the
    # Literal ever dropped v1 this would fail rather than the row quietly becoming unreadable.
    stored = _raw_request(
        config=KronosConfig(
            sample_count=1,
            worker_environment_id="kronos-worker-py311-requirements-v1",
        )
    )
    reloaded = ForecastRequest.model_validate(stored)
    assert reloaded.config.worker_environment_id == "kronos-worker-py311-requirements-v1"
    assert reloaded.model_dump(mode="json") == stored

    payload = _raw_request()
    config = payload["config"]
    assert isinstance(config, dict)
    config["worker_environment_id"] = "kronos-worker-py311-requirements-v1"
    with pytest.raises(worker.WorkerContractError, match="not the pinned shadow value"):
        worker._validate_request(payload)


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows peak-memory probe is win32 only")
def test_the_windows_peak_memory_probe_actually_measures_something() -> None:
    """A null reading is a valid answer, which is exactly why the break went unseen (#200).

    `_peak_process_memory_bytes` returns `None` when the API call fails, and `None` is also the
    honest answer on a platform that cannot report the figure -- so a broken probe and an
    unavailable one are indistinguishable in the record. They were not distinguishable in the
    code either: without `restype`/`argtypes`, ctypes truncated the 64-bit `GetCurrentProcess`
    pseudo-handle to a 32-bit `-1`, the call failed, and every Kronos forecast ever produced on
    Windows recorded `peak_process_memory_bytes: null`. Found by running the environment
    qualification for #200 and noticing the field was empty on both sides.

    Asserting "not None" alone would pass on a probe that returned zero, so the reading is
    checked against the interpreter's own footprint: this process has certainly touched more
    than 8 MB.
    """
    peak = worker._peak_process_memory_bytes()

    assert peak is not None, "the probe reported nothing on a platform that can measure it"
    assert peak > 8 * 1024 * 1024, f"implausible peak working set: {peak} bytes"
