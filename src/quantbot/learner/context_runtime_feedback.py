"""Bounded bridge from retained PAPER decisions/outcomes to the existing learner journal.

Market returns are outcome facts, never semantic reading labels or activation authority.
The caller supplies current ownership/reconciliation objections from its bounded core read.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Self

from pydantic import Field, model_validator

from quantbot.learner.context_feedback import DownstreamExample, ReadingReview, SourceInvalidation
from quantbot.learner.context_integration import ContextInvalidationRecord, ContextSnapshotRecord
from quantbot.learner.experiment_store import ExperimentStore, StoredEvent
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.models import LearnerModel
from quantbot.paper_core.context_integration import (
    ContextCapture,
    load_context_capture,
    load_source_permissions,
    retained_context_documents,
)
from quantbot.paper_core.contracts import Decision, Observation, RoundTrip
from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.external_context import (
    ContextSnapshot,
    SourcePermissions,
    aware,
    build_context_snapshot,
)
from quantbot.research.manifest import content_id

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from quantbot.paper_core.store import CoreStore

FEEDBACK_EVENT_KINDS = (
    "ContextSnapshotRecord",
    "ContextOutcomeFeedback",
    "ContextInvalidationRecord",
    "ReadingReview",
    "ContextModeledOutcomeLabel",
    "ContextOutcomeEligibility",
)


class ModeledCostContract(LearnerModel):
    recorded_at: datetime
    source_policy_refs: tuple[str, ...] = Field(min_length=1)
    stock_round_trip_bps: Literal[10] = 10
    crypto_round_trip_bps: Literal[50] = 50
    interpretation: Literal["RESEARCH_ESTIMATE_NOT_ACTUAL_FEE"] = "RESEARCH_ESTIMATE_NOT_ACTUAL_FEE"

    @property
    def contract_id(self) -> str:
        return content_id(self)


class ContextModeledOutcomeLabel(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    feedback_id: str
    contract: ModeledCostContract
    round_trip_cost_bps: Decimal
    gross_return_bps: Decimal
    net_return_bps: Decimal
    cost_basis: Literal["MODELED_NET"] = "MODELED_NET"
    grants_activation: Literal[False] = False

    @model_validator(mode="after")
    def declared_cost_math(self) -> Self:
        if (
            self.contract.recorded_at > self.recorded_at
            or self.round_trip_cost_bps
            not in {self.contract.stock_round_trip_bps, self.contract.crypto_round_trip_bps}
            or self.net_return_bps != self.gross_return_bps - self.round_trip_cost_bps
        ):
            raise ValueError("EXACT_DECLARED_MODELED_COST_ARITHMETIC_REQUIRED")
        return self


class ContextOutcomeFeedback(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    core_store_id: str = Field(min_length=1)
    decision_id: str
    decision_at: datetime
    observation_id: str
    snapshot_id: str
    context_at: datetime
    context_use: Literal["OBSERVED_ONLY", "USED"]
    outcome_id: str
    outcome_revision: int = Field(ge=1)
    supersedes_outcome_id: str | None
    lot_id: str
    label_available_at: datetime
    gross_return_bps: Decimal
    actual_net_return_bps: Decimal | None
    modeled_net_return_bps: Decimal | None = None
    cost_status: Literal["COSTS_PENDING", "COMPLETE"]
    cost_reason: str
    cost_evidence_ids: tuple[str, ...]
    source_state_hash: str
    lineage_refs: tuple[str, ...]
    reading_label: None = None
    grants_activation: Literal[False] = False

    @property
    def feedback_id(self) -> str:
        return content_id(self)


class ContextFeedbackReport(LearnerModel):
    experiment_id: str
    as_of: datetime
    decisions_considered: int
    snapshots_written: int
    feedback_written: int
    modeled_labels_written: int = 0
    actual_net_eligible: int
    gate_counts: tuple[tuple[str, int], ...]
    exclusions: tuple[tuple[str, str], ...]
    grants_activation: Literal[False] = False


class ContextOutcomeEligibility(LearnerModel):
    experiment_id: str
    recorded_at: datetime
    core_store_id: str
    lot_id: str
    outcome_id: str
    outcome_revision: int = Field(ge=1)
    objections: tuple[str, ...]
    grants_activation: Literal[False] = False


def _eligibility_states(
    events: Sequence[StoredEvent],
) -> dict[tuple[str, str], ContextOutcomeEligibility]:
    return {
        (row.core_store_id, row.lot_id): row
        for event in events
        if event.kind == "ContextOutcomeEligibility"
        for row in (event.decode(ContextOutcomeEligibility),)
    }


def _record_eligibility(
    experiments: ExperimentStore,
    states: dict[tuple[str, str], ContextOutcomeEligibility],
    *,
    experiment_id: str,
    core_store_id: str,
    lot_id: str,
    outcome_id: str,
    outcome_revision: int,
    objections: tuple[str, ...],
    as_of: datetime,
) -> bool:
    """An older or conflicting revision can never clear a newer suspension."""
    key = (core_store_id, lot_id)
    previous = states.get(key)
    if previous is not None and (
        outcome_revision < previous.outcome_revision
        or (outcome_revision == previous.outcome_revision and outcome_id != previous.outcome_id)
        or as_of < previous.recorded_at
    ):
        return False
    if previous is None and not objections:
        return True
    record = ContextOutcomeEligibility(
        experiment_id=experiment_id,
        recorded_at=as_of,
        core_store_id=core_store_id,
        lot_id=lot_id,
        outcome_id=outcome_id,
        outcome_revision=outcome_revision,
        objections=tuple(sorted(set(objections))),
    )
    if previous is None or previous.model_dump(exclude={"recorded_at"}) != record.model_dump(
        exclude={"recorded_at"}
    ):
        experiments.append(
            record, idempotency_key="context-outcome-eligibility:" + content_id(record)
        )
        states[key] = record
    return not record.objections


def _source_gate(
    research: DiscoveryStore,
    capture: ContextCapture,
    permissions: Mapping[str, SourcePermissions],
    revoked: set[str],
    at: datetime,
) -> str | None:
    refs = {capture.snapshot.snapshot_id}
    for binding in capture.bindings:
        refs.update((binding.document_id, binding.source_id, content_id(binding.permissions)))
        refs.update(binding.permissions.evidence_refs)
    if refs & revoked:
        return "SOURCE_INVALIDATED"
    for binding in capture.bindings:
        current = permissions.get(binding.permissions.provider)
        if (
            current is None
            or current.checked_at > at
            or any(
                getattr(current, field) != "VERIFIED"
                for field in ("access", "metadata_retention", "processing")
            )
        ):
            return "SOURCE_PERMISSION_UNVERIFIED"
        if {content_id(current), *current.evidence_refs} & revoked:
            return "SOURCE_INVALIDATED"
    try:
        documents = retained_context_documents(research, capture)
        if {row.document_id for row in documents} != {
            row.document_id for row in capture.bindings if row.kind == "NEWS_RESEARCH_V1"
        }:
            return "SOURCE_DOCUMENT_UNAVAILABLE"
    except (ValueError, KeyError):
        return "SOURCE_DOCUMENT_UNAVAILABLE"
    return None


def revalidate_retained_outcomes(
    store: CoreStore,
    experiments: ExperimentStore,
    *,
    experiment_id: str,
    feedback_ids: set[str],
    as_of: datetime,
    session: Session | None = None,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Recheck only the declared sample against the current existing canonical ledger."""
    from sqlalchemy import select, text

    from quantbot.paper_core.outcomes import OutcomeIncomplete, _resolve, quarantined_lots
    from quantbot.paper_core.store import table
    from quantbot.storage import StateConflictError

    as_of = aware(as_of)
    if len(feedback_ids) > 200:
        raise ValueError("BOUNDED_CURRENT_OUTCOME_SELECTION_REQUIRED")
    events = experiments.read_as_of(
        experiment_id,
        as_of,
        maximum_records=4000,
        kinds=("ContextOutcomeFeedback", "ContextOutcomeEligibility"),
    )
    selected = {
        row.feedback_id: row
        for event in events
        if event.kind == "ContextOutcomeFeedback"
        for row in (event.decode(ContextOutcomeFeedback),)
        if row.feedback_id in feedback_ids
    }
    if set(selected) != feedback_ids:
        raise ValueError("EXACT_SELECTED_OUTCOME_FEEDBACK_REQUIRED")
    if any(row.core_store_id != store.database.store_identity for row in selected.values()):
        raise ValueError("CONTEXT_CANONICAL_OUTCOME_STORE_MISMATCH")
    eligibility = _eligibility_states(events)
    result = []
    checks = []
    owns_transaction = session is None
    with nullcontext(session) if session is not None else store.database.transaction() as session:
        if owns_transaction:
            session.execute(text("BEGIN"))
        quarantined = quarantined_lots(session)
        pointer, work = table("core_outcome_index"), table("core_outcome_work")
        for identity, row in selected.items():
            reasons = []
            current = (
                session.execute(select(pointer).where(pointer.c.lot_id == row.lot_id))
                .mappings()
                .one_or_none()
            )
            pending = (
                session.execute(select(work).where(work.c.lot_id == row.lot_id))
                .mappings()
                .one_or_none()
            )
            outcome_id, revision = row.outcome_id, row.outcome_revision
            if (
                current is None
                or current["outcome_id"] != row.outcome_id
                or current["status"] not in {"COMPLETE", "COSTS_PENDING"}
            ):
                reasons.append("CONTEXT_OUTCOME_NOT_CURRENT")
                if current is not None:
                    encoded = session.execute(
                        select(table("core_outcome_revisions").c.payload).where(
                            table("core_outcome_revisions").c.id == current["outcome_id"]
                        )
                    ).scalar_one_or_none()
                    if encoded:
                        newest = RoundTrip.model_validate_json(encoded)
                        outcome_id, revision = newest.id, newest.revision
            if pending is None or pending["version"] > pending["processed_version"]:
                reasons.append("CONTEXT_OUTCOME_CORRECTION_PENDING")
            if row.lot_id in quarantined:
                reasons.append("CONTEXT_OUTCOME_QUARANTINED")
            try:
                resolved = _resolve(store, session, row.lot_id, as_of)
                if resolved.id != row.outcome_id:
                    reasons.append("CONTEXT_OUTCOME_SUPERSEDED")
            except (ValueError, StateConflictError, OutcomeIncomplete):
                reasons.append("CONTEXT_OUTCOME_UNVERIFIED")
            checks.append((identity, row, outcome_id, revision, tuple(reasons)))
    for identity, row, outcome_id, revision, checked_reasons in checks:
        verified = _record_eligibility(
            experiments,
            eligibility,
            experiment_id=experiment_id,
            core_store_id=row.core_store_id,
            lot_id=row.lot_id,
            outcome_id=outcome_id,
            outcome_revision=revision,
            objections=checked_reasons,
            as_of=as_of,
        )
        if not verified and not checked_reasons:
            checked_reasons = ("OUTCOME_ELIGIBILITY_SUSPENDED",)
        if checked_reasons:
            result.append((identity, checked_reasons))
    return tuple(result)


