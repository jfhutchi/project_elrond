"""Standalone Kronos JSON worker.

This file intentionally imports no ``quantbot`` module. The provider executes its absolute path
with a dedicated Python environment that contains model dependencies but not Elrond. Startup
fails closed if that environment can import ``quantbot``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import importlib.metadata
import importlib.util
import io
import json
import os
import platform
import random
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KRONOS_CODE_REVISION = "67b630e67f6a18c9e9be918d9b4337c960db1e9a"
MODEL_ID = "NeoQuasar/Kronos-small"
MODEL_REVISION = "901c26c1332695a2a8f243eb2f37243a37bea320"
MODEL_WEIGHT_SHA256 = "b082dfcbd8e8c142a725c8bbb99781802f38fec81210e13479effb32b3c3e020"
MODEL_CONFIG_SHA256 = "5e0f6a605d5f81b5c9b559fe5cf716a1acb041c744e6f41bd05b097b7a685396"
TOKENIZER_ID = "NeoQuasar/Kronos-Tokenizer-base"
TOKENIZER_REVISION = "0e0117387f39004a9016484a186a908917e22426"
TOKENIZER_WEIGHT_SHA256 = "59d85f6af76a2c3b8240ea06cb21db4213b4eeca053f246b23e29cf832fc6bee"
TOKENIZER_CONFIG_SHA256 = "2366e7ccfec76cbc19cf3c4c1b9c5d901be336ca1e83f2d2292c9bff381b77a2"
INTEGRATION_VERSION = "kronos-shadow-v1"
INPUT_TRANSFORM_VERSION = "ohlcv-no-amount-v1"
WORKER_ENVIRONMENT_ID = "kronos-worker-py311-requirements-v1"
EXPECTED_PACKAGE_VERSIONS = {
    "einops": "0.8.1",
    "huggingface-hub": "0.33.1",
    "numpy": "1.26.4",
    "pandas": "2.2.2",
    "safetensors": "0.6.2",
    "torch": "2.5.1",
    "tqdm": "4.67.1",
}


class WorkerContractError(RuntimeError):
    """Raised when the isolated environment or pinned artifact violates the contract."""


class _DeniedSocket(socket.socket):
    """Type-compatible socket whose network operations fail closed."""

    def connect(self, _address: Any) -> None:
        raise PermissionError("network is disabled in the Kronos worker")

    def connect_ex(self, _address: Any) -> int:
        raise PermissionError("network is disabled in the Kronos worker")

    def bind(self, _address: Any) -> None:
        raise PermissionError("network is disabled in the Kronos worker")

    def listen(self, _backlog: int = 0) -> None:
        raise PermissionError("network is disabled in the Kronos worker")

    def sendto(self, *_args: Any, **_kwargs: Any) -> int:
        raise PermissionError("network is disabled in the Kronos worker")


def _ensure_elrond_is_unavailable() -> None:
    if "quantbot" in sys.modules or importlib.util.find_spec("quantbot") is not None:
        raise WorkerContractError("worker environment must not be able to import Elrond")


def _verify_worker_environment() -> dict[str, str]:
    if sys.version_info[:2] != (3, 11):
        raise WorkerContractError("worker requires the pinned Python 3.11 environment")
    observed: dict[str, str] = {}
    for distribution, expected in EXPECTED_PACKAGE_VERSIONS.items():
        try:
            actual = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise WorkerContractError(f"worker dependency is missing: {distribution}") from exc
        if actual != expected:
            raise WorkerContractError(f"worker dependency version is not pinned: {distribution}")
        observed[distribution.replace("-", "_")] = actual
    return observed


def _deny_external_authority() -> None:
    """Remove common network/process escape hatches before importing upstream code."""

    def denied(*_args: object, **_kwargs: object) -> Any:
        raise PermissionError("network and child processes are disabled in the Kronos worker")

    socket_module: Any = socket
    socket_module.socket = _DeniedSocket
    for module, names in (
        (socket, ("create_connection", "getaddrinfo")),
        (subprocess, ("Popen", "run")),
        (os, ("system", "popen")),
    ):
        for name in names:
            setattr(module, name, denied)

    retained = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "CUDA_VISIBLE_DEVICES",
            "HF_HOME",
            "HF_HUB_OFFLINE",
            "PYTHONDONTWRITEBYTECODE",
            "PYTHONHASHSEED",
            "PYTHONNOUSERSITE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "TOKENIZERS_PARALLELISM",
            "TRANSFORMERS_OFFLINE",
            "WINDIR",
        }
    }
    os.environ.clear()
    os.environ.update(retained)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_single_weight(snapshot: str | Path, expected_sha256: str) -> None:
    candidates = sorted(Path(snapshot).rglob("*.safetensors"))
    if len(candidates) != 1:
        raise WorkerContractError("pinned artifact must contain exactly one safetensors file")
    if _sha256_file(candidates[0]) != expected_sha256:
        raise WorkerContractError("pinned artifact weight hash does not match request")


def _verify_config(snapshot: str | Path, expected_sha256: str) -> None:
    config = Path(snapshot) / "config.json"
    if not config.is_file() or _sha256_file(config) != expected_sha256:
        raise WorkerContractError("pinned artifact config hash does not match request")


def _verify_kronos_checkout(
    repository: Path, expected_revision: str, *, git_executable: Path
) -> None:
    try:
        completed = subprocess.run(
            [str(git_executable), "-C", str(repository), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerContractError("unable to verify the Kronos checkout") from exc
    if completed.returncode != 0 or completed.stdout.strip() != expected_revision:
        raise WorkerContractError("Kronos checkout revision does not match request")
    clean = subprocess.run(
        [
            str(git_executable),
            "-C",
            str(repository),
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        shell=False,
    )
    if clean.returncode != 0 or clean.stdout:
        raise WorkerContractError("Kronos checkout must be clean")


def _extract_pinned_revision(
    repository: Path, expected_revision: str, *, git_executable: Path
) -> tempfile.TemporaryDirectory[str]:
    """Materialize only tracked bytes from the pinned commit into an ephemeral directory."""
    try:
        completed = subprocess.run(
            [
                str(git_executable),
                "-C",
                str(repository),
                "archive",
                "--format=tar",
                expected_revision,
            ],
            capture_output=True,
            text=False,
            timeout=30,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorkerContractError("unable to materialize the pinned Kronos revision") from exc
    if completed.returncode != 0 or not completed.stdout:
        raise WorkerContractError("unable to materialize the pinned Kronos revision")

    staged = tempfile.TemporaryDirectory(prefix="kronos-source-", dir=Path.cwd())
    destination = Path(staged.name).resolve()
    try:
        with tarfile.open(fileobj=io.BytesIO(completed.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                target = (destination / member.name).resolve()
                try:
                    target.relative_to(destination)
                except ValueError as exc:
                    raise WorkerContractError("Kronos archive contains an unsafe path") from exc
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise WorkerContractError("Kronos archive contains an unsupported entry")
                source = archive.extractfile(member)
                if source is None:
                    raise WorkerContractError("Kronos archive entry could not be read")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
    except (OSError, tarfile.TarError, WorkerContractError):
        staged.cleanup()
        raise
    return staged


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise WorkerContractError(f"{name} must be an object")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerContractError(f"{name} must be a non-empty string")
    return value


def _integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerContractError(f"{name} must be an integer")
    return value


def _sequence(value: object, name: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise WorkerContractError(f"{name} must be an array")
    return value


def _parse_utc(value: object, name: str) -> datetime:
    text = _string(value, name)
    parsed = datetime.fromisoformat(
        text.removesuffix("Z") + ("+00:00" if text.endswith("Z") else "")
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise WorkerContractError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _validate_request(payload: object) -> Mapping[str, Any]:
    request = _mapping(payload, "request")
    _string(request.get("request_id"), "request_id")
    lookback = _integer(request.get("lookback"), "lookback")
    horizon = _integer(request.get("horizon"), "horizon")
    if not 1 <= lookback <= 512 or not 1 <= horizon <= 128:
        raise WorkerContractError("lookback or horizon is outside the supported range")

    snapshot = _mapping(request.get("snapshot"), "snapshot")
    as_of = _parse_utc(snapshot.get("as_of"), "snapshot.as_of")
    symbol = _string(snapshot.get("symbol"), "snapshot.symbol")
    bars = _sequence(snapshot.get("bars"), "snapshot.bars")
    if len(bars) != lookback:
        raise WorkerContractError("worker requires a complete lookback snapshot")
    prior_timestamp: datetime | None = None
    for index, raw_bar in enumerate(bars):
        bar = _mapping(raw_bar, f"snapshot.bars[{index}]")
        if _string(bar.get("symbol"), "bar.symbol") != symbol:
            raise WorkerContractError("source bar symbol does not match request")
        timestamp = _parse_utc(bar.get("timestamp"), "bar.timestamp")
        if timestamp > as_of or (prior_timestamp is not None and timestamp <= prior_timestamp):
            raise WorkerContractError("source bars violate point-in-time ordering")
        prior_timestamp = timestamp
        for field in ("open", "high", "low", "close", "volume"):
            float(_string(bar.get(field), f"bar.{field}"))

    future = _sequence(request.get("future_timestamps"), "future_timestamps")
    if len(future) != horizon:
        raise WorkerContractError("future timestamp count does not match horizon")
    prior_future = as_of
    for value in future:
        timestamp = _parse_utc(value, "future timestamp")
        if timestamp <= prior_future:
            raise WorkerContractError("future timestamps must be ordered after as_of")
        prior_future = timestamp

    config = _mapping(request.get("config"), "config")
    pinned = {
        "device": "cpu",
        "input_transform_version": INPUT_TRANSFORM_VERSION,
        "integration_version": INTEGRATION_VERSION,
        "kronos_code_revision": KRONOS_CODE_REVISION,
        "model_config_sha256": MODEL_CONFIG_SHA256,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_weight_sha256": MODEL_WEIGHT_SHA256,
        "tokenizer_config_sha256": TOKENIZER_CONFIG_SHA256,
        "tokenizer_id": TOKENIZER_ID,
        "tokenizer_revision": TOKENIZER_REVISION,
        "tokenizer_weight_sha256": TOKENIZER_WEIGHT_SHA256,
        "worker_environment_id": WORKER_ENVIRONMENT_ID,
    }
    for field, expected in pinned.items():
        if _string(config.get(field), field) != expected:
            raise WorkerContractError(f"{field} is not the pinned shadow value")
    for field in (
        "model_revision",
        "model_weight_sha256",
        "model_config_sha256",
        "tokenizer_id",
        "tokenizer_revision",
        "tokenizer_weight_sha256",
        "tokenizer_config_sha256",
        "kronos_code_revision",
    ):
        _string(config.get(field), field)
    sample_count = _integer(config.get("sample_count"), "sample_count")
    max_context = _integer(config.get("max_context"), "max_context")
    if not 1 <= sample_count <= 100 or not 2 <= max_context <= 512:
        raise WorkerContractError("sampling or context setting is outside the supported range")
    return request


def _runtime_hash(*, device: str, package_versions: Mapping[str, str]) -> str:
    payload = {
        "device": device,
        "packages": package_versions,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "worker_sha256": _sha256_file(Path(__file__)),
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _candle_from_mapping(value: object) -> dict[str, str]:
    candle = _mapping(value, "forecast candle")
    required = {"open", "high", "low", "close"}
    if not required <= set(candle):
        raise WorkerContractError("Kronos output is missing OHLC columns")
    return {
        "open": str(candle["open"]),
        "high": str(candle["high"]),
        "low": str(candle["low"]),
        "close": str(candle["close"]),
        "volume": str(candle.get("volume", "0")),
    }


def _peak_process_memory_bytes() -> int | None:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        success = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        )
        return int(counters.PeakWorkingSetSize) if success else None
    else:
        # `else`, not a fallthrough. The guard is `sys.platform` so that mypy narrows it and
        # checks each half on the platform that runs it -- and once it narrows, anything after
        # a branch that always returns reads as unreachable rather than as the other platform.
        try:
            import resource
        except ImportError:
            return None
        resource_module: Any = resource
        maximum = resource_module.getrusage(resource_module.RUSAGE_SELF).ru_maxrss
        multiplier = 1 if platform.system() == "Darwin" else 1024
        return int(maximum * multiplier)


def _run_forecast(
    payload: object, *, kronos_repository: Path, git_executable: Path
) -> dict[str, object]:
    """Run pinned zero-shot Kronos-small and retain each sampled path separately."""
    request = _validate_request(payload)
    versions = _verify_worker_environment()
    config = _mapping(request["config"], "config")
    snapshot = _mapping(request["snapshot"], "snapshot")
    _verify_kronos_checkout(
        kronos_repository,
        _string(config["kronos_code_revision"], "kronos_code_revision"),
        git_executable=git_executable,
    )
    staged_revision = _extract_pinned_revision(
        kronos_repository,
        _string(config["kronos_code_revision"], "kronos_code_revision"),
        git_executable=git_executable,
    )
    kronos_source = Path(staged_revision.name).resolve()
    pandas = importlib.import_module("pandas")
    numpy = importlib.import_module("numpy")
    torch = importlib.import_module("torch")
    hub = importlib.import_module("huggingface_hub")
    snapshot_download = hub.snapshot_download
    # Resolve the trusted dependency stack before socket replacement; modules such as ssl define
    # socket subclasses at import time. Upstream Kronos code is still imported only after denial.
    importlib.import_module("ssl")
    _deny_external_authority()
    if str(kronos_source) not in sys.path:
        sys.path.insert(0, str(kronos_source))
    kronos_module = importlib.import_module("model")
    module_file = getattr(kronos_module, "__file__", None)
    if module_file is None:
        raise WorkerContractError("Kronos model import has no source origin")
    try:
        Path(module_file).resolve().relative_to(kronos_source)
    except ValueError as exc:
        raise WorkerContractError("Kronos model import escaped the pinned source") from exc

    model_id = _string(config["model_id"], "model_id")
    model_revision = _string(config["model_revision"], "model_revision")
    tokenizer_id = _string(config["tokenizer_id"], "tokenizer_id")
    tokenizer_revision = _string(config["tokenizer_revision"], "tokenizer_revision")
    model_snapshot = snapshot_download(
        repo_id=model_id, revision=model_revision, local_files_only=True
    )
    tokenizer_snapshot = snapshot_download(
        repo_id=tokenizer_id, revision=tokenizer_revision, local_files_only=True
    )
    _verify_single_weight(
        model_snapshot, _string(config["model_weight_sha256"], "model_weight_sha256")
    )
    _verify_config(model_snapshot, _string(config["model_config_sha256"], "model_config_sha256"))
    _verify_single_weight(
        tokenizer_snapshot,
        _string(config["tokenizer_weight_sha256"], "tokenizer_weight_sha256"),
    )
    _verify_config(
        tokenizer_snapshot,
        _string(config["tokenizer_config_sha256"], "tokenizer_config_sha256"),
    )

    device = _string(config["device"], "device")
    initialized_at = time.perf_counter()
    tokenizer = kronos_module.KronosTokenizer.from_pretrained(str(tokenizer_snapshot))
    model = kronos_module.Kronos.from_pretrained(str(model_snapshot))
    tokenizer.eval()
    model.eval()
    predictor = kronos_module.KronosPredictor(
        model,
        tokenizer,
        device=device,
        max_context=_integer(config["max_context"], "max_context"),
        clip=5,
    )
    initialization_seconds = time.perf_counter() - initialized_at

    torch.use_deterministic_algorithms(True)
    if hasattr(torch.backends, "cudnn"):
        torch.backends.cudnn.benchmark = False
    bars = _sequence(snapshot["bars"], "snapshot.bars")
    max_context = _integer(config["max_context"], "max_context")
    selected_bars = bars[-max_context:]
    frame = pandas.DataFrame(
        [
            {
                "open": float(_mapping(bar, "bar")["open"]),
                "high": float(_mapping(bar, "bar")["high"]),
                "low": float(_mapping(bar, "bar")["low"]),
                "close": float(_mapping(bar, "bar")["close"]),
                "volume": float(_mapping(bar, "bar")["volume"]),
            }
            for bar in selected_bars
        ]
    )
    x_timestamps = pandas.Series(
        [_parse_utc(_mapping(bar, "bar")["timestamp"], "bar.timestamp") for bar in selected_bars]
    )
    y_timestamps = pandas.Series(
        [_parse_utc(value, "future timestamp") for value in request["future_timestamps"]]
    )

    sample_count = _integer(config["sample_count"], "sample_count")
    horizon = _integer(request["horizon"], "horizon")
    seed_base = _integer(config["seed"], "seed")
    inference_started = time.perf_counter()
    paths: list[list[dict[str, str]]] = []
    with torch.inference_mode():
        for sample_index in range(sample_count):
            seed = seed_base + sample_index
            random.seed(seed)
            numpy.random.seed(seed % (2**32))
            torch.manual_seed(seed)
            prediction = predictor.predict(
                df=frame,
                x_timestamp=x_timestamps,
                y_timestamp=y_timestamps,
                pred_len=horizon,
                T=float(_string(config["temperature"], "temperature")),
                top_k=_integer(config["top_k"], "top_k"),
                top_p=float(_string(config["top_p"], "top_p")),
                sample_count=1,
                verbose=False,
            )
            records = prediction.to_dict(orient="records")
            if len(records) != horizon:
                raise WorkerContractError("Kronos output horizon does not match request")
            paths.append([_candle_from_mapping(record) for record in records])
    inference_seconds = time.perf_counter() - inference_started
    return {
        "request_id": request["request_id"],
        "sample_paths": paths,
        "model_revision": model_revision,
        "tokenizer_revision": tokenizer_revision,
        "kronos_code_revision": config["kronos_code_revision"],
        "generated_at": datetime.now(UTC).isoformat(),
        "runtime_environment_hash": _runtime_hash(device=device, package_versions=versions),
        "device": device,
        "model_initialization_seconds": format(initialization_seconds, ".9f"),
        "inference_seconds": format(inference_seconds, ".9f"),
        "peak_process_memory_bytes": _peak_process_memory_bytes(),
        "peak_gpu_memory_bytes": None,
    }


def run(payload: object, *, kronos_repository: Path, git_executable: Path) -> dict[str, object]:
    """Reserve stdout for the single JSON protocol document emitted by ``main``."""
    with contextlib.redirect_stdout(io.StringIO()):
        return _run_forecast(
            payload,
            kronos_repository=kronos_repository,
            git_executable=git_executable,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantbot-kronos-worker")
    parser.add_argument("--kronos-repository", required=True, type=Path)
    parser.add_argument("--git-executable", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _ensure_elrond_is_unavailable()
    args = build_parser().parse_args(argv)
    payload: object = json.loads(sys.stdin.read())
    result = run(
        payload,
        kronos_repository=args.kronos_repository.resolve(),
        git_executable=args.git_executable.resolve(),
    )
    sys.stdout.write(
        json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
