"""Pinned observational launcher; no promotion, order, migration or clock-setting API."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, model_validator

from quantbot.config import Settings
from quantbot.learner.experiment_contract import ExperimentManifest, InstrumentIdentity
from quantbot.learner.experiment_coordinator import collect_observations, coordinator_tick
from quantbot.learner.experiment_news import read_position_journal
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute_launch import clock_healthy
from quantbot.learner.forward import _DECLARATIONS, _REGISTRY, _guard
from quantbot.learner.forward_source import Snapshot, collect_snapshot
from quantbot.learner.models import LearnerModel
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.operations.service import _git, isolated_application_environment
from quantbot.research.manifest import content_id
from quantbot.storage import Database, SingleWriterLock


class ResearchSources(LearnerModel):
    training_path: Path
    validation_path: Path
    training_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    validation_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ExperimentLaunchConfig(LearnerModel):
    root: Path
    store: Path
    manifest_path: Path
    operations_database: Path
    declaration_database: Path
    clock_receipt: Path
    code_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    environment_file: Path | None = None
    mode: Literal["SHADOW"] = "SHADOW"
    trading_enabled: Literal[False] = False
    maximum_workers: Literal[2] = 2
    research_sources: ResearchSources | None = None
    position_research_database: Path | None = None
    position_research_commit: str | None = Field(default=None, pattern=r"^[a-f0-9]{40}$")

    @model_validator(mode="after")
    def isolated(self) -> Self:
        if (self.position_research_database is None) != (self.position_research_commit is None):
            raise ValueError("RESEARCH_SOURCE_PATH_AND_COMMIT_REQUIRED_TOGETHER")
        paths = (
            (
                self.root,
                self.store,
                self.manifest_path,
                self.operations_database,
                self.declaration_database,
                self.clock_receipt,
            )
            + (
                ()
                if self.position_research_database is None
                else (self.position_research_database,)
            )
            + (
                ()
                if self.research_sources is None
                else (
                    self.research_sources.training_path,
                    self.research_sources.validation_path,
                )
            )
        )
        if any(not path.is_absolute() or path.is_symlink() for path in paths):
            raise ValueError("ABSOLUTE_NON_SYMLINK_PATH_REQUIRED")
        root = self.root.resolve()
        if root not in self.store.resolve().parents:
            raise ValueError("EXPERIMENT_STORE_MUST_BE_ISOLATED_OUTPUT")
        for source in paths[2:] + (
            () if self.environment_file is None else (self.environment_file,)
        ):
            source = source.resolve()
            if source == root or root in source.parents or source in root.parents:
                raise ValueError("OUTPUT_SOURCE_OVERLAP")
        if self.environment_file is not None and (
            not self.environment_file.is_absolute() or self.environment_file.is_symlink()
        ):
            raise ValueError("EXISTING_ABSOLUTE_ENVIRONMENT_REQUIRED")
        return self


def load_manifest(config: ExperimentLaunchConfig) -> ExperimentManifest:
    manifest = ExperimentManifest.model_validate(_read_json(config.manifest_path))
    if content_id(manifest) != config.manifest_hash:
        raise ValueError("MANIFEST_PIN_MISMATCH")
    if manifest.synthetic:
        raise ValueError("SYNTHETIC_NOT_DEPLOYABLE")
    return manifest


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or path.stat().st_size > 100_000:
        raise ValueError("BOUNDED_CONFIGURATION_REQUIRED")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("CONFIGURATION_OBJECT_REQUIRED")
    return value


def verify_checkout(config: ExperimentLaunchConfig) -> None:
    repository = Path(__file__).resolve().parents[3]
    if _git(repository, "rev-parse", "HEAD") != config.code_commit or _git(
        repository, "status", "--porcelain"
    ):
        raise ValueError("CLEAN_PINNED_CHECKOUT_REQUIRED")
    for source in (config.operations_database, config.declaration_database, config.clock_receipt):
        if not source.is_file():
            raise ValueError("AUTHORITATIVE_SOURCE_MISSING")


async def _collect_guarded_day(
    guard: Database,
    root: Path,
    symbols: tuple[str, ...],
    day: date,
    settings: Settings,
    *,
    clock: Callable[[], datetime],
) -> Snapshot:
    actual = clock()
    # Match collect_snapshot's UTC price-query bounds, rather than a rolling
    # window which can belong to a different date than the exchange session.
    start = datetime.combine(day, time.min, UTC)
    end = max(
        start,
        min(
            datetime.combine(day, time.max, UTC),
            actual.replace(second=0, microsecond=0) - timedelta(minutes=1),
        ),
    )
    if not _guard(guard, start, end):
        raise ValueError("PROTECTED_OBSERVATION_WINDOW")
    return await collect_snapshot(root, symbols, day, day, settings, clock=clock, timeframe="5Min")


def _pending_label_days(
    store: ExperimentStore, manifest: ExperimentManifest, *, now: datetime
) -> tuple[date, ...]:
    from quantbot.learner.experiment_coordinator import ResolvedMarketLabel, feature_key
    from quantbot.learner.experiment_observation import ObservationBatch

    events = store.read_as_of(manifest.experiment_id, now)
    labeled = {
        feature_key(event.decode(ResolvedMarketLabel).label.features)
        for event in events
        if event.kind == "ResolvedMarketLabel"
    }
    today = now.astimezone(XNYS_TIMEZONE).date()
    days = {
        row.target_at.astimezone(XNYS_TIMEZONE).date()
        for event in events
        if event.kind == "ObservationBatch"
        for row in event.decode(ObservationBatch).features
        if row.target_at <= now
        and feature_key(row) not in labeled
        and row.target_at.astimezone(XNYS_TIMEZONE).date() < today
    }
    return tuple(sorted(days)[:2])


async def _run_once(config: ExperimentLaunchConfig, settings: Settings) -> dict[str, object]:
    manifest = load_manifest(config)
    verify_checkout(config)

    def clock() -> datetime:
        return datetime.now(UTC)

    def healthy(now: datetime) -> bool:
        return clock_healthy(config.clock_receipt, now)

    config.root.mkdir(parents=True, exist_ok=True)
    with SingleWriterLock(config.root / "coordinator.lock"):
        store = ExperimentStore(config.store, create=not config.store.exists())
        store.append(manifest, idempotency_key="manifest")
        now = clock()
        observation = None
        captures: list[Snapshot] = []
        if healthy(now):
            # The existing canonical guard reads both authoritative ledgers. Its local
            # bookkeeping database is separate from operational/runtime state.
            guard_path = config.root / "guard.db"
            guard = Database(guard_path) if guard_path.exists() else Database.create(guard_path)
            registry_token = _REGISTRY.set(config.operations_database)
            declaration_token = _DECLARATIONS.set(config.declaration_database)
            try:

                async def provider(identity: InstrumentIdentity) -> Snapshot:
                    actual = clock()
                    day = actual.astimezone(XNYS_TIMEZONE).date()
                    captured = await _collect_guarded_day(
                        guard,
                        config.root / "market" / content_id({"asset_id": identity.asset_id}),
                        (identity.symbol,),
                        day,
                        settings,
                        clock=clock,
                    )
                    captures.append(captured)
                    return captured

                observation = await asyncio.wait_for(
                    collect_observations(
                        manifest, provider=provider, clock=clock, clock_health=healthy
                    ),
                    timeout=90,
                )
                # Recovery reads only the two oldest unresolved target sessions;
                # actual retrieval time remains the label's availability time.
                for day in _pending_label_days(store, manifest, now=clock()):
                    if not healthy(clock()):
                        break
                    captures.append(
                        await asyncio.wait_for(
                            _collect_guarded_day(
                                guard,
                                config.root / "market" / "pending" / day.isoformat(),
                                tuple(identity.symbol for identity in manifest.roster),
                                day,
                                settings,
                                clock=clock,
                            ),
                            timeout=30,
                        )
                    )
            finally:
                _REGISTRY.reset(registry_token)
                _DECLARATIONS.reset(declaration_token)
                guard.close()
        # Recheck after every blocking acquisition before evidence or decisions.
        now = clock()
        if healthy(now):
            from quantbot.learner.experiment_evaluation import calendar_evidence

            for capture in captures:
                evidence = calendar_evidence(manifest, capture)
                store.append(evidence, idempotency_key="calendar:" + content_id(evidence))
        receipt = await coordinator_tick(
            store,
            manifest,
            now=now,
            observation=observation,
            execution=None,
            clock_verified=healthy(now),
            owner=f"coordinator:{os.getpid()}",
            label_snapshots=tuple(captures),
            operational_path=config.operations_database,
            news_evidence=read_position_journal(
                config.position_research_database,
                manifest,
                now=now,
                expected_commit=config.position_research_commit,
            ),
        )
        document = receipt.model_dump(mode="json")
        document.update(
            code_commit=config.code_commit,
            config_hash=content_id(config.model_dump(mode="json")),
            scope="SHADOW",
        )
        _write_receipt(config, document)
        return document


def _write_receipt(config: ExperimentLaunchConfig, document: dict[str, object]) -> None:
    config.root.mkdir(parents=True, exist_ok=True)
    temporary = config.root / "receipt.json.tmp"
    temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    temporary.replace(config.root / "receipt.json")


async def run_once(config: ExperimentLaunchConfig, settings: Settings) -> dict[str, object]:
    # Invalid deployment pins are fatal preflight failures, never successful ticks.
    load_manifest(config)
    verify_checkout(config)
    try:
        return await _run_once(config, settings)
    except Exception as error:
        # Preserve cancellation (BaseException) and never serialize raw provider errors.
        reason = "COORDINATOR_TICK_FAILED"
        if isinstance(error, TimeoutError):
            reason = "COORDINATOR_TICK_TIMEOUT"
        elif isinstance(error, ValueError) and str(error) == "CLOCK_SYNC_REQUIRED":
            reason = "CLOCK_SYNC_REQUIRED"
        document: dict[str, object] = {
            "recorded_at": datetime.now(UTC).isoformat(),
            "status": "CLOCK_SYNC_REQUIRED" if reason == "CLOCK_SYNC_REQUIRED" else "FAULTED",
            "reason": reason,
            "next_expected_event": "VERIFIED_CLOCK"
            if reason == "CLOCK_SYNC_REQUIRED"
            else "OPERATOR_REVIEW",
            "clock_verified": False,
            "observations_recorded": 0,
            "labels_recorded": 0,
            "fits": 0,
            "intents": 0,
            "code_commit": config.code_commit,
            "config_hash": content_id(config.model_dump(mode="json")),
            "scope": "SHADOW",
        }
        _write_receipt(config, document)
        return document


def _run_research_once(config: ExperimentLaunchConfig) -> dict[str, object]:
    """Independent bounded worker; no execution, promotion, broker or migrations."""
    from quantbot.learner.experiment_research import (
        CanonicalTrialCharger,
        load_initial_inputs,
        research_eligibility,
        run_registered_fit,
        schedule_initial_fits,
    )

    manifest = load_manifest(config)
    verify_checkout(config)
    now = datetime.now(UTC)
    if not clock_healthy(config.clock_receipt, now):
        return {"status": "CLOCK_SYNC_REQUIRED", "reason": "CLOCK_SYNC_REQUIRED"}
    if config.research_sources is None:
        return {"status": "NORMAL_WAIT", "reason": "PREDECLARED_RESEARCH_SOURCES_REQUIRED"}
    if not config.store.exists():
        return {"status": "NORMAL_WAIT", "reason": "COORDINATOR_REGISTRATION_REQUIRED"}
    # Separate lock prevents multiple launchers from scheduling or selecting the worker.
    with SingleWriterLock(config.root / "research-worker.lock"):
        store = ExperimentStore(config.store)
        from quantbot.learner.experiment_evaluation import evaluate_experiment

        evaluation = evaluate_experiment(store, manifest, now=now)
        if not evaluation.new_research_allowed:
            return {"status": "NORMAL_WAIT", "reason": evaluation.status}
        guard_path = config.root / "guard.db"
        guard_database = (
            Database(guard_path) if guard_path.exists() else Database.create(guard_path)
        )
        registry_token = _REGISTRY.set(config.operations_database)
        declaration_token = _DECLARATIONS.set(config.declaration_database)
        try:

            class Guard:
                def permits(self, start: datetime, end: datetime) -> bool:
                    return _guard(guard_database, start, end)

            sources = config.research_sources
            training, validation = load_initial_inputs(
                manifest,
                training_path=sources.training_path,
                validation_path=sources.validation_path,
                training_hash=sources.training_hash,
                validation_hash=sources.validation_hash,
                guard=Guard(),
            )
            now = datetime.now(UTC)
            if not clock_healthy(config.clock_receipt, now):
                return {"status": "CLOCK_SYNC_REQUIRED", "reason": "CLOCK_SYNC_REQUIRED"}
            schedule_initial_fits(
                store,
                manifest,
                training=training,
                validation=validation,
                now=now,
                guard=Guard(),
            )
            request = research_eligibility(store, manifest, now=now)
            if request is None:
                from quantbot.learner.experiment_adaptation import schedule_next_successor

                adaptation = schedule_next_successor(store, manifest, now=now, guard=Guard())
                store.append(adaptation, idempotency_key="adaptation:" + content_id(adaptation))
                request = research_eligibility(store, manifest, now=now)
                if request is None:
                    return {"status": "NORMAL_WAIT", "reason": adaptation.reason}

            def clock() -> datetime:
                return datetime.now(UTC)

            receipt = run_registered_fit(
                store,
                manifest,
                request,
                guard=Guard(),
                charger=CanonicalTrialCharger(request, clock=clock),
                now=now,
                clock=clock,
            )
            document = receipt.model_dump(mode="json")
            document.update(code_commit=config.code_commit, scope="SHADOW")
            temporary = config.root / "research-receipt.json.tmp"
            temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
            temporary.replace(config.root / "research-receipt.json")
            return document
        finally:
            _REGISTRY.reset(registry_token)
            _DECLARATIONS.reset(declaration_token)
            guard_database.close()


def run_research_once(config: ExperimentLaunchConfig) -> dict[str, object]:
    """Persist every bounded worker outcome without credential-bearing exceptions."""
    manifest = load_manifest(config)
    verify_checkout(config)

    def persist(status: object, reason: object) -> None:
        document = {
            "experiment_id": manifest.experiment_id,
            "recorded_at": datetime.now(UTC).isoformat(),
            "status": status,
            "reason": reason,
            "code_commit": config.code_commit,
            "scope": "SHADOW",
            "synthetic": manifest.synthetic,
        }
        config.root.mkdir(parents=True, exist_ok=True)
        temporary = config.root / "worker-receipt.json.tmp"
        temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        temporary.replace(config.root / "worker-receipt.json")

    persist("STARTED", "BOUNDED_RESEARCH_ATTEMPT_STARTED")
    try:
        result = _run_research_once(config)
    except Exception:
        result = {"status": "FAULTED", "reason": "RESEARCH_WORKER_FAILED"}
    persist(result.get("status"), result.get("reason", "BOUNDED_RESEARCH_ATTEMPT_COMPLETED"))
    return result


def run_experiment(
    config_path: Path, *, once: bool = False, check: bool = False, config_hash: str | None = None
) -> int:
    if config_hash is not None:
        _read_json(config_path)  # Bound size and reject symlinks before reading bytes.
        if hashlib.sha256(config_path.read_bytes()).hexdigest() != config_hash:
            raise ValueError("PINNED_LAUNCH_CONFIGURATION_CHANGED")
    config = ExperimentLaunchConfig.model_validate(_read_json(config_path))
    load_manifest(config)
    verify_checkout(config)
    if check:
        print(
            json.dumps(
                {
                    "mode": "SHADOW",
                    "trading_enabled": False,
                    "code_commit": config.code_commit,
                    "manifest_hash": config.manifest_hash,
                }
            )
        )
        return 0
    context = (
        nullcontext()
        if config.environment_file is None
        else isolated_application_environment(config.environment_file)
    )
    with context:
        settings = Settings()
        if settings.TRADING_MODE.value != "PAPER" or settings.BROKER_ENVIRONMENT.value != "PAPER":
            raise ValueError("PAPER_ONLY")

        async def serve() -> int:
            worker: asyncio.Task[dict[str, object]] | None = None
            while True:
                if not once and (worker is None or worker.done()):
                    if worker is not None:
                        try:
                            worker.result()
                        except Exception:
                            # Bounded sanitized diagnostic; no raw registry/provider error.
                            print(
                                json.dumps(
                                    {"status": "FAULTED", "reason": "RESEARCH_WORKER_FAILED"}
                                )
                            )
                    worker = asyncio.create_task(asyncio.to_thread(run_research_once, config))
                receipt = await run_once(config, settings)
                print(
                    json.dumps(
                        {
                            key: receipt[key]
                            for key in (
                                "status",
                                "reason",
                                "next_expected_event",
                                "observations_recorded",
                                "labels_recorded",
                                "fits",
                                "intents",
                            )
                        }
                    )
                )
                if once:
                    return 1 if receipt["status"] == "FAULTED" else 0
                await asyncio.sleep(120)

        return asyncio.run(serve())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--config-hash")
    args = parser.parse_args()
    return run_experiment(
        args.config, once=args.once, check=args.check, config_hash=args.config_hash
    )


if __name__ == "__main__":
    try:
        result = main()
    except Exception:
        print(json.dumps({"status": "FAULTED", "reason": "EXPERIMENT_LAUNCH_FAILED"}))
        result = 1
    raise SystemExit(result)