def journal_runtime_context_feedback(
    experiments: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    core_store_id: str,
    decisions: Sequence[Decision],
    observations: Sequence[Observation],
    round_trips: Sequence[RoundTrip],
    outcome_objections: Mapping[str, tuple[str, ...]],
    as_of: datetime,
    modeled_cost_contract: ModeledCostContract | None = None,
) -> ContextFeedbackReport:
    """Journal real causal joins once; report unavailable gates without receipt churn.

    Every outcome ID requires an explicit objections entry. Empty means the caller proved
    current owned/reconciled/nonquarantined evidence with no pending correction. This API
    does not read an operational database, infer those facts, fit, or promote anything.
    """
    as_of = aware(as_of)
    if not core_store_id or max(len(decisions), len(observations), len(round_trips)) > 200:
        raise ValueError("BOUNDED_IDENTIFIED_CORE_FEEDBACK_REQUIRED")
    decisions = tuple(Decision.model_validate(row.model_dump()) for row in decisions)
    observations = tuple(Observation.model_validate(row.model_dump()) for row in observations)
    round_trips = tuple(RoundTrip.model_validate(row.model_dump()) for row in round_trips)
    events = experiments.read_as_of(
        experiment_id, as_of, maximum_records=4000, kinds=FEEDBACK_EVENT_KINDS
    )
    snapshots = {
        e.decode(ContextSnapshotRecord).snapshot_id
        for e in events
        if e.kind == ContextSnapshotRecord.__name__
    }
    previous = [
        e.decode(ContextOutcomeFeedback)
        for e in events
        if e.kind == ContextOutcomeFeedback.__name__
    ]
    existing = {(r.core_store_id, r.outcome_id) for r in previous}
    eligibility = _eligibility_states(events)
    # Objections are durable even when a context/source join later fails.
    for trip in round_trips:
        objections = outcome_objections.get(trip.id, ("OUTCOME_ELIGIBILITY_UNVERIFIED",))
        known = [
            r for r in previous if r.core_store_id == core_store_id and r.lot_id == trip.lot_id
        ]
        if (
            objections
            and known
            and trip.recorded_at_utc <= as_of
            and trip.revision >= max(r.outcome_revision for r in known)
        ):
            _record_eligibility(
                experiments,
                eligibility,
                experiment_id=experiment_id,
                core_store_id=core_store_id,
                lot_id=trip.lot_id,
                outcome_id=trip.id,
                outcome_revision=trip.revision,
                objections=objections,
                as_of=as_of,
            )
    revoked = {
        e.decode(ContextInvalidationRecord).invalidation.reference_id
        for e in events
        if e.kind == ContextInvalidationRecord.__name__
        and e.decode(ContextInvalidationRecord).invalidation.invalidated_at <= as_of
    }
    permissions = load_source_permissions(research)
    by_observation = {row.id: row for row in observations}
    gates: Counter[str] = Counter()
    exclusions: list[tuple[str, str]] = []
    snapshot_writes = feedback_writes = net_eligible = modeled_writes = 0
    modeled_keys = {
        (
            e.decode(ContextModeledOutcomeLabel).feedback_id,
            e.decode(ContextModeledOutcomeLabel).contract.contract_id,
        )
        for e in events
        if e.kind == ContextModeledOutcomeLabel.__name__
    }
    if modeled_cost_contract is not None:
        modeled_cost_contract = ModeledCostContract.model_validate(
            modeled_cost_contract.model_dump()
        )
        if modeled_cost_contract.recorded_at > as_of:
            raise ValueError("MODELED_COST_CONTRACT_UNAVAILABLE")

    def label_modeled(record: ContextOutcomeFeedback, observation: Observation) -> int:
        contract = modeled_cost_contract
        if contract is None or (record.feedback_id, contract.contract_id) in modeled_keys:
            return 0
        crypto = (
            observation.crypto_session is not None
            or (
                observation.asset_eligibility is not None
                and observation.asset_eligibility.asset_class == "crypto"
                and observation.asset_eligibility.symbol == observation.symbol
            )
            or (
                observation.asset_session is not None
                and observation.asset_session.calendar == "CRYPTO24"
                and observation.asset_session.feed == "crypto"
                and observation.asset_session.symbol == observation.symbol
            )
        )
        amount = Decimal(
            contract.crypto_round_trip_bps if crypto else contract.stock_round_trip_bps
        )
        label = ContextModeledOutcomeLabel(
            experiment_id=experiment_id,
            recorded_at=as_of,
            feedback_id=record.feedback_id,
            contract=contract,
            round_trip_cost_bps=amount,
            gross_return_bps=record.gross_return_bps,
            net_return_bps=record.gross_return_bps - amount,
        )
        wrote = experiments.append(
            label,
            idempotency_key="context-modeled-label:"
            + content_id((record.feedback_id, contract.contract_id)),
        )
        modeled_keys.add((record.feedback_id, contract.contract_id))
        return int(wrote)

    def exclude(identity: str, reason: str) -> None:
        gates[reason] += 1
        exclusions.append((identity, reason))

    for decision in decisions:
        observation = by_observation.get(decision.observation_id)
        snapshot_id = decision.context_snapshot_id
        if observation is None or not snapshot_id or observation.context_snapshot_id != snapshot_id:
            exclude(decision.id, "DECISION_CONTEXT_LINK_UNAVAILABLE")
            continue
        try:
            capture = load_context_capture(research, snapshot_id)
        except (ValueError, KeyError):
            exclude(decision.id, "CONTEXT_CAPTURE_UNAVAILABLE")
            continue
        snapshot = capture.snapshot
        if (
            decision.recorded_at_utc > as_of
            or snapshot.at > decision.recorded_at_utc
            or observation.available_at > decision.recorded_at_utc
            or observation.recorded_at_utc > decision.recorded_at_utc
        ):
            exclude(decision.id, "CONTEXT_OR_PRICE_UNAVAILABLE_AT_DECISION")
            continue
        if (
            snapshot.symbol != decision.symbol
            or observation.symbol != decision.symbol
            or observation.evidence_lane != decision.evidence_lane
        ):
            exclude(decision.id, "DECISION_CONTEXT_IDENTITY_MISMATCH")
            continue
        reason = _source_gate(research, capture, permissions, revoked, as_of)
        if reason:
            exclude(decision.id, reason)
            continue
        if snapshot_id not in snapshots:
            if snapshot.used_document_ids:
                exclude(decision.id, "USED_CONTEXT_REQUIRES_CANDIDATE_JOURNAL_CONTRACT")
                continue
            snapshot_writes += experiments.append(
                ContextSnapshotRecord(
                    experiment_id=experiment_id, recorded_at=as_of, snapshot_id=snapshot_id
                ),
                idempotency_key="context-snapshot:" + snapshot_id,
            )
            snapshots.add(snapshot_id)
        if decision.action != "BUY":
            exclude(decision.id, "WAIT_NO_EXECUTED_OUTCOME")
            continue
        trips = [r for r in round_trips if r.decision_id == decision.id]
        if not trips:
            exclude(decision.id, "COMPLETED_OUTCOME_UNAVAILABLE")
        for trip in trips:
            if (
                trip.available_at is None
                or not decision.recorded_at_utc < trip.available_at <= as_of
                or trip.recorded_at_utc > as_of
                or trip.source_end is None
                or not decision.recorded_at_utc < trip.source_end <= trip.available_at
            ):
                exclude(trip.id, "OUTCOME_UNAVAILABLE_AT_CUTOFF")
                continue
            if (
                trip.evidence_lane != decision.evidence_lane
                or not trip.source_state_hash
                or trip.entry_notional <= 0
                or not trip.reconciliation_id
                or not trip.protection_id
                or not trip.terminal_order_ids
            ):
                exclude(trip.id, "OUTCOME_PROVENANCE_INCOMPLETE")
                continue
            objections = outcome_objections.get(trip.id, ("OUTCOME_ELIGIBILITY_UNVERIFIED",))
            if objections:
                for reason in objections:
                    exclude(trip.id, reason)
                continue
            if {
                trip.id,
                decision.id,
                observation.id,
                trip.reconciliation_id,
                trip.protection_id,
                *trip.entry_fill_ids,
                *trip.exit_fill_ids,
                *trip.cost_item_ids,
            } & revoked:
                exclude(trip.id, "SOURCE_INVALIDATED")
                continue
            if any(
                r.lot_id == trip.lot_id
                and r.revision > trip.revision
                and r.recorded_at_utc <= as_of
                for r in trips
            ):
                exclude(trip.id, "OUTCOME_SUPERSEDED")
                continue
            if trip.revision > 1 and trip.supersedes not in {
                *(r.id for r in round_trips),
                *(r.outcome_id for r in previous if r.core_store_id == core_store_id),
            }:
                exclude(trip.id, "OUTCOME_REVISION_LINEAGE_UNAVAILABLE")
                continue
            if not _record_eligibility(
                experiments,
                eligibility,
                experiment_id=experiment_id,
                core_store_id=core_store_id,
                lot_id=trip.lot_id,
                outcome_id=trip.id,
                outcome_revision=trip.revision,
                objections=(),
                as_of=as_of,
            ):
                exclude(trip.id, "OUTCOME_ELIGIBILITY_SUSPENDED")
                continue
            for old in previous:
                if (
                    old.core_store_id != core_store_id
                    or old.lot_id != trip.lot_id
                    or old.outcome_revision >= trip.revision
                ):
                    continue
                retired = {old.feedback_id, old.outcome_id}
                retired.update(
                    content_id(e.decode(ContextModeledOutcomeLabel))
                    for e in events
                    if e.kind == ContextModeledOutcomeLabel.__name__
                    and e.decode(ContextModeledOutcomeLabel).feedback_id == old.feedback_id
                )
                for reference in sorted(retired - revoked):
                    experiments.append(
                        ContextInvalidationRecord(
                            experiment_id=experiment_id,
                            recorded_at=as_of,
                            invalidation=SourceInvalidation(
                                reference_id=reference,
                                invalidated_at=as_of,
                                reason="EVIDENCE_SUPERSEDED",
                            ),
                        ),
                        idempotency_key="context-superseded:" + content_id((reference, trip.id)),
                    )
                    revoked.add(reference)
            net = (
                trip.net_pnl / trip.entry_notional * 10000
                if trip.status == "COMPLETE"
                and trip.net_pnl is not None
                and trip.actual_fees is not None
                and trip.actual_fees >= 0
                and trip.cost_item_ids
                and trip.net_pnl == trip.gross_pnl - trip.actual_fees
                else None
            )
            modeled_net = (
                trip.modeled_net_pnl / trip.entry_notional * 10000
                if trip.modeled_net_pnl is not None
                and trip.modeled_costs is not None
                and trip.modeled_costs >= 0
                and trip.cost_item_ids
                and trip.modeled_net_pnl == trip.gross_pnl - trip.modeled_costs
                else None
            )
            training = all(
                permissions[b.permissions.provider].training == "VERIFIED" for b in capture.bindings
            )
            if net is None:
                gates["ACTUAL_NET_UNAVAILABLE"] += 1
            elif not training:
                gates["TRAINING_PERMISSION_UNVERIFIED"] += 1
            else:
                net_eligible += 1
            if (core_store_id, trip.id) in existing:
                old = next(
                    r
                    for r in previous
                    if r.core_store_id == core_store_id and r.outcome_id == trip.id
                )
                modeled_writes += label_modeled(old, observation)
                continue
            lineage = {
                core_store_id,
                decision.id,
                observation.id,
                snapshot_id,
                trip.id,
                trip.source_state_hash,
                trip.reconciliation_id,
                trip.protection_id,
                *trip.entry_fill_ids,
                *trip.exit_fill_ids,
                *trip.terminal_order_ids,
                *trip.cost_item_ids,
            }
            for binding in capture.bindings:
                lineage.update(
                    (
                        binding.document_id,
                        binding.source_id,
                        content_id(binding.permissions),
                        *binding.permissions.evidence_refs,
                    )
                )
            record = ContextOutcomeFeedback(
                experiment_id=experiment_id,
                recorded_at=as_of,
                core_store_id=core_store_id,
                decision_id=decision.id,
                decision_at=decision.recorded_at_utc,
                observation_id=observation.id,
                snapshot_id=snapshot_id,
                context_at=snapshot.at,
                context_use="USED" if snapshot.used_document_ids else "OBSERVED_ONLY",
                outcome_id=trip.id,
                outcome_revision=trip.revision,
                supersedes_outcome_id=trip.supersedes,
                lot_id=trip.lot_id,
                label_available_at=trip.available_at,
                gross_return_bps=trip.gross_pnl / trip.entry_notional * 10000,
                actual_net_return_bps=net,
                cost_status=trip.status,
                cost_reason=trip.cost_reason,
                modeled_net_return_bps=modeled_net,
                cost_evidence_ids=trip.cost_item_ids,
                source_state_hash=trip.source_state_hash,
                lineage_refs=tuple(sorted(lineage)),
            )
            feedback_writes += experiments.append(
                record, idempotency_key="context-outcome:" + content_id((core_store_id, trip.id))
            )
            existing.add((core_store_id, trip.id))
            previous.append(record)
            modeled_writes += label_modeled(record, observation)
    return ContextFeedbackReport(
        experiment_id=experiment_id,
        as_of=as_of,
        decisions_considered=len(decisions),
        snapshots_written=snapshot_writes,
        feedback_written=feedback_writes,
        modeled_labels_written=modeled_writes,
        actual_net_eligible=net_eligible,
        gate_counts=tuple(sorted(gates.items())),
        exclusions=tuple(exclusions),
    )


