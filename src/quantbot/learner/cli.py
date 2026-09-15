"""The learner's broker-free command surface over an explicitly selected research store."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path
from typing import TextIO

from pydantic import BaseModel, ConfigDict, Field

from quantbot.learner import service
from quantbot.learner.data import DailyInput, build_dataset, build_features
from quantbot.learner.models import EvaluationClass
from quantbot.research.registry import DataRole, DataWindow
from quantbot.storage import Database, RecordNotFoundError, StateConflictError
from quantbot.strategy import XNYSSessionSequence


class InputBundle(BaseModel):
    """Explicit local source contract. Internally checked, not provider-authenticated evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    inputs: tuple[DailyInput, ...]
    sessions: XNYSSessionSequence
    symbols: tuple[str, ...] = Field(min_length=1)
    universe_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    observation_dataset: str = Field(min_length=1)
    snapshot: str = Field(min_length=1)
    benchmark_symbol: str = "SPY"


def configure_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--database", required=True, help="existing research store")
    commands = parser.add_subparsers(dest="learner_command", required=True)
    commands.add_parser("status")
    dataset = commands.add_parser("dataset")
    dataset.add_argument("--input", required=True, type=Path)
    for split in ("train", "validation", "holdout"):
        for bound in ("start", "end"):
            dataset.add_argument(f"--{split}-{bound}", required=True, type=date.fromisoformat)
    training = commands.add_parser("train")
    training.add_argument("dataset_id")
    training.add_argument("--family", choices=("mean", "ridge", "quadratic_ridge"), default="ridge")
    training.add_argument("--alpha", type=float, default=1.0)
    training.add_argument("--seed", type=int, default=0)
    training.add_argument("--parent-model-id")
    prediction = commands.add_parser("predict")
    prediction.add_argument("model_id")
    mode = prediction.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", action="store_true")
    mode.add_argument("--input", type=Path)
    prediction.add_argument("--as-of", type=datetime.fromisoformat)
    resolution = commands.add_parser("resolve")
    resolution.add_argument("model_id")
    resolution.add_argument("--input", required=True, type=Path)
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("model_id")
    evaluation.add_argument("--evaluation-class", choices=tuple(EvaluationClass), required=True)
    comparison = commands.add_parser("compare")
    comparison.add_argument("incumbent_id")
    comparison.add_argument("challenger_id")


def run(args: argparse.Namespace, output: TextIO) -> int:
    database: Database | None = None
    try:
        database = Database(args.database)
        result = _run(args, database)
        output.write(json.dumps(result, sort_keys=True, allow_nan=False) + "\n")
        return 0
    except (ValueError, OSError, RuntimeError, RecordNotFoundError, StateConflictError) as error:
        output.write(json.dumps({"ok": False, "error": str(error)}, sort_keys=True) + "\n")
        return 2
    finally:
        if database is not None:
            database.close()


def _bundle(path: Path) -> InputBundle:
    return InputBundle.model_validate_json(path.read_text(encoding="utf-8-sig"))


def _run(args: argparse.Namespace, database: Database) -> dict[str, object]:
    action = args.learner_command
    if action == "status":
        return service.status(database)
    if action == "dataset":
        bundle = _bundle(args.input)
        windows = [
            DataWindow(
                dataset=bundle.observation_dataset,
                snapshot=bundle.snapshot,
                role=role,
                start=getattr(args, f"{name}_start"),
                end=getattr(args, f"{name}_end"),
            )
            for name, role in (
                ("train", DataRole.DISCOVERY),
                ("validation", DataRole.VALIDATION),
                ("holdout", DataRole.PROTECTED_EVALUATION),
            )
        ]
        with database.transaction() as session:
            service.guard_range(
                session,
                bundle.observation_dataset,
                min(r.bar.timestamp.date() for r in bundle.inputs),
                windows[1].end,
            )
        dataset = build_dataset(
            bundle.inputs,
            bundle.sessions,
            symbols=bundle.symbols,
            universe_id=bundle.universe_id,
            source_id=bundle.source_id,
            train_window=windows[0],
            validation_window=windows[1],
            holdout_window=windows[2],
            benchmark_symbol=bundle.benchmark_symbol,
        )
        return {
            "ok": True,
            "dataset_id": service.register_dataset(database, dataset),
            "rows": len(dataset.rows),
            "source_evidence": "LOCAL_DECLARED_REPLAY",
        }
    if action == "train":
        model = service.train(
            database,
            args.dataset_id,
            family=args.family,
            alpha=args.alpha,
            seed=args.seed,
            parent_model_id=args.parent_model_id,
        )
        return {"ok": True, "model": model.model_dump(mode="json")}
    if action == "predict":
        if args.replay:
            predictions = service.replay(database, args.model_id)
        else:
            if args.as_of is None:
                raise ValueError("--as-of is required with --input")
            bundle = _bundle(args.input)
            features = build_features(
                bundle.inputs,
                bundle.sessions,
                as_of=args.as_of,
                symbols=bundle.symbols,
                universe_id=bundle.universe_id,
                source_id=bundle.source_id,
                benchmark_symbol=bundle.benchmark_symbol,
            )
            predictions = service.issue_predictions(database, args.model_id, features)
        return {"ok": True, "predictions": [p.model_dump(mode="json") for p in predictions]}
    if action == "resolve":
        bundle = _bundle(args.input)
        outcomes = service.resolve(
            database, args.model_id, bundle.inputs, source_id=bundle.source_id
        )
        return {"ok": True, "resolved": len(outcomes)}
    if action == "evaluate":
        evaluation = service.evaluate(
            database, args.model_id, EvaluationClass(args.evaluation_class)
        )
        return {
            "ok": True,
            "evaluation_id": evaluation.evaluation_id,
            "metrics": json.loads(evaluation.metrics_json),
        }
    if action == "compare":
        return service.compare(database, args.incumbent_id, args.challenger_id)
    raise ValueError(f"unsupported learner command: {action}")
