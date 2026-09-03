"""Qualify one Kronos worker environment against another on a frozen synthetic corpus (#200).

Changing `torch` is not a dependency bump. The worker's package set is an attestation manifest,
and a forecast is attributable to the exact environment that produced it -- so a new PyTorch is a
new `WORKER_ENVIRONMENT_ID`, and whether its numbers are the *same* numbers is a question that
has to be measured rather than assumed.

This script measures it. It drives a worker entry file directly, the way
`quantbot.forecasting.provider.KronosSubprocessProvider` does: same argument vector, same
allowlisted offline environment, same isolated interpreter flags. It builds the request as plain
JSON rather than through `KronosConfig`, so one script can drive a v1 worker and a v2 worker
without the model layer's `Literal` having to know about both at once.

**The corpus is synthetic and deliberately so.** The question here is whether two environments
compute the same function, not whether the model is any good. Synthetic bars from a fixed seed
are reproducible anywhere, consume no protected evaluation window, add nothing to the
multiple-testing burden, and exercise the identical code path. Using market data would answer no
extra question and would spend a real resource to do it.

**The tolerance policy is fixed before the comparison is read**, because a threshold chosen after
seeing the numbers is not a threshold:

1. Both environments must initialise the model and return a well-formed artifact. A failure is
   disqualifying on its own -- there is nothing to compare.
2. Output schema, sample count, horizon and OHLCV fields must match exactly. A shape change is a
   different artifact, not a numerical difference.
3. Every value must be finite.
4. Each environment must reproduce itself exactly across two runs at a fixed seed. If an
   environment is not self-consistent, a cross-environment difference measures noise rather than
   the version change, and the comparison says nothing.
5. Given 1-4, **bit-identical output across environments is the only result that makes v2 the
   same forecaster as v1.** Any deviation, however small, means v2 is a distinct implementation:
   its forecasts start a new evidence series and are not pooled with v1's, per the #137 decision
   in `docs/worker-environment-identity.md`.

Rule 5 is deliberately absolute rather than a tolerance band. A band would require knowing how
much drift is harmless for a downstream claim that has not been made yet, and picking one now
would be choosing a number to get a convenient answer. Elrond loses nothing by treating v2 as a
new series -- it loses the ability to tell them apart by pretending it is not.

Usage::

    python scripts/qualify_kronos_environment.py corpus --out corpus.json
    python scripts/qualify_kronos_environment.py run --corpus corpus.json \\
        --worker-entry <path to kronos_worker.py> --worker-python <venv python> \\
        --kronos-repository <clone> --cache-directory <HF_HOME> --out run-v1-a.json
    python scripts/qualify_kronos_environment.py compare \\
        --baseline run-v1-a.json run-v1-b.json --candidate run-v2-a.json run-v2-b.json \\
        --out qualification.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

#: Allowlisted parent variables, mirroring `provider._SAFE_PARENT_ENVIRONMENT`. Kept as a literal
#: rather than imported: this script must be able to drive a worker without Elrond's package
#: layout being importable, which is the same reason the worker itself duplicates its pins.
SAFE_PARENT_ENVIRONMENT = ("CUDA_VISIBLE_DEVICES", "SYSTEMROOT", "TEMP", "TMP", "WINDIR")

OHLCV = ("open", "high", "low", "close", "volume")


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def build_corpus(*, seed: int, symbol: str, bars: int, horizon: int) -> dict[str, Any]:
    """A deterministic daily-bar series with plausible drift and volatility.

    Plausible rather than real: the model should not be fed a degenerate constant series, or the
    comparison measures agreement on a case the model never sees. Nothing here is market data and
    nothing here is evidence about a market.
    """
    generator = random.Random(seed)
    start = datetime(2024, 1, 2, 21, 0, tzinfo=UTC)
    price = 100.0
    rows: list[dict[str, str]] = []
    timestamp = start
    for _ in range(bars):
        step = generator.gauss(0.0004, 0.012)
        close = price * math.exp(step)
        open_ = price * math.exp(generator.gauss(0.0, 0.004))
        high = max(open_, close) * math.exp(abs(generator.gauss(0.0, 0.005)))
        low = min(open_, close) * math.exp(-abs(generator.gauss(0.0, 0.005)))
        volume = 1_000_000 * math.exp(generator.gauss(0.0, 0.25))
        rows.append(
            {
                "symbol": symbol,
                "timestamp": timestamp.isoformat().replace("+00:00", "Z"),
                "open": f"{open_:.4f}",
                "high": f"{high:.4f}",
                "low": f"{low:.4f}",
                "close": f"{close:.4f}",
                "volume": f"{volume:.2f}",
            }
        )
        price = close
        timestamp += timedelta(days=1)

    future = [
        (timestamp + timedelta(days=index)).isoformat().replace("+00:00", "Z")
        for index in range(horizon)
    ]
    corpus = {
        "corpus_version": 1,
        "seed": seed,
        "symbol": symbol,
        "as_of": rows[-1]["timestamp"],
        "bars": rows,
        "future_timestamps": future,
    }
    corpus["corpus_hash"] = digest({key: corpus[key] for key in sorted(corpus)})
    return corpus


def load_worker(entry: Path) -> ModuleType:
    """Import a worker entry file only to read its pinned constants.

    Nothing executes at import: every check the worker performs lives behind `main`. Importing it
    is how this script reads the pins of the exact file it is about to run, rather than being
    told what they are.
    """
    spec = importlib.util.spec_from_file_location("kronos_worker_under_test", entry)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot import worker entry {entry}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_request(
    corpus: Mapping[str, Any],
    worker: ModuleType,
    *,
    seed: int,
    sample_count: int,
    max_context: int,
) -> dict[str, Any]:
    bars = list(corpus["bars"])
    config = {
        "device": "cpu",
        "input_transform_version": worker.INPUT_TRANSFORM_VERSION,
        "integration_version": worker.INTEGRATION_VERSION,
        "kronos_code_revision": worker.KRONOS_CODE_REVISION,
        "max_context": max_context,
        "model_config_sha256": worker.MODEL_CONFIG_SHA256,
        "model_id": worker.MODEL_ID,
        "model_revision": worker.MODEL_REVISION,
        "model_weight_sha256": worker.MODEL_WEIGHT_SHA256,
        "sample_count": sample_count,
        "seed": seed,
        "temperature": "0.6",
        "tokenizer_config_sha256": worker.TOKENIZER_CONFIG_SHA256,
        "tokenizer_id": worker.TOKENIZER_ID,
        "tokenizer_revision": worker.TOKENIZER_REVISION,
        "tokenizer_weight_sha256": worker.TOKENIZER_WEIGHT_SHA256,
        "top_k": 0,
        "top_p": "0.9",
        "worker_environment_id": worker.WORKER_ENVIRONMENT_ID,
    }
    return {
        "request_id": digest(
            {
                "config": config,
                "corpus_hash": corpus["corpus_hash"],
                "horizon": len(corpus["future_timestamps"]),
                "lookback": len(bars),
            }
        ),
        "lookback": len(bars),
        "horizon": len(corpus["future_timestamps"]),
        "snapshot": {"as_of": corpus["as_of"], "symbol": corpus["symbol"], "bars": bars},
        "future_timestamps": list(corpus["future_timestamps"]),
        "config": config,
    }


def run_worker(
    request: Mapping[str, Any],
    *,
    worker_entry: Path,
    worker_python: Path,
    kronos_repository: Path,
    cache_directory: Path,
    git_executable: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    environment = {
        key: value for key, value in os.environ.items() if key in SAFE_PARENT_ENVIRONMENT
    }
    environment.update(
        {
            "HF_HOME": str(cache_directory),
            "HF_HUB_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "TRANSFORMERS_OFFLINE": "1",
        }
    )
    started = datetime.now(UTC)
    completed = subprocess.run(
        [
            str(worker_python),
            "-I",
            "-B",
            str(worker_entry),
            "--kronos-repository",
            str(kronos_repository),
            "--git-executable",
            str(git_executable),
        ],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
        shell=False,
        cwd=str(cache_directory),
        env=environment,
    )
    wall_seconds = (datetime.now(UTC) - started).total_seconds()
    if completed.returncode != 0:
        return {
            "ok": False,
            "returncode": completed.returncode,
            "stderr_tail": completed.stderr[-4000:],
            "wall_seconds": wall_seconds,
        }
    return {
        "ok": True,
        "returncode": 0,
        "wall_seconds": wall_seconds,
        "request_id": request["request_id"],
        "response": json.loads(completed.stdout),
    }


def _paths(run: Mapping[str, Any]) -> list[list[dict[str, str]]]:
    response = run.get("response")
    if not isinstance(response, Mapping):
        return []
    paths = response.get("sample_paths")
    return list(paths) if isinstance(paths, list) else []


def _shape(run: Mapping[str, Any]) -> tuple[int, tuple[int, ...], tuple[str, ...]]:
    paths = _paths(run)
    fields: tuple[str, ...] = ()
    if paths and paths[0]:
        fields = tuple(sorted(paths[0][0]))
    return len(paths), tuple(len(path) for path in paths), fields


def _finite(run: Mapping[str, Any]) -> bool:
    for path in _paths(run):
        for candle in path:
            for field in OHLCV:
                try:
                    value = float(candle[field])
                except (KeyError, TypeError, ValueError):
                    return False
                if not math.isfinite(value):
                    return False
    return True


def _terminal_return(run: Mapping[str, Any], last_close: float) -> float | None:
    paths = _paths(run)
    if not paths or last_close == 0:
        return None
    closes = [float(path[-1]["close"]) for path in paths if path]
    if not closes:
        return None
    return sum(closes) / len(closes) / last_close - 1.0


def _deviation(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
    worst_absolute = 0.0
    worst_relative = 0.0
    differing = 0
    compared = 0
    for left_path, right_path in zip(_paths(left), _paths(right), strict=False):
        for left_candle, right_candle in zip(left_path, right_path, strict=False):
            for field in OHLCV:
                compared += 1
                a = float(left_candle[field])
                b = float(right_candle[field])
                if left_candle[field] != right_candle[field]:
                    differing += 1
                absolute = abs(a - b)
                worst_absolute = max(worst_absolute, absolute)
                if a != 0:
                    worst_relative = max(worst_relative, absolute / abs(a))
    return {
        "values_compared": compared,
        "values_differing": differing,
        "max_absolute_deviation": worst_absolute,
        "max_relative_deviation": worst_relative,
    }


def compare(
    baseline: Sequence[Mapping[str, Any]],
    candidate: Sequence[Mapping[str, Any]],
    *,
    last_close: float,
) -> dict[str, Any]:
    reasons: list[str] = []

    for label, runs in (("baseline", baseline), ("candidate", candidate)):
        for index, run in enumerate(runs):
            if not run.get("ok"):
                reasons.append(f"{label} run {index} did not return an artifact")

    if reasons:
        return {"comparable": False, "identical": False, "reasons": reasons}

    if not _finite(baseline[0]) or not _finite(candidate[0]):
        reasons.append("a run produced a non-finite value")

    baseline_shape = _shape(baseline[0])
    candidate_shape = _shape(candidate[0])
    if baseline_shape != candidate_shape:
        reasons.append(
            f"output shape differs: baseline {baseline_shape} vs candidate {candidate_shape}"
        )

    repeatability: dict[str, Any] = {}
    for label, runs in (("baseline", baseline), ("candidate", candidate)):
        if len(runs) < 2:
            repeatability[label] = {"checked": False}
            continue
        deviation = _deviation(runs[0], runs[1])
        repeatability[label] = {"checked": True, **deviation}
        if deviation["values_differing"]:
            reasons.append(f"{label} does not reproduce itself at a fixed seed")

    cross = _deviation(baseline[0], candidate[0])
    identical = cross["values_differing"] == 0 and not reasons

    baseline_return = _terminal_return(baseline[0], last_close)
    candidate_return = _terminal_return(candidate[0], last_close)
    direction_agrees = (
        None
        if baseline_return is None or candidate_return is None
        else (baseline_return > 0) == (candidate_return > 0)
    )

    return {
        "comparable": not reasons,
        "identical": identical,
        "reasons": reasons,
        "shape": {"baseline": list(baseline_shape), "candidate": list(candidate_shape)},
        "repeatability": repeatability,
        "cross_environment": cross,
        "mean_terminal_return": {
            "baseline": baseline_return,
            "candidate": candidate_return,
            "direction_agrees": direction_agrees,
        },
        "runtime_seconds": {
            "baseline": [run.get("wall_seconds") for run in baseline],
            "candidate": [run.get("wall_seconds") for run in candidate],
        },
        "worker_reported": {
            "baseline": _reported(baseline[0]),
            "candidate": _reported(candidate[0]),
        },
        "verdict": (
            "SAME_FORECASTER_ON_THIS_CORPUS"
            if identical
            else "DISTINCT_FORECASTER_NEW_EVIDENCE_SERIES"
        ),
    }


def _reported(run: Mapping[str, Any]) -> dict[str, Any]:
    response = run.get("response")
    if not isinstance(response, Mapping):
        return {}
    return {
        key: response.get(key)
        for key in (
            "runtime_environment_hash",
            "device",
            "model_initialization_seconds",
            "inference_seconds",
            "peak_process_memory_bytes",
            "peak_gpu_memory_bytes",
        )
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="qualify-kronos-environment")
    commands = parser.add_subparsers(dest="command", required=True)

    corpus = commands.add_parser("corpus")
    corpus.add_argument("--out", required=True, type=Path)
    corpus.add_argument("--seed", type=int, default=20260902)
    corpus.add_argument("--symbol", default="SYNTH")
    corpus.add_argument("--bars", type=int, default=256)
    corpus.add_argument("--horizon", type=int, default=12)

    run = commands.add_parser("run")
    run.add_argument("--corpus", required=True, type=Path)
    run.add_argument("--worker-entry", required=True, type=Path)
    run.add_argument("--worker-python", required=True, type=Path)
    run.add_argument("--kronos-repository", required=True, type=Path)
    run.add_argument("--cache-directory", required=True, type=Path)
    run.add_argument("--git-executable", required=True, type=Path)
    run.add_argument("--out", required=True, type=Path)
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--sample-count", type=int, default=4)
    run.add_argument("--max-context", type=int, default=256)
    run.add_argument("--timeout-seconds", type=int, default=1800)

    comparison = commands.add_parser("compare")
    comparison.add_argument("--corpus", required=True, type=Path)
    comparison.add_argument("--baseline", required=True, nargs="+", type=Path)
    comparison.add_argument("--candidate", required=True, nargs="+", type=Path)
    comparison.add_argument("--out", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "corpus":
        corpus = build_corpus(
            seed=args.seed, symbol=args.symbol, bars=args.bars, horizon=args.horizon
        )
        args.out.write_text(json.dumps(corpus, indent=2) + "\n", encoding="utf-8")
        print(f"corpus_hash={corpus['corpus_hash']} bars={len(corpus['bars'])}")
        return 0

    if args.command == "run":
        corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
        worker = load_worker(args.worker_entry.resolve())
        request = build_request(
            corpus,
            worker,
            seed=args.seed,
            sample_count=args.sample_count,
            max_context=args.max_context,
        )
        result = run_worker(
            request,
            worker_entry=args.worker_entry.resolve(),
            worker_python=args.worker_python.resolve(),
            kronos_repository=args.kronos_repository.resolve(),
            cache_directory=args.cache_directory.resolve(),
            git_executable=args.git_executable.resolve(),
            timeout_seconds=args.timeout_seconds,
        )
        result["worker_environment_id"] = worker.WORKER_ENVIRONMENT_ID
        result["worker_entry_sha256"] = hashlib.sha256(
            args.worker_entry.read_bytes()
        ).hexdigest()
        result["corpus_hash"] = corpus["corpus_hash"]
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(
            f"{worker.WORKER_ENVIRONMENT_ID} ok={result['ok']} "
            f"wall={result['wall_seconds']:.1f}s -> {args.out}"
        )
        return 0 if result["ok"] else 1

    corpus = json.loads(args.corpus.read_text(encoding="utf-8"))
    baseline = [json.loads(path.read_text(encoding="utf-8")) for path in args.baseline]
    candidate = [json.loads(path.read_text(encoding="utf-8")) for path in args.candidate]
    for label, runs in (("baseline", baseline), ("candidate", candidate)):
        hashes = {run.get("corpus_hash") for run in runs}
        if hashes != {corpus["corpus_hash"]}:
            raise SystemExit(f"{label} runs did not use the named corpus: {hashes}")
    report = compare(baseline, candidate, last_close=float(corpus["bars"][-1]["close"]))
    report["corpus_hash"] = corpus["corpus_hash"]
    report["environments"] = {
        "baseline": sorted({str(run.get("worker_environment_id")) for run in baseline}),
        "candidate": sorted({str(run.get("worker_environment_id")) for run in candidate}),
    }
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["comparable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