def load_runtime_context_feedback(
    experiments: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    as_of: datetime,
    include_suspended: bool = False,
) -> tuple[tuple[ContextOutcomeFeedback, ...], tuple[tuple[str, str], ...]]:
    """Read immutable facts, excluding revoked sources and superseded outcomes today.

    These are observed-context outcomes, not fit-ready DownstreamExamples or reading labels.
    Fitting still requires canonical admission and independent source/time split validation.
    """
    as_of = aware(as_of)
    events = experiments.read_as_of(
        experiment_id, as_of, maximum_records=4000, kinds=FEEDBACK_EVENT_KINDS
    )
    rows = [
        e.decode(ContextOutcomeFeedback)
        for e in events
        if e.kind == ContextOutcomeFeedback.__name__
    ]
    revoked = {
        e.decode(ContextInvalidationRecord).invalidation.reference_id
        for e in events
        if e.kind == ContextInvalidationRecord.__name__
        and e.decode(ContextInvalidationRecord).invalidation.invalidated_at <= as_of
    }
    permissions = load_source_permissions(research)
    eligibility = _eligibility_states(events)
    retained, excluded = [], []
    for row in rows:
        reason = "SOURCE_INVALIDATED" if set(row.lineage_refs) & revoked else None
        state = eligibility.get((row.core_store_id, row.lot_id))
        if reason is None and state is not None and state.objections:
            reason = "OUTCOME_ELIGIBILITY_SUSPENDED"
        if reason is None and any(
            other.core_store_id == row.core_store_id
            and other.lot_id == row.lot_id
            and other.outcome_revision > row.outcome_revision
            for other in rows
        ):
            reason = "OUTCOME_SUPERSEDED"
        if reason is None:
            try:
                capture = load_context_capture(research, row.snapshot_id)
                reason = _source_gate(research, capture, permissions, revoked, as_of)
            except (ValueError, KeyError):
                reason = "CONTEXT_CAPTURE_UNAVAILABLE"
        if reason:
            excluded.append((row.feedback_id, reason))
            if include_suspended and reason == "OUTCOME_ELIGIBILITY_SUSPENDED":
                retained.append(row)
        else:
            retained.append(row)
    return tuple(retained), tuple(excluded)


