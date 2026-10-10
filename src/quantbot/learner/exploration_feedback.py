"""Immutable descriptive updates from completed, owned and protected trade episodes.

These are experience receipts, not fitted predictors, strategy promotion or authority.
Actual fill-price economics already include execution-price effects; modeled costs are
not subtracted again. Missing fees, ownership or residual inventory never become zeros.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_lots import DeadlineBrokerClockEvidence, LotEvent, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore, StoredEvent
from quantbot.learner.exploration import ExplorationObservation, ExplorationPolicy
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id


class ExplorationFeedback(LearnerModel):
    experiment_id: Text
    policy_id: Digest
    lot_id: Digest
    observation_id: Digest
    source_state_hash: Digest
    recorded_at: datetime
    result: Literal["WIN", "LOSS", "FLAT"]
    net_pnl: Decimal
    actual_fees: Decimal
    actual_net_return: Decimal
    executed_gross_return: Decimal
    baseline_executed_return_differences: dict[str, Decimal]
    comparison_scope: Literal["EXECUTED_EXIT_NOT_FIXED_TARGET"] = "EXECUTED_EXIT_NOT_FIXED_TARGET"
    hypothesis: Literal[
        "NET_GAIN_OBSERVED_NOT_PROOF_OF_EDGE", "ADVERSE_EXECUTED_PRICE_MOVE",
        "COSTS_EXCEEDED_GROSS_PRICE_GAIN", "NET_FLAT_OUTCOME",
    ]
    predictor_fit: Literal[False] = False
    grants_execution: Literal[False] = False
    synthetic: bool


def _source_hash(
    rows: tuple[LotEvent, ...], anchors: tuple[DeadlineBrokerClockEvidence, ...] = (),
) -> str:
    return content_id(sorted(content_id(row) for row in (*rows, *anchors)))


def _anchors(
    store: ExperimentStore, experiment: str, lot_id: str, now: datetime,
) -> tuple[DeadlineBrokerClockEvidence, ...]:
    return tuple(record.decode(DeadlineBrokerClockEvidence)
                 for record in store.read_as_of(experiment, now)
                 if record.kind == "DeadlineBrokerClockEvidence"
                 and record.decode(DeadlineBrokerClockEvidence).lot_id == lot_id)


def _observations(
    events: tuple[StoredEvent, ...], policy: ExplorationPolicy,
) -> dict[str, ExplorationObservation]:
    manifests = [row.decode(ExperimentManifest) for row in events
                 if row.kind == "ExperimentManifest"]
    if (len(manifests) != 1 or content_id(manifests[0]) != policy.manifest_hash
            or manifests[0].synthetic != policy.synthetic
            or not any(row.kind == "ExplorationPolicy" and row.decode(ExplorationPolicy) == policy
                       for row in events)):
        raise ValueError("IMMUTABLE_EXPLORATION_POLICY_REQUIRED")
    rows = [event.decode(ExplorationObservation) for event in events
            if event.kind == "ExplorationObservation"]
    counts = Counter(row.prediction_id for row in rows)
    return {row.prediction_id: row for row in rows
            if row.policy_id == policy.policy_id and counts[row.prediction_id] == 1}


def _feedback(
    store: ExperimentStore, policy: ExplorationPolicy, lot_id: str,
    rows: tuple[LotEvent, ...], observations: dict[str, ExplorationObservation], *, now: datetime,
) -> ExplorationFeedback | None:
    declarations = [row for row in rows if row.kind == "ENTRY_DECLARED"]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    observation = observations.get(declaration.decision_id)
    anchors = _anchors(store, policy.experiment_id, lot_id, now)
    if (observation is None or observation.synthetic != policy.synthetic
            or observation.experiment_id != policy.experiment_id
            or observation.features.symbol != declaration.symbol
            or not policy.recorded_at <= observation.recorded_at <= declaration.occurred_at <= now
            or observation.features.decision_at > observation.recorded_at
            or any(row.recorded_at > now for row in rows)):
        return None
    outcome = resolve_lot(store, lot_id, now=now)
    if (outcome.status != "COMPLETE" or outcome.net_pnl is None or outcome.fees is None
            or outcome.synthetic != policy.synthetic):
        return None
    notional = sum((row.quantity * row.price for row in rows
                    if row.kind == "FILL" and row.side == "BUY" and row.price is not None),
                   Decimal(0))
    if notional <= 0:
        raise ValueError("EXACT_EXECUTED_ENTRY_NOTIONAL_REQUIRED")
    net, gross = outcome.net_pnl / notional, (outcome.net_pnl + outcome.fees) / notional
    hypothesis = (
        "NET_GAIN_OBSERVED_NOT_PROOF_OF_EDGE" if outcome.net_pnl > 0
        else "NET_FLAT_OUTCOME" if outcome.net_pnl == 0
        else "ADVERSE_EXECUTED_PRICE_MOVE" if gross < 0
        else "COSTS_EXCEEDED_GROSS_PRICE_GAIN"
    )
    return ExplorationFeedback(
        experiment_id=policy.experiment_id, policy_id=policy.policy_id, lot_id=lot_id,
        observation_id=observation.prediction_id,
        source_state_hash=_source_hash(rows, anchors), recorded_at=now,
        result="WIN" if outcome.net_pnl > 0 else "LOSS" if outcome.net_pnl < 0 else "FLAT",
        net_pnl=outcome.net_pnl, actual_fees=outcome.fees, actual_net_return=net,
        executed_gross_return=gross,
        baseline_executed_return_differences={
            model: gross - Decimal(str(score))
            for model, score in observation.baseline_scores.items()
        }, hypothesis=hypothesis, synthetic=policy.synthetic,
    )


def journal_exploration_feedback(
    store: ExperimentStore, policy: ExplorationPolicy, *, now: datetime,
) -> tuple[ExplorationFeedback, ...]:
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    events = store.read_as_of(policy.experiment_id, now)
    observations = _observations(events, policy)
    existing = {(row.lot_id, row.source_state_hash) for event in events
                if event.kind == "ExplorationFeedback"
                for row in (event.decode(ExplorationFeedback),)
                if row.policy_id == policy.policy_id}
    lots: dict[str, list[LotEvent]] = {}
    for event in events:
        if event.kind == "LotEvent":
            row = event.decode(LotEvent)
            lots.setdefault(row.lot_id, []).append(row)
    result = []
    for lot_id, rows in sorted(lots.items()):
        feedback = _feedback(store, policy, lot_id, tuple(rows), observations, now=now)
        if feedback is not None and (lot_id, feedback.source_state_hash) not in existing:
            store.append(feedback, idempotency_key="exploration-feedback:" + content_id(
                (policy.policy_id, lot_id, feedback.source_state_hash)))
            result.append(feedback)
    return tuple(result)


def verified_feedback(
    store: ExperimentStore, policy: ExplorationPolicy, *, now: datetime,
) -> tuple[ExplorationFeedback, ...]:
    """Later changed evidence invalidates learning input, never erases earlier receipts."""
    events = store.read_as_of(policy.experiment_id, now)
    observations = _observations(events, policy)
    lots = tuple(event.decode(LotEvent) for event in events if event.kind == "LotEvent")
    feedback = [event.decode(ExplorationFeedback) for event in events
                if event.kind == "ExplorationFeedback"
                and event.decode(ExplorationFeedback).policy_id == policy.policy_id]
    counts = Counter((row.lot_id, row.source_state_hash) for row in feedback)
    result = []
    for row in feedback:
        current = tuple(item for item in lots if item.lot_id == row.lot_id)
        if (counts[row.lot_id, row.source_state_hash] == 1
                and resolve_lot(store, row.lot_id, now=now).status == "COMPLETE"
                and row.source_state_hash == _source_hash(
                    current, _anchors(store, policy.experiment_id, row.lot_id, now))
                and _feedback(store, policy, row.lot_id, current, observations,
                              now=row.recorded_at) == row):
            result.append(row)
    return tuple(sorted(result, key=lambda row: (row.recorded_at, row.lot_id)))
