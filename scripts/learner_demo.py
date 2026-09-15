"""Run a broker-free synthetic demonstration of the durable learner workflow.

This script exercises software behavior only.  Its generated prices and weekday schedule are
synthetic and establish no market evidence, profitability, or promotion eligibility.
"""

from __future__ import annotations

import argparse
import io
import json
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import cast

from quantbot.cli import main as quantbot_main
from quantbot.domain import Bar
from quantbot.learner.cli import InputBundle
from quantbot.learner.data import DailyInput
from quantbot.learner.models import EvaluationClass
from quantbot.storage import Database
from quantbot.strategy import XNYSSession, XNYSSessionSequence

EVIDENCE_CLASS = "LOCAL_SYNTHETIC_SOFTWARE_DEMONSTRATION"
CALENDAR_LABEL = "SYNTHETIC_CALENDAR"
SESSION_COUNT = 165
LAST_INPUT_SESSION_INDEX = 139
HOLDOUT_START_INDEX = 140
HOLDOUT_END_INDEX = 159
SYMBOLS = ("ALFA", "BRAV", "CHAR", "DELT", "ECHO")
BENCHMARK = "SPY"


def _weekday_dates(count: int) -> tuple[date, ...]:
    days: list[date] = []
    cursor = date(2020, 1, 2)
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return tuple(days)


def _sessions() -> XNYSSessionSequence:
    sessions = tuple(
        XNYSSession(
            session_date=day,
            open_at=datetime.combine(day, time(14, 30), tzinfo=UTC),
            close_at=datetime.combine(day, time(21), tzinfo=UTC),
        )
        for day in _weekday_dates(SESSION_COUNT)
    )
    # XNYSSessionSequence is the learner's session-index carrier.  These dates are deliberately
    # weekday-only synthetic dates, not a claim that an exchange calendar provider supplied them.
    return XNYSSessionSequence(calendar="XNYS", sessions=sessions)


def _close(symbol: str, index: int) -> float:
    market_log = math.log(100.0) + 0.0007 * index + 0.012 * math.sin(index / 9.0)
    if symbol == BENCHMARK:
        return math.exp(market_log)
    parameters = {
        "ALFA": (80.0, 0.032, 6.0, 0.0, 0.00010),
        "BRAV": (95.0, 0.026, 7.5, 0.7, -0.00004),
        "CHAR": (110.0, 0.038, 9.0, 1.4, 0.00006),
        "DELT": (125.0, 0.022, 11.0, 2.1, -0.00008),
        "ECHO": (140.0, 0.044, 13.0, 2.8, 0.00002),
    }
    base, amplitude, period, phase, relative_trend = parameters[symbol]
    relative_log = amplitude * math.sin(index / period + phase) + relative_trend * index
    return base * math.exp(market_log - math.log(100.0) + relative_log)


def _inputs(sessions: XNYSSessionSequence) -> tuple[DailyInput, ...]:
    inputs: list[DailyInput] = []
    for index, session in enumerate(sessions.sessions[: LAST_INPUT_SESSION_INDEX + 1]):
        for symbol in (*SYMBOLS, BENCHMARK):
            close = _close(symbol, index)
            open_price = close * (1.0 - 0.001 * math.sin(index + len(symbol)))
            high = max(open_price, close) * 1.002
            low = min(open_price, close) * 0.998
            inputs.append(
                DailyInput(
                    bar=Bar(
                        symbol=symbol,
                        timestamp=session.close_at,
                        open=f"{open_price:.8f}",
                        high=f"{high:.8f}",
                        low=f"{low:.8f}",
                        close=f"{close:.8f}",
                        volume=str(1_000_000 + index * 1000 + len(symbol) * 10_000),
                        adjustment="1",
                    ),
                    available_at=session.close_at,
                )
            )
    return tuple(inputs)


def _run_cli(arguments: Sequence[str]) -> dict[str, object]:
    output = io.StringIO()
    exit_code = quantbot_main(list(arguments), output=output)
    lines = [line for line in output.getvalue().splitlines() if line.strip()]
    if len(lines) != 1:
        raise RuntimeError("learner command did not return one JSON result")
    payload = json.loads(lines[0])
    if not isinstance(payload, dict):
        raise RuntimeError("learner command returned a non-object result")
    result = cast(dict[str, object], payload)
    if exit_code != 0 or result.get("ok") is not True:
        raise RuntimeError(f"learner command failed: {result.get('error', result)}")
    return result