def journal_reading_review(
    experiments: ExperimentStore,
    research: DiscoveryStore,
    review: ReadingReview,
    *,
    snapshot_id: str,
    as_of: datetime,
) -> bool:
    """Ingest an explicit human/source correction; never manufacture it from an outcome.

    ReviewEvidence is the reviewer's independent evidence declaration. This function checks
    its contract, source linkage and availability; it does not authenticate external truth.
    Journal availability is the actual import time, never a backdated external review time.
    """
    review = ReadingReview.model_validate(review.model_dump())
    as_of = aware(as_of)
    if review.recorded_at > as_of:
        raise ValueError("REVIEW_UNAVAILABLE_AT_CUTOFF")
    capture = load_context_capture(research, snapshot_id)
    if not capture.snapshot.at <= review.reviewed_at or review.reading_id not in {
        row.reading_id for row in capture.snapshot.readings
    }:
        raise ValueError("REVIEW_REQUIRES_EXACT_AVAILABLE_READING")
    events = experiments.read_as_of(
        review.experiment_id, as_of, maximum_records=4000, kinds=FEEDBACK_EVENT_KINDS
    )
    if not any(
        e.kind == ContextSnapshotRecord.__name__
        and e.decode(ContextSnapshotRecord).snapshot_id == snapshot_id
        for e in events
    ):
        raise ValueError("REVIEW_REQUIRES_JOURNALED_SNAPSHOT")
    revoked = {
        e.decode(ContextInvalidationRecord).invalidation.reference_id
        for e in events
        if e.kind == ContextInvalidationRecord.__name__
        and e.decode(ContextInvalidationRecord).invalidation.invalidated_at <= as_of
    }
    reason = _source_gate(research, capture, load_source_permissions(research), revoked, as_of)
    if reason or {row.reference_id for row in review.evidence} & revoked:
        raise ValueError(reason or "REVIEW_EVIDENCE_INVALIDATED")
    identity = content_id(review.model_dump(exclude={"recorded_at"}))
    for event in events:
        if (
            event.kind == ReadingReview.__name__
            and content_id(event.decode(ReadingReview).model_dump(exclude={"recorded_at"}))
            == identity
        ):
            return False
    imported = ReadingReview.model_validate(review.model_dump() | {"recorded_at": as_of})
    return experiments.append(imported, idempotency_key="context-reading-review:" + identity)


