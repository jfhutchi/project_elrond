"""One fixed historical learner run against an existing dedicated research snapshot.

No downloads, credentials, broker runtime, migrations, parameter sweep or promotion. A failed
run leaves its directory and fit attempts intact; retry individual CLI steps, never erase it.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

from quantbot.cli import main as quantbot_main
from quantbot.learner.economics import write_report
from quantbot.learner.store import StoreReplaySpec
from quantbot.research.manifest import content_id


def run_replay(database: Path, spec_path: Path, output: Path) -> dict[str, Any]:
    spec = StoreReplaySpec.model_validate_json(spec_path.read_text(encoding="utf-8-sig"))
    output.mkdir(parents=True, exist_ok=False)
    calendar = output / "calendar.json"
    calendar.write_text(spec.sessions.model_dump_json(), encoding="utf-8")
    (output / "spec.json").write_text(spec.model_dump_json(), encoding="utf-8")

    def command(*args: str) -> dict[str, Any]:
        stream = io.StringIO()
        code = quantbot_main(["learner", "--database", str(database), *args], output=stream)
        result: dict[str, Any] = json.loads(stream.getvalue())
        with (output / "steps.jsonl").open("a", encoding="utf-8") as ledger:
            ledger.write(json.dumps({"args": args, "exit_code": code, "result": result}) + "\n")
        if code:
            raise RuntimeError(f"learner step failed: {args[0]}; see steps.jsonl")
        return result

    dataset = command("dataset-from-store", "--spec", str(spec_path))
    models: dict[str, Any] = {}
    for name, family in (("mean", "mean"), ("ridge", "ridge"), ("ridge_successor", "ridge")):
        parent = (["--parent-model-id", models["ridge"]["model_id"]]
                  if name == "ridge_successor" else [])
        model = command("train", dataset["dataset_id"], "--family", family, *parent)["model"]
        models[name] = {k: model[k] for k in (
            "model_id", "artifact_hash", "version", "parent_model_id", "validation_mse")}
        command("predict", model["model_id"], "--replay")
        command("resolve-from-store", model["model_id"], "--calendar", str(calendar))
        metrics = command("evaluate", model["model_id"], "--evaluation-class", "HISTORICAL_REPLAY")
        command("economics", model["model_id"], "--calendar", str(calendar),
                "--output", str(output / (name + "-economics.json")))
        report = json.loads((output / (name + "-economics.json")).read_text(encoding="utf-8"))
        models[name]["rank_ic"] = metrics["metrics"]["mean_ic"]
        models[name]["period_count"] = report["report"]["period_count"]
        models[name]["portfolios"] = {
            key: {k: v for k, v in value.items() if k != "daily_equity"}
            for key, value in report["report"]["portfolios"].items()}
    comparison = command(
        "compare", models["ridge"]["model_id"], models["ridge_successor"]["model_id"])
    result = {
        "dataset_id": dataset["dataset_id"], "spec_hash": content_id(spec), "models": models,
        "comparison": comparison, "evidence_class": "EXPLORATORY_HISTORICAL_STORE_REPLAY",
        "positive_expectancy_justified": False, "promotion": "NONE",
        "trial_accounting": "Three fits recorded in this research store; retain it across retries.",
    }
    write_report(output / "summary.json", result)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    result = run_replay(args.database, args.spec, args.output_dir)
    print(json.dumps({"output": str(args.output_dir), "dataset_id": result["dataset_id"],
                      "promotion": "NONE"}))


if __name__ == "__main__":
    main()
