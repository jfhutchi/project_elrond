"""Read-only operational sources, isolated tracking outputs and two frozen shadows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from quantbot.learner.five_minute_launch import FiveMinuteLaunchConfig, clock_healthy
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard, _read, _save
from quantbot.learner.models import LearnerModel
from quantbot.learner.tracking_feedback import ingest_market_labels, shadow_tick
from quantbot.learner.trade_learning import journal_trade_episodes, read_trade_episodes
from quantbot.research.manifest import content_id
from quantbot.storage import Database, SingleWriterLock


class TrackingConfig(LearnerModel):
    mode: str = Field(pattern="^PAPER$")
    root: Path
    source_config: Path
    source_config_hash: str = Field(pattern="^[a-f0-9]{64}$")
    source_code_commit: str = Field(pattern="^[a-f0-9]{40}$")
    strategy_id: str
    shadow_models: tuple[Path, Path]
    shadow_hashes: tuple[str, str]
    code_commit: str = Field(pattern="^[a-f0-9]{40}$")
    source_tree_hash: str = Field(pattern="^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def isolated(self) -> TrackingConfig:
        if len(set(self.shadow_hashes)) != 2:
            raise ValueError("exactly two distinct frozen shadow artifacts required")
        for path in (self.root, self.source_config, *self.shadow_models):
            if not path.is_absolute():
                raise ValueError("absolute paths required")
        return self


def source_tree_hash(package: Path) -> str:
    return content_id(
        {
            str(path.relative_to(package)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in sorted(package.rglob("*.py"))
        }
    )


def verify_source(config: TrackingConfig, package: Path | None = None) -> None:
    package = package or Path(__file__).resolve().parents[1]
    if (
        source_tree_hash(package) != config.source_tree_hash
        or (package.parent.parent / "REVISION").read_text().strip() != config.code_commit
    ):
        raise ValueError("executed observation-only release pin changed")


def run_once(config: TrackingConfig, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    verify_source(config)
    source_document = _read(config.source_config)
    source = FiveMinuteLaunchConfig.model_validate(source_document)
    if (
        content_id(source_document) != config.source_config_hash
        or source.code_commit != config.source_code_commit
    ):
        raise ValueError("existing producer binding changed")
    for path in (
        source.root,
        source.operations_database,
        source.declaration_database,
        source.clock_receipt,
        source.frozen,
        config.source_config,
        *config.shadow_models,
    ):
        if (
            config.root.resolve() == path.resolve()
            or config.root.resolve() in path.resolve().parents
            or path.resolve() in config.root.resolve().parents
        ):
            raise ValueError("tracking output overlaps an authoritative source")
    models = [_read(path) for path in config.shadow_models]
    if tuple(content_id(model) for model in models) != config.shadow_hashes:
        raise ValueError("frozen shadow model hash changed")
    # No credential environment or broker API is loaded by this process.
    config.root.mkdir(parents=True, exist_ok=True)
    with SingleWriterLock(config.root / "tracking.lock"):
        _save(config.root / "deployment.json", config.model_dump(mode="json"))
        database = (
            Database(config.root / "guard.db")
            if (config.root / "guard.db").exists()
            else Database.create(config.root / "guard.db")
        )
        rt = _REGISTRY.set(source.operations_database)
        dt = _DECLARATIONS.set(source.declaration_database)
        try:

            def guard(start: datetime, end: datetime) -> bool:
                return _guard(database, start, end)

            episodes = read_trade_episodes(
                source.operations_database, now=now, strategy_id=config.strategy_id, guard=guard
            )
            revisions = journal_trade_episodes(config.root / "execution", episodes)
            labels = ingest_market_labels(
                source=source.root,
                output=config.root / "training-labels",
                now=now,
                guard=guard,
                model_ids=(source.mean_model, source.ridge_model),
            )
            healthy = clock_healthy(source.clock_receipt, now)
            shadow = shadow_tick(
                source=source.root,
                output=config.root / "raw-shadow",
                models=models,
                now=now,
                guard=guard,
                model_ids=(source.mean_model, source.ridge_model),
                clock_healthy=healthy,
                clock_health=lambda sample: clock_healthy(source.clock_receipt, sample),
            )
            receipt = dict(
                started_at=now.isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
                status="TRACKING" if healthy else "CLOCK_BLOCKED_NEW_SHADOW_FORECASTS",
                scope="OBSERVATION_ONLY",
                mode="PAPER",
                orders=0,
                fits=0,
                promotions=0,
                active_model_changed=False,
                order_authority=False,
                code_commit=config.code_commit,
                config_hash=content_id(config.model_dump(mode="json")),
                source_code_commit=source.code_commit,
                execution_episodes=len(episodes),
                complete_executed_outcomes=sum(e.status == "CLOSED" for e in episodes),
                execution_revisions=revisions,
                new_market_training_labels=labels,
                raw_shadow=shadow,
                clock_healthy=healthy,
                costs="ACTUAL_FEES_IN_EXECUTION_JOURNAL;SHADOW_5BPS_PER_SIDE_ASSUMPTION",
            )
        except Exception as error:
            receipt = dict(
                started_at=now.isoformat(),
                finished_at=datetime.now(UTC).isoformat(),
                status="ERROR",
                error_type=type(error).__name__,
                orders=0,
                order_authority=False,
                code_commit=config.code_commit,
            )
            publish(config.root, receipt)
            raise
        finally:
            _REGISTRY.reset(rt)
            _DECLARATIONS.reset(dt)
            database.close()
        publish(config.root, receipt)
        return receipt


def publish(root: Path, receipt: dict[str, Any]) -> None:
    _save(root / "runs" / (content_id(receipt) + ".json"), receipt)
    temporary = root / (".receipt-" + uuid4().hex)
    try:
        temporary.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
        os.replace(temporary, root / "receipt.json")
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    config = TrackingConfig.model_validate_json(parser.parse_args().config.read_bytes())
    print(json.dumps(run_once(config), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