def _evaluation_summary(result: dict[str, object]) -> dict[str, object]:
    metrics = cast(dict[str, object], result["metrics"])
    keys = (
        "evidence_status",
        "positive_expectancy_justified",
        "sample_count",
        "expected_count",
        "coverage",
        "period_count",
        "valid_ic_periods",
        "mean_ic",
        "directional_hit_rate",
        "limitations",
    )
    return {
        "evaluation_id": result["evaluation_id"],
        **{key: metrics.get(key) for key in keys},
    }


def _model_summary(
    trained: dict[str, object],
    replayed: dict[str, object],
    resolved: dict[str, object],
    evaluated: dict[str, object],
) -> dict[str, object]:
    model = cast(dict[str, object], trained["model"])
    artifact = cast(dict[str, object], model["artifact"])
    predictions = cast(list[object], replayed["predictions"])
    return {
        "model_id": model["model_id"],
        "family": model["family"],
        "version": model["version"],
        "parent_model_id": model["parent_model_id"],
        "algorithm_version": artifact["algorithm_version"],
        "training_mse": artifact["training_mse"],
        "validation_mse": model["validation_mse"],
        "historical_replay_predictions": len(predictions),
        "resolved_predictions": resolved["resolved"],
        "evaluation": _evaluation_summary(evaluated),
    }


def _training_summary(trained: dict[str, object]) -> dict[str, object]:
    model = cast(dict[str, object], trained["model"])
    artifact = cast(dict[str, object], model["artifact"])
    return {
        "model_id": model["model_id"],
        "family": model["family"],
        "version": model["version"],
        "parent_model_id": model["parent_model_id"],
        "algorithm_version": artifact["algorithm_version"],
        "training_mse": artifact["training_mse"],
        "validation_mse": model["validation_mse"],
    }