class ContextShadowConsumption(LearnerModel):
    observation_id: str = Field(min_length=1)
    original_snapshot_id: str = Field(min_length=1)
    consumed_at: datetime
    recorded_at: datetime
    shadow: ContextSnapshot
    candidate_id: str | None = None
    candidate_feature_values: tuple[float, ...] | None = None
    reading_predictions: tuple[tuple[str, float], ...] = ()
    scope: Literal["SHADOW_FEATURE_PREPARATION"] = "SHADOW_FEATURE_PREPARATION"
    grants_execution: Literal[False] = False

    @model_validator(mode="after")
    def prospective_shadow(self) -> Self:
        if self.consumed_at != self.recorded_at or self.shadow.at != self.consumed_at:
            raise ValueError("PROSPECTIVE_SHADOW_TIMESTAMPS_REQUIRED")
        if not self.shadow.used_document_ids:
            raise ValueError("SHADOW_REQUIRES_ACTUAL_FEATURE_CONSUMPTION")
        if self.candidate_id is None:
            if self.candidate_feature_values is not None or self.reading_predictions:
                raise ValueError("CANDIDATE_FEATURES_REQUIRE_CANDIDATE_ID")
        else:
            selected = {
                r.reading_id
                for r in self.shadow.readings
                if r.document_id in self.shadow.used_document_ids
            }
            predictions = dict(self.reading_predictions)
            if set(predictions) != selected or len(predictions) != len(self.reading_predictions):
                raise ValueError("EXACT_CANDIDATE_READING_PREDICTIONS_REQUIRED")
            values = tuple(predictions.values())
            if any(not -1 <= value <= 1 for value in values):
                raise ValueError("BOUNDED_CANDIDATE_POLARITY_REQUIRED")
            positive, negative = sum(v > 0 for v in values), sum(v < 0 for v in values)
            expected = (
                float(len(values)),
                float(positive),
                float(negative),
                sum(values) / len(values),
                float(bool(positive and negative)),
                self.shadow.features[-1][1],
            )
            if self.candidate_feature_values != expected:
                raise ValueError("EXACT_CANDIDATE_FEATURE_VALUES_REQUIRED")
        return self

    @property
    def receipt_id(self) -> str:
        return content_id(self)


