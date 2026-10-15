"""Read-only guarded archive replay. Bars do not certify NBBO, queues or live fills."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quantbot.domain import BrokerOrder, OrderIntent, OrderSide
from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.forward_source import Snapshot, read_snapshot
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.research.datasets import observation_identity
from quantbot.research.manifest import content_id
from quantbot.research.registry import HypothesisRegistry


@contextmanager
def readonly(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("regular existing canonical database required")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.rollback()
        connection.close()


def guard_day(
    operations: Path, declarations: Path, *, day: date, end_day: date | None = None
) -> dict[str, Any]:
    """Whole UTC query day covers every feature/dependency/holding interval in it."""
    at = datetime.now(UTC)
    end_day = end_day or day
    blockers: list[dict[str, Any]] = []
    with readonly(declarations) as connection:
        rows = connection.execute(
            "SELECT dataset_id,json_extract(document_json,'$.holdout_window') FROM learner_datasets"
        ).fetchall()
        for identity, raw in rows:
            window = json.loads(raw)
            if (
                observation_identity(window["dataset"]) == "us-equities-daily"
                and window["start"] <= end_day.isoformat()
                and window["end"] >= day.isoformat()
            ):
                blockers.append({"declared_holdout_id": identity})
    for authority, path in (("operations", operations), ("declarations", declarations)):
        with readonly(path) as connection:
            engine = create_engine("sqlite+pysqlite://", creator=lambda: connection)
            try:
                with Session(engine) as session:
                    for row in HypothesisRegistry(session).blocking_protected_windows(
                        "us-equities-daily",
                        day,
                        end_day,
                        now=at.date(),
                        include_forward_paper=True,
                    ):
                        blockers.append({"authority": authority, **dict(row)})
            finally:
                engine.dispose(close=False)
    if blockers:
        raise ValueError("PROTECTED_ARCHIVE_REFUSED:" + json.dumps(blockers, default=str))
    return {
        "checked_at": at.isoformat(),
        "guarded_start_day": day.isoformat(),
        "guarded_end_day": end_day.isoformat(),
        "canonical_authorities": (str(operations), str(declarations)),
        "include_forward_paper": True,
        "declared_holdouts_checked": True,
        "blockers": blockers,
        "connection_mode": "mode=ro;query_only;BEGIN;WAL-aware",
    }


def read_guarded_archive(
    *,
    path: Path,
    expected_hash: str,
    symbol: str,
    day: date,
    manifest: ExperimentManifest,
    operations: Path,
    declarations: Path,
    experiment_store: Path,
) -> tuple[Snapshot, dict[str, Any]]:
    # Metadata only before this guard; never call archived_capture's writable guard constructor.
    with readonly(experiment_store) as connection:
        registered = connection.execute(
            "SELECT document FROM events WHERE kind='ExperimentManifest' AND experiment_id=?",
            (manifest.experiment_id,),
        ).fetchall()
        if len(registered) != 1 or content_id(
            ExperimentManifest.model_validate_json(registered[0][0])
        ) != content_id(manifest):
            raise ValueError("ARCHIVE_MANIFEST_BINDING_INVALID")
        locators = connection.execute(
            "SELECT e.idempotency_key,e.digest,json_extract(e.document,'$.synthetic'),"
            "json_extract(f.value,'$.feature_at'),json_extract(f.value,'$.target_at'),"
            "json_extract(f.value,'$.receipt_at') FROM events e, "
            "json_each(e.document,'$.features') f WHERE e.kind='ObservationBatch' "
            "AND e.experiment_id=? AND json_extract(f.value,'$.symbol')=? "
            "AND json_extract(f.value,'$.source_snapshot_hash')=?",
            (manifest.experiment_id, symbol, expected_hash),
        ).fetchall()
    if not locators or any(
        synthetic
        or datetime.fromisoformat(feature).astimezone(UTC).date() != day
        or datetime.fromisoformat(target).astimezone(UTC).date() != day
        or datetime.fromisoformat(feature) - timedelta(minutes=35)
        < datetime.combine(day, time.min, UTC)
        for _, _, synthetic, feature, target, _ in locators
    ):
        raise ValueError("ARCHIVE_PRE_READ_WINDOW_BINDING_INVALID")
    # Authoritative immutable event metadata + the source's one-day capture
    # contract binds this guarded day to the exact expected raw content hash.
    guard = guard_day(operations, declarations, day=day)
    roster = {row.symbol: row.asset_id for row in manifest.roster}
    if symbol not in roster or path.name != expected_hash + ".json" or path.is_symlink():
        raise ValueError("ARCHIVE_LOCATOR_IDENTITY_INVALID")
    asset_digest = content_id({"asset_id": roster[symbol]})
    if path.parent.name != "snapshots" or path.parents[2].name != asset_digest:
        raise ValueError("ARCHIVE_ASSET_PATH_INVALID")
    snapshot = read_snapshot(path)
    start = datetime.combine(day, time.min, UTC)
    end = start + timedelta(days=1)
    if (
        snapshot.identity != expected_hash
        or snapshot.source_id != "alpaca:iex:raw:5min"
        or snapshot.query.timeframe != "5Min"
        or symbol not in snapshot.query.symbols
        or "SPY" not in snapshot.query.symbols
        or not start <= snapshot.query.start <= snapshot.query.end < end
        or any(not start <= item.bar.timestamp < end for item in snapshot.inputs)
    ):
        raise ValueError("ARCHIVE_CONTENT_OR_QUERY_INVALID")
    return snapshot, {
        **guard,
        "authoritative_locator_metadata": locators,
        "one_day_source_contract": "exploration_launch.archived_capture-v1",
        "raw_hash_checked_before_hydration": True,
        "snapshot_id": snapshot.identity,
        "path": str(path),
        "typed_manifest_id": content_id(manifest),
        "query": snapshot.query.model_dump(mode="json"),
        "capture_received_at": snapshot.received_at.isoformat(),
        "receipts": [r.model_dump(mode="json") for r in snapshot.receipts],
    }


class LocalSimulationBroker(SyntheticBroker):
    """Only in-memory execution, with explicit assumptions and no broker transport."""

    provider = "LOCAL_SIM_REAL_DATA"

    def __init__(
        self,
        *,
        account_id: str,
        now: datetime,
        scenario: Literal["base", "latency", "partial", "reject"] = "base",
    ) -> None:
        super().__init__(account_id=account_id, now=now)
        self.scenario = scenario

    async def submit_order(self, intent: OrderIntent) -> BrokerOrder:
        if self.scenario == "reject" and intent.side is OrderSide.BUY:
            order = BrokerOrder(
                broker_order_id="local-reject-" + intent.client_order_id,
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                quantity=intent.quantity,
                filled_quantity=0,
                status="rejected",
                submitted_at=self.now,
            )
            self.orders[intent.client_order_id] = order
            self.posts.append(intent)
            return order
        mark = self.marks[intent.symbol]
        bps = Decimal(2) + (Decimal(1) if self.scenario == "latency" else Decimal(0))
        sign = Decimal(1) if intent.side is OrderSide.BUY else Decimal(-1)
        self.marks[intent.symbol] = (mark[0] * (1 + sign * bps / 10000), mark[1])
        try:
            order = await super().submit_order(intent)
        finally:
            self.marks[intent.symbol] = mark
        if self.scenario == "partial" and intent.side is OrderSide.BUY:
            fill = self.fills.pop()
            quantity = intent.quantity / 2
            self.quantities[intent.symbol] -= quantity
            self.cash += quantity * fill.price
            self.fills.append(fill.model_copy(update={"quantity": quantity}))
            order = order.model_copy(update={"filled_quantity": quantity, "status": "canceled"})
            self.orders[intent.client_order_id] = order
        return order


class LocalClock:
    process_id = "m5-local-retrospective-replay"
    execution_domain = "LOCAL_SIM_REAL_DATA:retrospective-virtual-time-v1"

    def __init__(self, at: datetime) -> None:
        self.at = at

    def sample(self) -> tuple[datetime, float]:
        return self.at, 0.0


async def demonstrate_recorded_archives(
    path: Path, *, source_config: Path, index_path: Path
) -> dict[str, Any]:
    """Evidence runner on fixture stores; source config's secret reference is never opened."""
    from quantbot.learner.five_minute import FEATURE_NAMES, build_features
    from quantbot.paper_core.activation import initialize_fixture_model
    from quantbot.paper_core.contracts import (
        BrokerEvent,
        CoreConfig,
        CostItem,
        LearningCalendar,
        LearningPolicy,
        ModelArtifact,
        Observation,
    )
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.learning import consider_learning
    from quantbot.paper_core.outcomes import import_broker_events, process_outcomes
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.store import CoreStore
    from quantbot.paper_core.trader import trader_cycle
    from quantbot.storage import Database
    from quantbot.strategy import XNYSSessionSequence

    config = json.loads(source_config.read_text(encoding="utf-8-sig"))
    manifest = ExperimentManifest.model_validate_json(
        Path(config["manifest_path"]).read_text(encoding="utf-8-sig")
    )
    index = json.loads(index_path.read_text(encoding="utf-8"))
    selected: dict[str, dict[str, Any]] = {}
    for row in index["archives"]:
        if row["symbol"] == "XLF" and (
            row["feature_day"] not in selected or row["size"] > selected[row["feature_day"]]["size"]
        ):
            selected[row["feature_day"]] = row
    captures, guards = [], []
    for day, row in sorted(selected.items()):
        snapshot, guard = read_guarded_archive(
            path=Path(row["path"]),
            expected_hash=row["source_snapshot_hash"],
            symbol="XLF",
            day=date.fromisoformat(day),
            manifest=manifest,
            operations=Path(config["operations_database"]),
            declarations=Path(config["declaration_database"]),
            experiment_store=Path(config["store"]),
        )
        captures.append(snapshot)
        guards.append(guard)
    if not captures:
        raise ValueError("NO_PERMITTED_REAL_ARCHIVES")
    actual_now = datetime.now(UTC)
    sessions = {
        s.session_date: s
        for capture in captures
        for s in capture.sessions.sessions
        if s.session_date.isoformat() in selected and s.close_at <= actual_now
    }
    calendar = LearningCalendar(
        authority="LOCAL_ARCHIVED_XNYS_V1",
        sequence=XNYSSessionSequence(
            calendar="XNYS", sessions=tuple(sessions[d] for d in sorted(sessions))
        ),
    )
    chosen = next(c for c in reversed(captures) if len(c.inputs) >= 14)
    bars = tuple(item.bar for item in chosen.inputs)
    feature_at = sorted({b.timestamp for b in bars})[6] + timedelta(minutes=5)
    (row,) = build_features(
        tuple(b for b in bars if b.timestamp < feature_at),
        chosen.sessions,
        decision_at=feature_at,
        receipt_at=feature_at,
        symbols=("XLF",),
        source_snapshot_hash=chosen.identity,
    )
    missing_benchmark_rows = build_features(
        tuple(b for b in bars if b.timestamp < feature_at and b.symbol != "SPY"),
        chosen.sessions,
        decision_at=feature_at,
        receipt_at=feature_at,
        symbols=("XLF",),
        source_snapshot_hash=chosen.identity,
    )
    if missing_benchmark_rows:
        raise ValueError("MISSING_REAL_BENCHMARK_WAS_NOT_REFUSED")
    reference = next(
        b.close
        for b in bars
        if b.symbol == "XLF" and b.timestamp == feature_at - timedelta(minutes=5)
    )
    later = next(
        b.close
        for b in bars
        if b.symbol == "XLF" and b.timestamp == feature_at + timedelta(minutes=25)
    )
    close = next(
        s.close_at for s in chosen.sessions.sessions if s.session_date == feature_at.date()
    )
    scenarios = []
    learning_receipt: dict[str, Any] | None = None
    for scenario in ("base", "latency", "partial", "reject"):
        core_config = CoreConfig(
            account_id="local-sim-" + scenario,
            symbol="XLF",
            evidence_lane="LOCAL_SIM_REAL_DATA",
            owner="local-simulation",
            release_id="m5-local-bars-simulation",
        )
        broker = LocalSimulationBroker(
            account_id=core_config.account_id, now=feature_at, scenario=scenario
        )
        directory = path / scenario
        directory.mkdir()
        with Database.create(directory / "core.db") as db:
            store = CoreStore(db, core_config)
            proof = initialize_fixture(store, broker, now=feature_at, entry_enabled=True)
            model = ModelArtifact(feature_version="five-minute-price-alpha-v1")
            initialize_fixture_model(store, model, at=feature_at)
            observation = Observation(
                recorded_at_utc=feature_at,
                evidence_lane="LOCAL_SIM_REAL_DATA",
                release_id=core_config.release_id,
                feed="LOCAL_SIM_REAL_DATA",
                capture_received_at=chosen.received_at,
                symbol="XLF",
                source_start=feature_at - timedelta(minutes=35),
                source_end=feature_at,
                available_at=feature_at,
                raw_data_digest=chosen.identity,
                price=reference,
                features=tuple(zip(FEATURE_NAMES, row.values, strict=True)),
                feature_version="five-minute-price-alpha-v1",
                dependencies=("SPY",),
                dependency_available_at=(feature_at,),
                sequence=1,
                session_close=close,
            )
            broker.set_mark("XLF", reference, as_of=feature_at)
            decision = await trader_cycle(store, broker, observation, model, proof, now=feature_at)
            broker.now = feature_at + timedelta(minutes=30)
            broker.set_mark("XLF", later, as_of=broker.now)
            await guardian_cycle(store, broker, now=broker.now)
            costs = []
            for fill in broker.fills:
                order = next(
                    o for o in broker.orders.values() if o.broker_order_id == fill.broker_order_id
                )
                cost = CostItem(
                    recorded_at_utc=broker.now,
                    evidence_lane="LOCAL_SIM_REAL_DATA",
                    release_id=core_config.release_id,
                    fill_id=fill.fill_id,
                    effective_start=feature_at,
                    effective_end=broker.now,
                    amount=fill.quantity * fill.price * Decimal(".00005"),
                    source="MODELED",
                    component="MODELED_SLIPPAGE",
                    document_id="LOCAL_ASSUMPTION_EXTRA_IMPACT_0.5BPS_PER_FILL",
                )
                costs.append(
                    BrokerEvent(
                        recorded_at_utc=broker.now,
                        evidence_lane="LOCAL_SIM_REAL_DATA",
                        release_id=core_config.release_id,
                        occurred_at=broker.now,
                        provider="LOCAL_SIM_REAL_DATA",
                        account_id=core_config.account_id,
                        endpoint="local/modeled-cost",
                        client_order_id=order.client_order_id,
                        event_id=cost.id,
                        raw_digest=content_id(cost),
                        cost=cost,
                    )
                )
            import_broker_events(store, tuple(costs))
            outcomes = process_outcomes(store, cutoff=broker.now)
            if scenario != "reject":
                assert len(outcomes.outcomes) == 1
                assert outcomes.outcomes[0].net_pnl is None
            cutoff = max(broker.now, max(s.close_at for s in calendar.sequence.sessions))
            receipt = consider_learning(
                store,
                LearningPolicy(
                    parent_model=model,
                    evidence_lane="LOCAL_SIM_REAL_DATA",
                    dataset="LOCAL_SIM_REAL_DATA",
                ),
                cutoff=cutoff,
                calendar=calendar,
                execution_clock=LocalClock(cutoff),
            )
            learning_receipt = receipt.model_dump(mode="json")
            scenarios.append(
                {
                    "scenario": scenario,
                    "bundle": store.export_loop(decision.id),
                    "learning_receipt": learning_receipt,
                }
            )
    return {
        "evidence_lane": "LOCAL_SIM_REAL_DATA",
        "source_stores_read_only": True,
        "typed_manifest_id": content_id(manifest),
        "source_session_days": sorted(selected),
        "completed_source_session_count_at_actual_execution": len(sessions),
        "latest_source_session_may_be_incomplete": len(sessions) < len(selected),
        "guards": guards,
        "learning_receipt": learning_receipt,
        "scenarios": scenarios,
        "prospective_eligibility_claimed": False,
        "missing_benchmark_feature_rows": len(missing_benchmark_rows),
        "time_semantics": (
            "Retrospective virtual replay at bar closes; original actual capture times retained. "
            "No historical prospective eligibility claim."
        ),
        "assumptions": {
            "spread": "modeled 4 bps round trip; no observed NBBO",
            "latency": (
                "modeled additional 1 bps each side in latency scenario; no observed latency"
            ),
            "partial": "modeled half entry then cancellation; no observed queue priority",
            "reject": "modeled terminal entry rejection",
            "extra_impact": "0.5 bps per fill in modeled field only",
            "fees": "missing observed/contractual fee authority; actual net remains null",
            "training": (
                "fewer than ten recorded exchange sessions and costs pending; no real-data fit"
            ),
        },
    }
