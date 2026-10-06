"""Per-instrument coverage from genuine five-minute receipts; no inference or orders."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.five_minute import FiveMinuteFeatureRow, FiveMinuteLabel, build_features
from quantbot.learner.five_minute import resolve_label as resolve_market_label
from quantbot.learner.forward_source import Snapshot
from quantbot.learner.models import LearnerModel, Text


class CoverageReport(LearnerModel):
    symbol: Text
    status: Literal["AVAILABLE", "MISSING", "WARMUP", "CLOSED_SESSION", "NO_ENTRY_WINDOW",
                    "WAITING_BOUNDARY", "NOT_STARTED", "PROTECTED_WINDOW"]
    reason: Text
    source_hash: Digest


class ObservationBatch(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    snapshot_hash: Digest
    coverage: tuple[CoverageReport, ...]
    features: tuple[FiveMinuteFeatureRow, ...]
    synthetic: bool = False


def _source(snapshot: Snapshot, now: datetime) -> Snapshot:
    snapshot = Snapshot.model_validate(snapshot.model_dump())
    if snapshot.source_id != "alpaca:iex:raw:5min":
        raise ValueError("genuine fixed five-minute IEX contract required")
    if snapshot.received_at > now:
        raise ValueError("FUTURE_SOURCE_RECEIPT")
    if any(receipt.status_code != 200 for receipt in snapshot.receipts):
        raise ValueError("FAILED_PROVIDER_RECEIPT")
    return snapshot


def build_observation_batch(
    manifest: ExperimentManifest, snapshot: Snapshot, *, now: datetime
) -> ObservationBatch:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    snapshot = _source(snapshot, now)
    session = next((s for s in snapshot.sessions.sessions if s.open_at <= now < s.close_at), None)
    bars = tuple(item.bar for item in snapshot.inputs)
    coverage: list[CoverageReport] = []
    features: list[FiveMinuteFeatureRow] = []
    for identity in manifest.roster:
        status: Literal["AVAILABLE", "MISSING", "WARMUP", "CLOSED_SESSION", "NO_ENTRY_WINDOW",
                        "WAITING_BOUNDARY", "NOT_STARTED"]
        if now < manifest.prospective_start_at:
            status = "NOT_STARTED"
        elif session is None:
            status = "CLOSED_SESSION"
        elif now < session.open_at + timedelta(minutes=35):
            status = "WARMUP"
        else:
            rows = build_features(
                bars, snapshot.sessions, decision_at=now, receipt_at=snapshot.received_at,
                symbols=(identity.symbol,), source_snapshot_hash=snapshot.identity,
            )
            if rows:
                features.extend(rows)
                status = "AVAILABLE"
            else:
                cutoff = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
                if cutoff + timedelta(minutes=30) > session.close_at:
                    status = "NO_ENTRY_WINDOW"
                elif now > cutoff + timedelta(seconds=120):
                    status = "WAITING_BOUNDARY"
                else:
                    status = "MISSING"
        coverage.append(CoverageReport(symbol=identity.symbol, status=status, reason=status,
                                       source_hash=snapshot.identity))
    return ObservationBatch(experiment_id=manifest.experiment_id, recorded_at=now,
                            snapshot_hash=snapshot.identity, coverage=tuple(coverage),
                            features=tuple(features), synthetic=manifest.synthetic)


def resolve_batch_labels(
    batch: ObservationBatch, snapshot: Snapshot, *, now: datetime
) -> tuple[FiveMinuteLabel, ...]:
    snapshot = _source(snapshot, now)
    batch = ObservationBatch.model_validate(batch.model_dump())
    labels = []
    for row in batch.features:
        label = resolve_market_label(
            row, tuple(item.bar for item in snapshot.inputs), resolved_at=now,
            receipt_at=snapshot.received_at, source_snapshot_hash=snapshot.identity,
        )
        if label is not None:
            labels.append(label)
    return tuple(labels)
