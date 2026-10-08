"""Immutable descriptive updates from completed, owned and protected trade episodes.

These are experience receipts, not fitted predictors, strategy promotion or authority.
Actual fill-price economics already include execution-price effects; modeled costs are
not subtracted again. Missing fees, ownership or residual inventory never become zeros.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from quantbot.learner.experiment_contract import Digest
from quantbot.learner.experiment_lots import DeadlineBrokerClockEvidence, LotEvent, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
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


def journal_exploration_feedback(
    store: ExperimentStore, policy: ExplorationPolicy, *, now: datetime,
) -> tuple[ExplorationFeedback, ...]:
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    events = store.read_as_of(policy.experiment_id, now)
    if not any(row.kind == "ExplorationPolicy" and row.decode(ExplorationPolicy) == policy
               for row in events):
        raise ValueError("IMMUTABLE_EXPLORATION_POLICY_REQUIRED")
    observations = {row.prediction_id: row for event in events
                    if event.kind == "ExplorationObservation"
                    for row in (event.decode(ExplorationObservation),)
                    if row.policy_id == policy.policy_id}
    existing = {event.decode(ExplorationFeedback).lot_id for event in events
                if event.kind == "ExplorationFeedback"
                and event.decode(ExplorationFeedback).policy_id == policy.policy_id}
    lots: dict[str, list[LotEvent]] = {}
    for event in events:
        if event.kind == "LotEvent":
            row = event.decode(LotEvent)
            lots.setdefault(row.lot_id, []).append(row)
    result = []
    for lot_id, rows in sorted(lots.items()):
        if lot_id in existing:
            continue
        declaration = next((row for row in rows if row.kind == "ENTRY_DECLARED"), None)
        observation = observations.get(declaration.decision_id) if declaration else None
        if (observation is None or declaration is None
                or declaration.occurred_at < observation.recorded_at
                or observation.synthetic != policy.synthetic
                or observation.features.symbol != declaration.symbol):
            continue
        outcome = resolve_lot(store, lot_id, now=now)
        if (outcome.status != "COMPLETE" or outcome.net_pnl is None or outcome.fees is None
                or outcome.synthetic != policy.synthetic):
            continue
        notional = sum((row.quantity * row.price for row in rows
                        if row.kind == "FILL" and row.side == "BUY" and row.price is not None),
                       Decimal(0))
        if notional <= 0:
            raise ValueError("EXACT_EXECUTED_ENTRY_NOTIONAL_REQUIRED")
        net, gross = outcome.net_pnl / notional, (outcome.net_pnl + outcome.fees) / notional
        win = outcome.net_pnl > 0
        hypothesis = (
            "NET_GAIN_OBSERVED_NOT_PROOF_OF_EDGE" if win
            else "NET_FLAT_OUTCOME" if outcome.net_pnl == 0
            else "ADVERSE_EXECUTED_PRICE_MOVE" if gross < 0
            else "COSTS_EXCEEDED_GROSS_PRICE_GAIN"
        )
        feedback = ExplorationFeedback(
            experiment_id=policy.experiment_id, policy_id=policy.policy_id, lot_id=lot_id,
            observation_id=observation.prediction_id, source_state_hash=_source_hash(
                tuple(rows), _anchors(store, policy.experiment_id, lot_id, now)),
            recorded_at=now, result="WIN" if win else "LOSS" if outcome.net_pnl < 0 else "FLAT",
            net_pnl=outcome.net_pnl, actual_fees=outcome.fees, actual_net_return=net,
            executed_gross_return=gross,
            baseline_executed_return_differences={
                model: gross - Decimal(str(score))
                for model, score in observation.baseline_scores.items()
            },
            hypothesis=hypothesis, synthetic=policy.synthetic,
        )
        store.append(feedback, idempotency_key="exploration-feedback:" + lot_id)
        result.append(feedback)
    return tuple(result)


def verified_feedback(
    store: ExperimentStore, policy: ExplorationPolicy, *, now: datetime,
) -> tuple[ExplorationFeedback, ...]:
    """Later changed evidence invalidates learning input, never erases earlier receipts."""
    events = store.read_as_of(policy.experiment_id, now)
    lots = tuple(event.decode(LotEvent) for event in events if event.kind == "LotEvent")
    result = []
    for event in events:
        if event.kind != "ExplorationFeedback":
            continue
        row = event.decode(ExplorationFeedback)
        current = tuple(item for item in lots if item.lot_id == row.lot_id)
        if (row.policy_id == policy.policy_id and row.source_state_hash == _source_hash(
                current, _anchors(store, policy.experiment_id, row.lot_id, now))
                and resolve_lot(store, row.lot_id, now=now).status == "COMPLETE"):
            result.append(row)
    return tuple(sorted(result, key=lambda row: (row.recorded_at, row.lot_id)))
