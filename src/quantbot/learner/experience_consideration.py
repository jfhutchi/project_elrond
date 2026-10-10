"""Idempotent descriptive experience reviews; no fitting or activation authority."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Literal

from quantbot.learner.experience_continuity import verified_policy_lineage
from quantbot.learner.experiment_lots import DeadlineBrokerClockEvidence, LotEvent, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration import ExplorationObservation, ExplorationPolicy
from quantbot.learner.exploration_feedback import (
    ExplorationFeedback,
    journal_exploration_feedback,
    verified_feedback,
)
from quantbot.learner.models import LearnerModel, Text
from quantbot.research.manifest import content_id
from quantbot.storage import SingleWriterLock


class ExperienceConsideration(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    lineage_root_id: Text
    lot_id: Text
    original_policy_id: str | None
    source_revision: Text
    feedback_id: str | None
    status: Literal["VERIFIED_DESCRIPTIVE", "EVIDENCE_INCOMPLETE"]
    net_pnl: Decimal | None
    synthetic: bool
    purpose: Literal["DESCRIPTIVE_EXPLORATION_REVIEW"] = "DESCRIPTIVE_EXPLORATION_REVIEW"
    grants_fit: Literal[False] = False
    grants_activation: Literal[False] = False

    @property
    def consideration_id(self) -> str:
        return content_id(self)


class ExperienceReview(LearnerModel):
    experiment_id: Text
    recorded_at: datetime
    policy_id: Text
    source_set_hash: Text
    verified_episodes: int
    incomplete_episodes: int
    status: Literal["NO_CHANGE"] = "NO_CHANGE"
    reason: Literal[
        "DESCRIPTIVE_FEEDBACK_NOT_FIT_AUTHORITY",
        "OWNED_OUTCOME_EVIDENCE_INCOMPLETE",
        "NO_ELIGIBLE_EXPERIENCE",
    ]
    synthetic: bool
    grants_fit: Literal[False] = False
    grants_activation: Literal[False] = False

    @property
    def review_id(self) -> str:
        return content_id(self)


def lineage_feedback(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> tuple[ExplorationFeedback, ...]:
    rows = tuple(
        row
        for ancestor in verified_policy_lineage(store, policy, now=now)
        for row in verified_feedback(store, ancestor, now=now)
    )
    counts = Counter(row.lot_id for row in rows)
    return tuple(
        sorted((row for row in rows if counts[row.lot_id] == 1), key=lambda row: row.lot_id)
    )


def _derive(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> tuple[ExperienceConsideration, ...]:
    lineage = verified_policy_lineage(store, policy, now=now)
    events = store.read_as_of(policy.experiment_id, now)
    observations = [
        e.decode(ExplorationObservation) for e in events if e.kind == "ExplorationObservation"
    ]
    lots = [e.decode(LotEvent) for e in events if e.kind == "LotEvent"]
    feedback = {row.lot_id: row for row in lineage_feedback(store, policy, now=now)}
    policies = {ancestor.policy_id for ancestor in lineage}
    result = []
    for lot_id in sorted({row.lot_id for row in lots if row.kind == "ENTRY_DECLARED"}):
        rows = [row for row in lots if row.lot_id == lot_id]
        declarations = [row for row in rows if row.kind == "ENTRY_DECLARED"]
        matches = [
            row
            for row in observations
            if any(row.prediction_id == declared.decision_id for declared in declarations)
        ]
        origin = (
            matches[0].policy_id
            if len(matches) == len(declarations) == 1 and matches[0].policy_id in policies
            else None
        )
        # Unrelated policy-owned episodes cannot silently become this lineage's experience.
        if len(matches) == 1 and matches[0].policy_id not in policies:
            continue
        outcome = resolve_lot(store, lot_id, now=now)
        evidence = [
            e.record_id
            for e in events
            if e.kind == "LotEvent"
            and e.decode(LotEvent).lot_id == lot_id
            or e.kind == "DeadlineBrokerClockEvidence"
            and e.decode(DeadlineBrokerClockEvidence).lot_id == lot_id
        ]
        valid = feedback.get(lot_id) if origin is not None else None
        revision = content_id(
            (
                sorted(evidence),
                [content_id(row) for row in matches],
                origin,
                outcome.model_dump(exclude={"resolved_at"}),
                content_id(valid) if valid else None,
            )
        )
        result.append(
            ExperienceConsideration(
                experiment_id=policy.experiment_id,
                recorded_at=now,
                lineage_root_id=lineage[0].policy_id,
                lot_id=lot_id,
                original_policy_id=origin,
                source_revision=revision,
                feedback_id=content_id(valid) if valid else None,
                status="VERIFIED_DESCRIPTIVE" if valid else "EVIDENCE_INCOMPLETE",
                net_pnl=valid.net_pnl if valid else None,
                synthetic=policy.synthetic,
            )
        )
    return tuple(result)


def current_considerations(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> tuple[ExperienceConsideration, ...]:
    derived = {(row.lot_id, row.source_revision): row for row in _derive(store, policy, now=now)}
    recorded = [
        e.decode(ExperienceConsideration)
        for e in store.read_as_of(policy.experiment_id, now)
        if e.kind == "ExperienceConsideration"
    ]
    counts = Counter((row.lineage_root_id, row.lot_id, row.source_revision) for row in recorded)
    policies = {p.policy_id: p for p in verified_policy_lineage(store, policy, now=now)}
    result = []
    for row in recorded:
        current = derived.get((row.lot_id, row.source_revision))
        ancestor = policies.get(row.original_policy_id or row.lineage_root_id)
        if (
            counts[row.lineage_root_id, row.lot_id, row.source_revision] != 1
            or current is None
            or ancestor is None
            or current.model_copy(update={"recorded_at": row.recorded_at}) != row
        ):
            continue
        try:
            historical = _derive(store, ancestor, now=row.recorded_at)
        except ValueError:
            continue
        if row in historical:
            result.append(row)
    return tuple(result)


def _consider_experience(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> ExperienceReview:
    lineage = verified_policy_lineage(store, policy, now=now)
    for ancestor in lineage:
        journal_exploration_feedback(store, ancestor, now=now)
    existing = [
        e.decode(ExperienceConsideration)
        for e in store.read_as_of(policy.experiment_id, now)
        if e.kind == "ExperienceConsideration"
    ]
    for row in _derive(store, policy, now=now):
        if not any(
            (old.lineage_root_id, old.lot_id, old.source_revision)
            == (row.lineage_root_id, row.lot_id, row.source_revision)
            for old in existing
        ):
            store.append(
                row,
                idempotency_key="experience-consideration:"
                + content_id((row.lineage_root_id, row.lot_id, row.source_revision, row.purpose)),
            )
    current = current_considerations(store, policy, now=now)
    verified = sum(row.status == "VERIFIED_DESCRIPTIVE" for row in current)
    expected = _derive(store, policy, now=now)
    incomplete = len(expected) - verified
    source_hash = content_id(
        (
            policy.policy_id,
            [
                content_id(row.model_copy(update={"recorded_at": policy.recorded_at}))
                for row in expected
            ],
            sorted(row.consideration_id for row in current),
        )
    )
    review = ExperienceReview(
        experiment_id=policy.experiment_id,
        recorded_at=now,
        policy_id=policy.policy_id,
        source_set_hash=source_hash,
        verified_episodes=verified,
        incomplete_episodes=incomplete,
        reason=(
            "OWNED_OUTCOME_EVIDENCE_INCOMPLETE"
            if incomplete
            else "DESCRIPTIVE_FEEDBACK_NOT_FIT_AUTHORITY"
            if verified
            else "NO_ELIGIBLE_EXPERIENCE"
        ),
        synthetic=policy.synthetic,
    )
    prior = [
        e.decode(ExperienceReview)
        for e in store.read_as_of(policy.experiment_id, now)
        if e.kind == "ExperienceReview"
        and e.decode(ExperienceReview).source_set_hash == source_hash
    ]
    if prior:
        if (
            len(prior) != 1
            or review.model_copy(update={"recorded_at": prior[0].recorded_at}) != prior[0]
        ):
            raise ValueError("INCONSISTENT_EXPERIENCE_REVIEW")
        if any(row.recorded_at > prior[0].recorded_at for row in current):
            raise ValueError("BACKDATED_EXPERIENCE_REVIEW")
        historical_lineage = verified_policy_lineage(store, policy, now=prior[0].recorded_at)
        if historical_lineage != lineage:
            raise ValueError("BACKDATED_EXPERIENCE_REVIEW_LINEAGE")
        return prior[0]
    store.append(review, idempotency_key="experience-review:" + source_hash)
    return review


def consider_experience(
    store: ExperimentStore,
    policy: ExplorationPolicy,
    *,
    now: datetime,
) -> ExperienceReview:
    """One writer reviews a store; a competing wake fails before any journal writes."""
    with SingleWriterLock(store.path.with_name(store.path.name + ".experience.lock")):
        return _consider_experience(store, policy, now=now)
