"""Bounded recoverable shadow ticks; collection never fits or activates a model."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self

from pydantic import model_validator

from quantbot.learner.estimators import predict
from quantbot.learner.experiment_contract import (
    CandidateArtifact,
    DecisionRecord,
    Digest,
    ExperimentManifest,
    InstrumentIdentity,
)
from quantbot.learner.experiment_observation import (
    CoverageReport,
    ObservationBatch,
    build_observation_batch,
    resolve_batch_labels,
)
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.five_minute import FiveMinuteFeatureRow, FiveMinuteLabel
from quantbot.learner.forward_source import Snapshot
from quantbot.learner.models import LearnerModel, Text
from quantbot.learner.paper_runner import IntradayPaperServices
from quantbot.market_data.base import MarketDataError
from quantbot.operations.cycle import CycleResult, DataSyncRunner
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from quantbot.runtime import Runtime


@dataclass(frozen=True)
class ActiveCycleContext:
    """Explicit commissioned service dependencies, absent from shadow launcher."""

    runtime: Runtime
    data_sync: DataSyncRunner
    clock: Callable[[], datetime]


class ExperimentCycleReceipt(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    tick_id: Digest
    activation_id: Digest
    result: CycleResult
    synthetic: Literal[False] = False


class ResolvedMarketLabel(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    label: FiveMinuteLabel
    synthetic: bool

    @model_validator(mode="after")
    def available(self) -> Self:
        if self.label.available_at > self.recorded_at:
            raise ValueError("LABEL_AVAILABILITY_CANNOT_BE_BACKDATED")
        return self


class TickReceipt(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    tick_id: Digest
    input_hash: Digest
    status: Literal[
        "CLOCK_SYNC_REQUIRED",
        "LEASE_BUSY",
        "NORMAL_WAIT",
        "OBSERVING",
        "WAITING_FOR_COVERAGE",
        "FAULTED",
    ]
    reason: Text
    next_expected_event: Text
    next_wakeup_at: datetime
    clock_verified: bool
    observations_recorded: int = 0
    labels_recorded: int = 0
    decisions_recorded: int = 0
    fits: int = 0
    fit_budget_reserved: int = 0
    intents: int = 0
    fit_request_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    synthetic: bool = False


def feature_key(row: FiveMinuteFeatureRow) -> str:
    """One predeclared instrument/cutoff/target, retaining its first receipt."""
    return content_id(
        {
            "symbol": row.symbol,
            "feature_at": row.feature_at,
            "target_at": row.target_at,
            "contract": row.contract,
        }
    )


async def collect_observations(
    manifest: ExperimentManifest,
    *,
    provider: Callable[[InstrumentIdentity], Awaitable[Snapshot]],
    clock: Callable[[], datetime],
    clock_health: Callable[[datetime], bool],
) -> ObservationBatch:
    """Capture per-instrument panels, then resample the actual clock before decisions.

    A wholly absent symbol can fail its capture without invalidating another symbol's
    genuine panel. No bar fill-in, backward sliding or training call occurs here.
    """
    initial = clock()
    if not clock_health(initial):
        raise ValueError("CLOCK_SYNC_REQUIRED")
    sources: dict[str, Snapshot | None] = {}
    protected: set[str] = set()
    for identity in manifest.roster:
        try:
            sources[identity.symbol] = await asyncio.wait_for(provider(identity), timeout=5)
        except (TimeoutError, ValueError, OSError, MarketDataError) as error:
            sources[identity.symbol] = None
            if isinstance(error, ValueError) and str(error) == "PROTECTED_OBSERVATION_WINDOW":
                protected.add(identity.symbol)
    actual = clock()
    if actual < initial or not clock_health(actual):
        raise ValueError("CLOCK_SYNC_REQUIRED")
    coverage: list[CoverageReport] = []
    features: list[FiveMinuteFeatureRow] = []
    for identity in manifest.roster:
        source = sources[identity.symbol]
        if source is None:
            coverage.append(
                CoverageReport(
                    symbol=identity.symbol,
                    status="PROTECTED_WINDOW" if identity.symbol in protected else "MISSING",
                    reason="PROTECTED_OBSERVATION_WINDOW"
                    if identity.symbol in protected
                    else "CAPTURE_UNAVAILABLE",
                    source_hash=content_id(
                        {"asset_id": identity.asset_id, "status": "CAPTURE_UNAVAILABLE"}
                    ),
                )
            )
            continue
        batch = build_observation_batch(manifest, source, now=actual)
        coverage.extend(row for row in batch.coverage if row.symbol == identity.symbol)
        features.extend(row for row in batch.features if row.symbol == identity.symbol)
    return ObservationBatch(
        experiment_id=manifest.experiment_id,
        recorded_at=actual,
        snapshot_hash=content_id(sorted(row.source_hash for row in coverage)),
        coverage=tuple(coverage),
        features=tuple(features),
        synthetic=manifest.synthetic,
    )


def _validate_batch(manifest: ExperimentManifest, batch: ObservationBatch, now: datetime) -> None:
    symbols = {identity.symbol for identity in manifest.roster}
    coverage = {row.symbol: row for row in batch.coverage}
    if (
        batch.experiment_id != manifest.experiment_id
        or batch.synthetic != manifest.synthetic
        or batch.recorded_at > now
        or set(coverage) != symbols
        or len(coverage) != len(batch.coverage)
    ):
        raise ValueError("OBSERVATION_MANIFEST_MISMATCH")
    for row in batch.features:
        if (
            row.symbol not in symbols
            or coverage[row.symbol].status != "AVAILABLE"
            or row.source_snapshot_hash != coverage[row.symbol].source_hash
            or not manifest.prospective_start_at <= row.decision_at <= batch.recorded_at
        ):
            raise ValueError("OBSERVATION_LINEAGE_UNVERIFIED")


async def coordinator_tick(
    store: ExperimentStore,
    manifest: ExperimentManifest,
    *,
    now: datetime,
    observation: ObservationBatch | None,
    execution: IntradayPaperServices | None,
    clock_verified: bool = False,
    owner: str = "coordinator",
    label_snapshots: Sequence[Snapshot] = (),
    operational_path: Path | None = None,
    active_cycle: ActiveCycleContext | None = None,
) -> TickReceipt:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    state = store.state(manifest.experiment_id)
    if state.manifest_hash != content_id(manifest):
        raise ValueError("FROZEN_MANIFEST_MISMATCH")
    activation = None
    if active_cycle is not None:
        from quantbot.learner.experiment_activation import active_artifact

        activation = active_artifact(store, manifest, now=now)
        if (
            manifest.synthetic
            or activation is None
            or activation.synthetic
            or execution is None
            or execution.runner.model.model_id != activation.model_id
            or execution.runner.binding.configuration_hash != activation.binding_hash
        ):
            raise ValueError("EXACT_ARTIFACT_BINDING_ACTIVATION_REQUIRED")
    tick_id = content_id({"experiment": manifest.experiment_id, "wake": now})
    input_hash = content_id(
        {
            "observation": observation,
            "label_sources": label_snapshots,
            "clock_verified": clock_verified,
            "activation_id": activation.activation_id if activation is not None else None,
        }
    )
    base = dict(
        experiment_id=manifest.experiment_id,
        recorded_at=now,
        tick_id=tick_id,
        input_hash=input_hash,
        next_wakeup_at=now + timedelta(minutes=2),
        clock_verified=clock_verified,
        synthetic=manifest.synthetic,
    )
    if not clock_verified:
        # A diagnostic receipt is not a financial-evidence timestamp or lease authority.
        return TickReceipt(
            **base,
            status="CLOCK_SYNC_REQUIRED",
            reason="CLOCK_SYNC_REQUIRED",
            next_expected_event="CLOCK_VERIFICATION",
        )
    if state.state == "FAULTED":
        return TickReceipt(
            **base,
            status="FAULTED",
            reason=state.reason,
            next_expected_event="OPERATOR_REVIEW_REQUIRED",
        )
    if not store.acquire_lease(
        manifest.experiment_id, owner=owner, now=now, duration=timedelta(minutes=3)
    ):
        return TickReceipt(
            **base,
            status="LEASE_BUSY",
            reason="OTHER_COORDINATOR_OWNS_LEASE",
            next_expected_event="NEXT_COORDINATOR_WAKEUP",
        )
    events = store.read_as_of(manifest.experiment_id, now)
    for event in events:
        if event.kind == "TickReceipt":
            receipt = event.decode(TickReceipt)
            if receipt.tick_id == tick_id:
                if receipt.input_hash != input_hash:
                    raise ValueError("CONFLICTING_TICK_INPUT")
                return receipt
    if execution is not None:
        from quantbot.learner.experiment_execution import dispatch_pending

        await dispatch_pending(store, services=execution, now=now)
        operational_path = execution.runner._database.path
    if operational_path is not None:
        from quantbot.learner.experiment_lot_reader import refresh_lot_journal

        refreshed = refresh_lot_journal(store, manifest, operational_path=operational_path, now=now)
    else:
        refreshed = ()
    known = [event.decode(ObservationBatch) for event in events if event.kind == "ObservationBatch"]
    labeled = {
        feature_key(event.decode(ResolvedMarketLabel).label.features)
        for event in events
        if event.kind == "ResolvedMarketLabel"
    }
    observed = {feature_key(row) for batch in known for row in batch.features}
    labels_recorded, observations_recorded = 0, 0
    event_ids = [content_id(row) for row in refreshed]
    # Resolve prior targets even after close. Market labels are never trade returns.
    for batch in known:
        for source in label_snapshots:
            for label in resolve_batch_labels(batch, source, now=now):
                key = feature_key(label.features)
                if key in labeled:
                    continue
                record = ResolvedMarketLabel(
                    experiment_id=manifest.experiment_id,
                    recorded_at=now,
                    label=label,
                    synthetic=manifest.synthetic,
                )
                store.append(record, idempotency_key="market-label:" + key)
                labeled.add(key)
                labels_recorded += 1
                event_ids.append(content_id(record))
    if observation is not None:
        observation = ObservationBatch.model_validate(observation.model_dump())
        _validate_batch(manifest, observation, now)
        observations_recorded = len({feature_key(row) for row in observation.features} - observed)
        store.append(observation, idempotency_key="observation:" + content_id(observation))
        event_ids.append(content_id(observation))
    from quantbot.learner.experiment_research import FitReceipt, FitRequest, FitStart

    starts = [event.decode(FitStart) for event in events if event.kind == "FitStart"]
    fit_receipts = [event.decode(FitReceipt) for event in events if event.kind == "FitReceipt"]
    pending_fits = (
        tuple(
            request.request_id
            for event in events
            if event.kind == "FitRequest"
            for request in (event.decode(FitRequest),)
            if request.request_id not in {row.request_id for row in starts}
        )
        if len(starts) < manifest.fit_budget
        else ()
    )
    decisions_recorded = 0
    active_decisions: list[DecisionRecord] = []
    candidates = [
        event.decode(CandidateArtifact) for event in events if event.kind == "CandidateArtifact"
    ]
    decision_keys = {
        (row.model_id, feature_key(row.features))
        for event in events
        if event.kind == "DecisionRecord"
        for row in (event.decode(DecisionRecord),)
    }
    for candidate in candidates:
        if (
            candidate.manifest_hash != state.manifest_hash
            or candidate.experiment_id != manifest.experiment_id
            or candidate.synthetic != manifest.synthetic
            or not manifest.registered_at <= candidate.recorded_at <= now
        ):
            raise ValueError("CANDIDATE_MANIFEST_MISMATCH")
        if not manifest.synthetic and not any(
            fit.status == "SUCCEEDED"
            and fit.charged is True
            and fit.candidate_id == candidate.model_id
            for fit in fit_receipts
        ):
            raise ValueError("CANONICAL_CHARGED_ARTIFACT_RECEIPT_REQUIRED")
        rows = tuple(
            row
            for row in (observation.features if observation else ())
            if candidate.recorded_at <= row.decision_at
            and (candidate.model_id, feature_key(row)) not in decision_keys
        )
        for row, score in zip(
            rows, predict(candidate.artifact, tuple(row.values for row in rows)), strict=True
        ):
            activated = (
                activation is not None
                and candidate.model_id == activation.model_id
                and activation.recorded_at <= row.decision_at
            )
            decision = DecisionRecord(
                experiment_id=manifest.experiment_id,
                recorded_at=now,
                model_id=candidate.model_id,
                features=row,
                score=score,
                action="BUY" if score > manifest.round_trip_cost_bps / 10000 else "WAIT",
                reason=("PAPER_SCORE_ABOVE_COST" if activated else "SHADOW_SCORE_ABOVE_COST")
                if score > manifest.round_trip_cost_bps / 10000
                else "SCORE_BELOW_COST",
                synthetic=manifest.synthetic,
                activation_id=activation.activation_id if activated and activation else None,
                paper_eligible=activated and score > manifest.round_trip_cost_bps / 10000,
            )
            store.append(
                decision,
                idempotency_key="shadow-decision:" + candidate.model_id + ":" + feature_key(row),
            )
            decision_keys.add((candidate.model_id, feature_key(row)))
            decisions_recorded += 1
            event_ids.append(decision.decision_id)
            if activated:
                active_decisions.append(decision)
    cycle_result = None
    if (
        active_cycle is not None
        and execution is not None
        and activation is not None
        and active_decisions
    ):
        from quantbot.learner.experiment_execution import run_experiment_cycle

        cycle_result = await run_experiment_cycle(
            active_cycle.runtime,
            execution,
            manifest=manifest,
            decisions=active_decisions,
            store=store,
            owner=owner,
            data_sync=active_cycle.data_sync,
            clock=active_cycle.clock,
            run_id="adaptive-" + tick_id,
        )
        cycle_receipt = ExperimentCycleReceipt(
            experiment_id=manifest.experiment_id,
            recorded_at=cycle_result.finished_at,
            tick_id=tick_id,
            activation_id=activation.activation_id,
            result=cycle_result,
        )
        store.append(cycle_receipt, idempotency_key="cycle:" + tick_id)
        event_ids.append(content_id(cycle_receipt))
    statuses = {row.status for row in observation.coverage} if observation else set()
    if state.state == "FAULTED":
        status, reason, next_event = "FAULTED", state.reason, "OPERATOR_REVIEW_REQUIRED"
    elif "CLOSED_SESSION" in statuses:
        status, reason, next_event = "NORMAL_WAIT", "MARKET_CLOSED", "NEXT_EXCHANGE_SESSION"
    elif statuses == {"PROTECTED_WINDOW"}:
        status, reason, next_event = (
            "NORMAL_WAIT",
            "PROTECTED_OBSERVATION_WINDOW",
            "CANONICAL_WINDOW_ELIGIBILITY",
        )
    elif statuses and statuses <= {"WARMUP", "WAITING_BOUNDARY", "NOT_STARTED", "NO_ENTRY_WINDOW"}:
        status, reason, next_event = "NORMAL_WAIT", sorted(statuses)[0], "NEXT_FIVE_MINUTE_BOUNDARY"
    elif "AVAILABLE" in statuses:
        status, reason, next_event = (
            "OBSERVING",
            "FORWARD_EVIDENCE_ACCUMULATING",
            "NEXT_TARGET_LABEL",
        )
    else:
        status, reason, next_event = (
            "WAITING_FOR_COVERAGE",
            "NO_ELIGIBLE_SOURCE",
            "DATA_PROVIDER_RECOVERY",
        )
    if state.state in {"REGISTERED", "WAITING_FOR_COVERAGE"} and status == "OBSERVING":
        store.transition(
            manifest.experiment_id,
            expected_revision=state.revision,
            target="OBSERVING",
            reason=reason,
            now=now,
        )
    receipt = TickReceipt(
        **base,
        status=status,
        reason=reason,
        next_expected_event=next_event,
        observations_recorded=observations_recorded,
        labels_recorded=labels_recorded,
        decisions_recorded=decisions_recorded,
        fits=sum(row.charged is True for row in fit_receipts),
        fit_budget_reserved=len(starts),
        fit_request_ids=pending_fits,
        intents=cycle_result.orders_submitted if cycle_result is not None else 0,
        event_ids=tuple(event_ids),
    )
    store.append(receipt, idempotency_key="tick:" + tick_id)
    return receipt
