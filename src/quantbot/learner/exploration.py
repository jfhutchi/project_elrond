"""Outcome-first PAPER exploration evidence, separate from predictive models.

No broker, promotion or consent API. Negative baseline scores are retained unchanged;
they neither authorize nor veto sampling. Execution still requires the retained risk,
protection, reconciliation and exact deployment gates.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field

from quantbot.learner.experiment_contract import DecisionRecord, Digest, ExperimentManifest
from quantbot.learner.experiment_observation import ObservationBatch, build_observation_batch
from quantbot.learner.five_minute import FiveMinuteFeatureRow
from quantbot.learner.forward_source import Snapshot
from quantbot.learner.models import Finite, LearnerModel, Text
from quantbot.research.manifest import content_id


class ExplorationPolicy(LearnerModel):
    schema_version: Literal["PAPER_EXPLORATION_V1"] = "PAPER_EXPLORATION_V1"
    experiment_id: Text
    manifest_hash: Digest
    recorded_at: datetime
    code_hash: Digest
    mode: Literal["PAPER"] = "PAPER"
    selection: Literal["ROUND_ROBIN_COMPLETED_LOTS"] = "ROUND_ROBIN_COMPLETED_LOTS"
    maximum_concurrent_positions: Literal[1] = 1
    maximum_position_bps: int = Field(default=50, gt=0, le=50, strict=True)
    maximum_entry_risk_bps: int = Field(default=25, gt=0, le=25, strict=True)
    stop_bps: Literal[100] = 100
    horizon_minutes: Literal[30] = 30
    forecast_gate_required: Literal[False] = False
    predictive_model: Literal[False] = False
    synthetic: bool = False

    @property
    def policy_id(self) -> str:
        return content_id(self)


class ExplorationObservation(LearnerModel):
    experiment_id: Text
    policy_id: Digest
    features: FiveMinuteFeatureRow
    recorded_at: datetime
    baseline_scores: dict[Digest, Finite]
    baseline_decision_ids: tuple[Digest, ...]
    selection_basis: Literal["OUTCOME_FIRST_PAPER_EXPLORATION"] = (
        "OUTCOME_FIRST_PAPER_EXPLORATION"
    )
    order_authority: Literal[False] = False
    synthetic: bool = False

    @property
    def prediction_id(self) -> str:
        """Compatibility key for retained idempotency; this is not a forecast."""
        return content_id(self)

    @property
    def decision_id(self) -> str:
        return self.prediction_id


def exploration_observations(
    manifest: ExperimentManifest,
    policy: ExplorationPolicy,
    batch: ObservationBatch,
    *,
    source: Snapshot | Mapping[str, Snapshot],
    baseline_decisions: Sequence[DecisionRecord],
    holdout_guard: Callable[[datetime, datetime], bool],
) -> tuple[ExplorationObservation, ...]:
    manifest = ExperimentManifest.model_validate(manifest.model_dump())
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    batch = ObservationBatch.model_validate(batch.model_dump())
    if (
        policy.manifest_hash != content_id(manifest)
        or policy.experiment_id != manifest.experiment_id
        or batch.experiment_id != manifest.experiment_id
        or policy.synthetic != manifest.synthetic
        or batch.synthetic != manifest.synthetic
        or policy.recorded_at > batch.recorded_at
    ):
        raise ValueError("EXACT_POINT_IN_TIME_EXPLORATION_POLICY_REQUIRED")
    if any(not holdout_guard(row.feature_at - timedelta(minutes=35), row.target_at)
           for row in batch.features):
        raise ValueError("PROTECTED_OBSERVATION_WINDOW")
    if isinstance(source, Snapshot):
        sources = {row.symbol: source for row in manifest.roster}
        rebuilt = build_observation_batch(manifest, source, now=batch.recorded_at)
    else:
        sources = dict(source)
        if set(sources) != {row.symbol for row in manifest.roster}:
            raise ValueError("COMPLETE_GENUINE_EXPLORATION_COHORT_REQUIRED")
        panels = {symbol: build_observation_batch(manifest, capture, now=batch.recorded_at)
                  for symbol, capture in sources.items()}
        coverage = tuple(row for identity in manifest.roster
                         for row in panels[identity.symbol].coverage
                         if row.symbol == identity.symbol)
        panel_features = tuple(row for identity in manifest.roster
                         for row in panels[identity.symbol].features
                         if row.symbol == identity.symbol)
        rebuilt = ObservationBatch(
            experiment_id=manifest.experiment_id, recorded_at=batch.recorded_at,
            snapshot_hash=content_id(sorted(row.source_hash for row in coverage)),
            coverage=coverage, features=panel_features, synthetic=manifest.synthetic,
        )
    if (
        not batch.features
        or {row.symbol for row in batch.features} != {row.symbol for row in manifest.roster}
        or len(batch.features) != len(manifest.roster)
        or len({(row.feature_at, row.target_at) for row in batch.features}) != 1
        or any(capture.query.timeframe != "5Min" or capture.query.feed.value != "iex"
               or capture.query.adjustment.value != "raw" for capture in sources.values())
        or rebuilt != batch
    ):
        raise ValueError("COMPLETE_GENUINE_EXPLORATION_COHORT_REQUIRED")
    features = {row.symbol: row for row in batch.features}
    scores: dict[str, dict[str, float]] = {symbol: {} for symbol in features}
    ids: dict[str, list[str]] = {symbol: [] for symbol in features}
    for item in baseline_decisions:
        decision = DecisionRecord.model_validate(item.model_dump())
        symbol = decision.features.symbol
        if (
            decision.experiment_id != manifest.experiment_id
            or decision.synthetic != manifest.synthetic
            or decision.features != features.get(symbol)
            or decision.recorded_at > batch.recorded_at
            or decision.model_id in scores[symbol]
        ):
            raise ValueError("EXACT_PRETRADE_BASELINE_EVIDENCE_REQUIRED")
        scores[symbol][decision.model_id] = decision.score
        ids[symbol].append(decision.decision_id)
    return tuple(ExplorationObservation(
        experiment_id=manifest.experiment_id, policy_id=policy.policy_id,
        features=row, recorded_at=batch.recorded_at,
        baseline_scores=scores[row.symbol], baseline_decision_ids=tuple(sorted(ids[row.symbol])),
        synthetic=manifest.synthetic,
    ) for row in batch.features)


def round_robin_observations(
    policy: ExplorationPolicy, observations: Sequence[ExplorationObservation], *,
    completed_lots: int,
) -> tuple[ExplorationObservation, ...]:
    """No daily quota, no ranking by realized or predicted profitability."""
    policy = ExplorationPolicy.model_validate(policy.model_dump())
    if type(completed_lots) is not int or completed_lots < 0:
        raise ValueError("NONNEGATIVE_COMPLETED_LOT_COUNT_REQUIRED")
    ordered = tuple(sorted(
        (ExplorationObservation.model_validate(row.model_dump()) for row in observations),
        key=lambda row: row.features.symbol,
    ))
    if (not ordered or any(row.policy_id != policy.policy_id for row in ordered)
            or len({row.features.symbol for row in ordered}) != len(ordered)):
        raise ValueError("EXACT_DISTINCT_EXPLORATION_COHORT_REQUIRED")
    offset = completed_lots % len(ordered)
    return ordered[offset:] + ordered[:offset]