def retain_shadow_consumption(
    research: DiscoveryStore,
    receipt: ContextShadowConsumption,
    *,
    now: datetime,
    candidate_validator: Callable[[ContextShadowConsumption], bool] | None = None,
) -> bool:
    """Called prospectively by the runtime before it produces a decision, never backfilled."""
    receipt = ContextShadowConsumption.model_validate(receipt.model_dump())
    now = aware(now)
    if receipt.consumed_at != now:
        raise ValueError("PROSPECTIVE_SHADOW_CURRENT_CLOCK_REQUIRED")
    if receipt.candidate_id is not None and (
        candidate_validator is None or not candidate_validator(receipt)
    ):
        raise ValueError("RECOMPUTED_VALIDATED_CANDIDATE_REQUIRED")
    capture = load_context_capture(research, receipt.original_snapshot_id)
    if capture.snapshot.at > now or capture.snapshot.symbol != receipt.shadow.symbol:
        raise ValueError("SHADOW_ORIGINAL_CONTEXT_UNAVAILABLE")
    reason = _source_gate(research, capture, load_source_permissions(research), set(), now)
    if reason:
        raise ValueError(reason)
    expected = build_context_snapshot(
        retained_context_documents(research, capture),
        symbol=capture.snapshot.symbol,
        at=now,
        coverage=capture.snapshot.coverage,
        used_document_ids=receipt.shadow.used_document_ids,
    )
    if receipt.shadow != expected:
        raise ValueError("SHADOW_FEATURE_PROVENANCE_MISMATCH")
    key = "context-shadow:" + receipt.observation_id
    with research.connection() as db:
        previous = research._get(db, key)
        if previous is not None:
            if previous == receipt.receipt_id:
                return False
            raise ValueError("SHADOW_CONSUMPTION_IMMUTABLE_CONFLICT")
        identity = research._receipt(db, receipt)
        research._set(db, key, identity)
    return True