def run_demo(output_dir: Path) -> dict[str, object]:
    """Create and run one fresh synthetic learner store, refusing every existing output path."""
    destination = output_dir.resolve()
    try:
        destination.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise FileExistsError(f"output directory already exists: {destination}") from exc

    database_path = destination / "learner.db"
    database = Database.create(database_path)
    database.close()

    sessions = _sessions()
    bundle = InputBundle(
        inputs=_inputs(sessions),
        sessions=sessions,
        symbols=SYMBOLS,
        universe_id="local-synthetic-five-v1",
        source_id="local-synthetic-v1",
        observation_dataset=CALENDAR_LABEL,
        snapshot="deterministic-formulas-v1",
        benchmark_symbol=BENCHMARK,
    )
    bundle_path = destination / "input_bundle.json"
    bundle_path.write_text(bundle.model_dump_json(indent=2) + "\n", encoding="utf-8")

    database_argument = str(database_path)
    dataset_result = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "dataset",
            "--input",
            str(bundle_path),
            "--train-start",
            sessions.sessions[60].session_date.isoformat(),
            "--train-end",
            sessions.sessions[99].session_date.isoformat(),
            "--validation-start",
            sessions.sessions[100].session_date.isoformat(),
            "--validation-end",
            sessions.sessions[139].session_date.isoformat(),
            "--holdout-start",
            sessions.sessions[HOLDOUT_START_INDEX].session_date.isoformat(),
            "--holdout-end",
            sessions.sessions[HOLDOUT_END_INDEX].session_date.isoformat(),
        )
    )
    dataset_id = cast(str, dataset_result["dataset_id"])

    model_runs: dict[str, dict[str, object]] = {}
    model_ids: dict[str, str] = {}
    trained_models: dict[str, dict[str, object]] = {}
    for family in ("mean", "ridge", "quadratic_ridge"):
        trained = _run_cli(
            (
                "learner",
                "--database",
                database_argument,
                "train",
                dataset_id,
                "--family",
                family,
                "--alpha",
                "0.1",
                "--seed",
                "0",
            )
        )
        model = cast(dict[str, object], trained["model"])
        model_id = cast(str, model["model_id"])
        model_ids[family] = model_id
        trained_models[family] = trained
        model_runs[family] = _training_summary(trained)

    ridge_replayed = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "predict",
            model_ids["ridge"],
            "--replay",
        )
    )
    ridge_resolved = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "resolve",
            model_ids["ridge"],
            "--input",
            str(bundle_path),
        )
    )
    ridge_evaluated = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "evaluate",
            model_ids["ridge"],
            "--evaluation-class",
            EvaluationClass.HISTORICAL_REPLAY.value,
        )
    )
    model_runs["ridge"] = _model_summary(
        trained_models["ridge"], ridge_replayed, ridge_resolved, ridge_evaluated
    )

    challenger_trained = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "train",
            dataset_id,
            "--family",
            "ridge",
            "--alpha",
            "0.1",
            "--seed",
            "0",
            "--parent-model-id",
            model_ids["ridge"],
        )
    )
    challenger_model = cast(dict[str, object], challenger_trained["model"])
    challenger_id = cast(str, challenger_model["model_id"])
    challenger_replayed = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "predict",
            challenger_id,
            "--replay",
        )
    )
    challenger_resolved = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "resolve",
            challenger_id,
            "--input",
            str(bundle_path),
        )
    )
    challenger_evaluated = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "evaluate",
            challenger_id,
            "--evaluation-class",
            EvaluationClass.HISTORICAL_REPLAY.value,
        )
    )
    model_runs["ridge_v2"] = _model_summary(
        challenger_trained,
        challenger_replayed,
        challenger_resolved,
        challenger_evaluated,
    )

    comparison_result = _run_cli(
        (
            "learner",
            "--database",
            database_argument,
            "compare",
            model_ids["ridge"],
            challenger_id,
        )
    )
    comparison = {
        key: comparison_result[key]
        for key in (
            "incumbent_id",
            "challenger_id",
            "matched_observations",
            "promotion",
            "evidence_class",
            "positive_expectancy_justified",
        )
    }
    status = _run_cli(("learner", "--database", database_argument, "status"))
    predictions_by_class = cast(dict[str, object], status["predictions"])
    mean_validation_mse = cast(float, model_runs["mean"]["validation_mse"])
    ridge_validation_mse = cast(float, model_runs["ridge"]["validation_mse"])
    ridge_beats_mean = ridge_validation_mse < mean_validation_mse
    report: dict[str, object] = {
        "ok": True,
        "evidence_class": EVIDENCE_CLASS,
        "description": (
            "Deterministic generated prices exercise local learner software only. They are not "
            "market observations and establish no profitability or forward evidence."
        ),
        "database": str(database_path),
        "dataset_id": dataset_id,
        "data": {
            "calendar_label": CALENDAR_LABEL,
            "calendar_contract": (
                "weekday-only synthetic schedule carried by the XNYS-compatible session type"
            ),
            "session_count": len(sessions.sessions),
            "last_input_session_index": LAST_INPUT_SESSION_INDEX,
            "holdout_session_indices": [HOLDOUT_START_INDEX, HOLDOUT_END_INDEX],
            "holdout_inputs_loaded": 0,
            "symbols": list(SYMBOLS),
            "benchmark": BENCHMARK,
        },
        "models": model_runs,
        "validation_baseline_check": {
            "baseline_family": "mean",
            "candidate_family": "ridge",
            "mean_validation_mse": mean_validation_mse,
            "ridge_validation_mse": ridge_validation_mse,
            "ridge_minus_mean_mse": ridge_validation_mse - mean_validation_mse,
            "ridge_beats_mean": ridge_beats_mean,
            "outcome": (
                "RIDGE_BEAT_MEAN_BASELINE"
                if ridge_beats_mean
                else "RIDGE_DID_NOT_BEAT_MEAN_BASELINE"
            ),
        },
        "comparison": comparison,
        "prediction_classes": predictions_by_class,
        "protected_holdout_evidence": status["protected_holdout_evidence"],
        "forward_paper_evidence": status["forward_paper_evidence"],
        "positive_expectancy_justified": False,
        "promotion": "NONE",
        "workflow_interface": "quantbot.cli learner subcommands",
    }
    if predictions_by_class.get(EvaluationClass.SHADOW.value) != 0:
        raise RuntimeError("synthetic demonstration unexpectedly created SHADOW predictions")
    report_path = destination / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = run_demo(args.output_dir)
    except (FileExistsError, OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, sort_keys=True))
        return 2
    print(
        json.dumps(
            {
                "ok": True,
                "evidence_class": report["evidence_class"],
                "report": str(args.output_dir.resolve() / "report.json"),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
