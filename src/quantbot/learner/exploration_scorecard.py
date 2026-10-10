"""Descriptive owned PAPER outcomes; no fit, adoption or execution authority.

Actual fill prices already include execution-price effects. Fees are verified by
the owned-lot resolver; modeled costs must not be subtracted again. A favorable
sample does not establish causal improvement or statistical edge.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal

from quantbot.learner.experiment_contract import Digest, ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration import ExplorationPolicy
from quantbot.learner.exploration_feedback import ExplorationFeedback, verified_feedback
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id


class ExplorationScorecard(LearnerModel):
    experiment_id: Text
    policy_id: Digest
    recorded_at: datetime
    recorded_feedback: int
    verified_episodes: int
    excluded_feedback: int
    profitable_episodes: int
    losing_episodes: int
    flat_episodes: int
    profitable_episode_percent: Decimal | None
    net_pnl: Decimal | None
    verified_fees: Decimal | None
    expectancy_usd: Decimal | None
    mean_net_return: Decimal | None
    profit_factor: Decimal | None
    evidence_class: Literal["ACTUAL_PAPER_OWNED_FILLS", "SYNTHETIC_TEST_ONLY"]
    learning_effect: Literal["ROUND_ROBIN_SELECTION_COUNT_ONLY"] = (
        "ROUND_ROBIN_SELECTION_COUNT_ONLY"
    )
    improvement_established: Literal[False] = False
    predictor_fit: Literal[False] = False
    grants_execution: Literal[False] = False
    synthetic: bool


def exploration_scorecard(
    store: ExperimentStore, policy: ExplorationPolicy, *, now: datetime,
) -> ExplorationScorecard:
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    if now.tzinfo is None or now.utcoffset() is None or now < policy.recorded_at:
        raise ValueError("ACTUAL_AS_OF_EVALUATION_TIME_REQUIRED")
    events = store.read_as_of(policy.experiment_id, now)
    manifests = [event.decode(ExperimentManifest) for event in events
                 if event.kind == "ExperimentManifest"]
    if (len(manifests) != 1 or content_id(manifests[0]) != policy.manifest_hash
            or manifests[0].synthetic != policy.synthetic
            or not any(event.kind == "ExplorationPolicy"
                       and event.decode(ExplorationPolicy) == policy for event in events)):
        raise ValueError("IMMUTABLE_EXPLORATION_POLICY_REQUIRED")
    recorded = [event.decode(ExplorationFeedback) for event in events
                if event.kind == "ExplorationFeedback"
                and event.decode(ExplorationFeedback).policy_id == policy.policy_id]
    counts = Counter(row.lot_id for row in recorded)
    verified = []
    for row in verified_feedback(store, policy, now=now):
        outcome = resolve_lot(store, row.lot_id, now=now)
        notional = sum((fill.quantity * fill.price for event in events
                        if event.kind == "LotEvent" for fill in (event.decode(LotEvent),)
                        if fill.lot_id == row.lot_id and fill.kind == "FILL"
                        and fill.side == "BUY" and fill.price is not None), Decimal(0))
        if (counts[row.lot_id] == 1 and row.synthetic == policy.synthetic
                and outcome.synthetic == policy.synthetic and row.net_pnl == outcome.net_pnl
                and row.actual_fees == outcome.fees and notional > 0
                and row.actual_net_return == row.net_pnl / notional):
            verified.append(row)
    wins = sum(row.net_pnl > 0 for row in verified)
    losses = sum(row.net_pnl < 0 for row in verified)
    count = len(verified)
    net = sum((row.net_pnl for row in verified), Decimal(0)) if count else None
    loss_sum = -sum((row.net_pnl for row in verified if row.net_pnl < 0), Decimal(0))
    gain_sum = sum((row.net_pnl for row in verified if row.net_pnl > 0), Decimal(0))
    return ExplorationScorecard(
        experiment_id=policy.experiment_id, policy_id=policy.policy_id, recorded_at=now,
        recorded_feedback=len(recorded), verified_episodes=count,
        excluded_feedback=len(recorded) - count,
        profitable_episodes=wins, losing_episodes=losses, flat_episodes=count - wins - losses,
        profitable_episode_percent=Decimal(wins) * 100 / count if count else None,
        net_pnl=net,
        verified_fees=sum((row.actual_fees for row in verified), Decimal(0)) if count else None,
        expectancy_usd=net / count if net is not None else None,
        mean_net_return=sum((row.actual_net_return for row in verified), Decimal(0)) / count
        if count else None,
        profit_factor=gain_sum / loss_sum if loss_sum > 0 else None,
        evidence_class="SYNTHETIC_TEST_ONLY" if policy.synthetic else "ACTUAL_PAPER_OWNED_FILLS",
        synthetic=policy.synthetic,
    )