def load_shadow_consumption(
    research: DiscoveryStore,
    *,
    observation_id: str,
) -> ContextShadowConsumption | None:
    with research.connection() as db:
        identity = research._get(db, "context-shadow:" + observation_id)
        if identity is None:
            return None
        row = db.execute("select payload from receipts where id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("SHADOW_CONSUMPTION_UNAVAILABLE")
    receipt = ContextShadowConsumption.model_validate_json(row[0])
    if receipt.receipt_id != identity or receipt.observation_id != observation_id:
        raise ValueError("SHADOW_CONSUMPTION_IDENTITY_MISMATCH")
    return receipt


def runtime_downstream_examples(
    experiments: ExperimentStore,
    research: DiscoveryStore,
    *,
    experiment_id: str,
    observations: Sequence[Observation],
    as_of: datetime,
    cost_basis: Literal["ACTUAL_NET", "MODELED_NET"],
    modeled_cost_contract_id: str | None = None,
    decision_ids: set[str] | None = None,
) -> tuple[tuple[DownstreamExample, ...], tuple[tuple[str, str], ...]]:
    """Pair genuine prospective shadow consumption with later cost-labeled outcome facts.

    This is shadow research input, not evidence that context influenced the executed trade.
    Canonical trial admission, chronology and source-cluster checks still precede fitting.
    """
    as_of = aware(as_of)
    if len(observations) > 200:
        raise ValueError("BOUNDED_SHADOW_PRICE_OBSERVATIONS_REQUIRED")
    if decision_ids is not None and (
        len(decision_ids) > 200
        or any(not isinstance(identity, str) or not identity for identity in decision_ids)
    ):
        raise ValueError("BOUNDED_SHADOW_DECISION_SELECTION_REQUIRED")
    rows, prior_exclusions = load_runtime_context_feedback(
        experiments, research, experiment_id=experiment_id, as_of=as_of
    )
    if decision_ids is not None:
        rows = tuple(row for row in rows if row.decision_id in decision_ids)
    by_id = {row.id: Observation.model_validate(row.model_dump()) for row in observations}
    permissions = load_source_permissions(research)
    events = experiments.read_as_of(
        experiment_id,
        as_of,
        maximum_records=4000,
        kinds=("ContextModeledOutcomeLabel", "ContextInvalidationRecord"),
    )
    labels = [
        e.decode(ContextModeledOutcomeLabel)
        for e in events
        if e.kind == ContextModeledOutcomeLabel.__name__
    ]
    revoked = {
        e.decode(ContextInvalidationRecord).invalidation.reference_id
        for e in events
        if e.kind == ContextInvalidationRecord.__name__
        and e.decode(ContextInvalidationRecord).invalidation.invalidated_at <= as_of
    }
    examples, excluded = [], list(prior_exclusions)
    for row in rows:
        observation = by_id.get(row.observation_id)
        shadow = load_shadow_consumption(research, observation_id=row.observation_id)
        if observation is None:
            excluded.append((row.feedback_id, "SHADOW_PRICE_OBSERVATION_UNAVAILABLE"))
            continue
        if tuple(name for name, _ in observation.features) != FEATURE_NAMES:
            excluded.append((row.feedback_id, "SHADOW_PRICE_FEATURE_SCHEMA_MISMATCH"))
            continue
        if (
            shadow is None
            or shadow.original_snapshot_id != row.snapshot_id
            or shadow.consumed_at > row.decision_at
        ):
            excluded.append((row.feedback_id, "PROSPECTIVE_SHADOW_CONSUMPTION_UNAVAILABLE"))
            continue
        if {shadow.receipt_id, shadow.candidate_id} & revoked:
            excluded.append((row.feedback_id, "SHADOW_INTERPRETER_INVALIDATED"))
            continue
        capture = load_context_capture(research, row.snapshot_id)
        if any(
            permissions[b.permissions.provider].training != "VERIFIED"
            for b in capture.bindings
            if b.document_id in shadow.shadow.used_document_ids
        ):
            excluded.append((row.feedback_id, "TRAINING_PERMISSION_UNVERIFIED"))
            continue
        target = (
            row.actual_net_return_bps if cost_basis == "ACTUAL_NET" else row.modeled_net_return_bps
        )
        cost_evidence_id = content_id(row.cost_evidence_ids)
        label_available_at = row.label_available_at
        if cost_basis == "MODELED_NET" and modeled_cost_contract_id is not None:
            label = next(
                (
                    item
                    for item in labels
                    if item.feedback_id == row.feedback_id
                    and item.contract.contract_id == modeled_cost_contract_id
                ),
                None,
            )
            if (
                label is None
                or {modeled_cost_contract_id, *label.contract.source_policy_refs} & revoked
            ):
                excluded.append((row.feedback_id, "DECLARED_MODELED_COST_LABEL_UNAVAILABLE"))
                continue
            target = label.net_return_bps
            cost_evidence_id = content_id(label)
            label_available_at = max(label_available_at, label.recorded_at)
        if target is None:
            excluded.append((row.feedback_id, cost_basis + "_UNAVAILABLE"))
            continue
        examples.append(
            DownstreamExample(
                decision_id=row.decision_id,
                decision_at=row.decision_at,
                context=shadow.shadow,
                price_features=tuple(value for _, value in observation.features),
                price_available_at=observation.available_at,
                label_available_at=label_available_at,
                net_return_bps=float(target),
                cost_basis=cost_basis,
                cost_evidence_id=cost_evidence_id,
                consumed_context_features=shadow.candidate_feature_values,
                interpreter_id=shadow.candidate_id,
            )
        )
    return tuple(examples), tuple(excluded)
