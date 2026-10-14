"""Atomic, explicitly commissioned local model selection; no operational authority."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Literal, Self, cast

from pydantic import SerializerFunctionWrapHandler, model_serializer
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerModel
from quantbot.domain import IntentState
from quantbot.paper_core.contracts import LearningReceipt, ModelArtifact
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError, StorageRepository


class ActivationAuthority(BrokerModel):
    runtime_commission_id: str | None = None

    @model_serializer(mode="wrap")
    def preserve_v34_authority(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        if "runtime_commission_id" not in self.model_fields_set:
            payload.pop("runtime_commission_id", None)
        return payload

    config_id: str
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"]
    candidate_id: str
    receipt_id: str
    expected_activation_id: str
    commissioned_at: datetime
    expires_at: datetime
    reviewer: str
    feature_version: Literal["five-minute-price-alpha-v1"] = "five-minute-price-alpha-v1"
    target_contract: Literal["owned-executed-net-return-v1"] = "owned-executed-net-return-v1"
    exit_contract: Literal["owned-30m-stop100-v1"] = "owned-30m-stop100-v1"
    policy_id: Literal["collect-owned-30m-stop100-v1"] = "collect-owned-30m-stop100-v1"
    procedure_version: Literal["daily-all-history-walk-forward-v2"] = (
        "daily-all-history-walk-forward-v2"
    )
    selection_transition: Literal["BOUNDED_COLLECT_TO_PREDICT"] = "BOUNDED_COLLECT_TO_PREDICT"
    risk_contract: Literal["positions1-position50-risk25-stop100-horizon30-reserve10-v1"] = (
        "positions1-position50-risk25-stop100-horizon30-reserve10-v1"
    )

    @property
    def id(self) -> str:
        return content_id(self)

    @classmethod
    def fixture(
        cls,
        store: CoreStore,
        *,
        candidate_id: str,
        receipt_id: str,
        expected_activation_id: str,
        at: datetime,
    ) -> Self:
        if store.config.evidence_lane == "BROKER_PAPER":
            raise StateConflictError("FIXTURE_AUTHORITY_CANNOT_COMMISSION_PAPER")
        return cls(
            config_id=store.config_id,
            evidence_lane=store.config.evidence_lane,
            candidate_id=candidate_id,
            receipt_id=receipt_id,
            expected_activation_id=expected_activation_id,
            commissioned_at=at,
            expires_at=at + timedelta(minutes=5),
            reviewer="EXPLICIT_LOCAL_FIXTURE_COMMISSION",
        )


class Activation(BrokerModel):
    config_id: str
    evidence_lane: Literal["SYNTHETIC_TEST", "LOCAL_SIM_REAL_DATA", "BROKER_PAPER"]
    generation: int
    previous_activation_id: str | None
    previous_model_id: str | None
    model_id: str
    model_digest: str
    receipt_id: str | None
    authority_id: str | None
    at: datetime
    action: Literal["INITIALIZE", "ACTIVATE", "ACKNOWLEDGE", "ROLLBACK"]
    reason: str
    learning_run_id: str | None = None

    @property
    def id(self) -> str:
        return content_id(self)


def _document(session: Session, name: str, identity: str, kind: type[Any]) -> Any:
    target = table(name)
    payload = session.execute(
        select(target.c.payload).where(target.c.id == identity)
    ).scalar_one_or_none()
    if payload is None:
        raise StateConflictError("MODEL_AUTHORITY_DOCUMENT_MISSING")
    document = kind.model_validate_json(payload)
    if document.id != identity:
        raise StateConflictError("MODEL_AUTHORITY_DIGEST_MISMATCH")
    return document


def active_in_session(
    store: CoreStore, session: Session
) -> tuple[ModelArtifact, Activation] | None:
    pointer = session.execute(select(table("core_active_model"))).mappings().one_or_none()
    if pointer is None:
        # A removed/corrupt pointer must not reenable the legacy COLLECT path.
        if session.execute(select(table("core_activations").c.id)).first():
            raise StateConflictError("ACTIVE_MODEL_POINTER_MISSING")
        return None
    event: Activation = _document(session, "core_activations", pointer["activation_id"], Activation)
    model: ModelArtifact = _document(session, "core_models", event.model_id, ModelArtifact)
    if (
        event.config_id != store.config_id
        or event.evidence_lane != store.config.evidence_lane
        or event.generation != pointer["generation"]
        or event.model_id != pointer["model_id"]
        or event.model_digest != model.id
        or event.receipt_id != pointer["receipt_id"]
    ):
        raise StateConflictError("ACTIVE_MODEL_BINDING_INVALID")
    if event.authority_id:
        authority: ActivationAuthority = _document(
            session, "core_activation_authorities", event.authority_id, ActivationAuthority
        )
        if (
            authority.config_id != store.config_id
            or authority.evidence_lane != event.evidence_lane
            or authority.candidate_id != model.id
            or authority.receipt_id != event.receipt_id
        ):
            raise StateConflictError("ACTIVE_MODEL_AUTHORITY_INVALID")
        if event.evidence_lane == "BROKER_PAPER":
            from quantbot.paper_core.authority import commission_in_session

            commission = commission_in_session(store, session)
            if authority.runtime_commission_id != commission.id:
                raise StateConflictError("ACTIVE_MODEL_RUNTIME_AUTHORITY_INVALID")
        receipt = _receipt(session, model, authority.receipt_id)
        if receipt.available_at is None or receipt.available_at > event.at:
            raise StateConflictError("ACTIVE_MODEL_RECEIPT_UNAVAILABLE")
    elif event.action != "INITIALIZE" and event.action != "ROLLBACK":
        raise StateConflictError("ACTIVE_MODEL_APPROVAL_MISSING")
    events = [
        Activation.model_validate_json(payload)
        for payload in session.execute(select(table("core_activations").c.payload)).scalars()
    ]
    if (
        any(e.generation > event.generation for e in events)
        or sum(e.generation == event.generation for e in events) != 1
    ):
        raise StateConflictError("ACTIVE_MODEL_POINTER_AMBIGUOUS")
    return model, event


def read_active_model(store: CoreStore) -> ModelArtifact:
    with store.database.transaction() as session:
        selection = active_in_session(store, session)
        if selection is None:
            raise StateConflictError("ACTIVE_MODEL_UNCOMMISSIONED")
        return selection[0]


def _write_event(session: Session, event: Activation) -> None:
    put(session, "core_activations", event.id, event)
    pointer = table("core_active_model")
    values = dict(
        activation_id=event.id,
        model_id=event.model_id,
        generation=event.generation,
        receipt_id=event.receipt_id,
    )
    if event.previous_activation_id is None:
        session.execute(pointer.insert().values(id=1, **values))
    else:
        result = session.execute(
            update(pointer)
            .where(
                pointer.c.id == 1,
                pointer.c.activation_id == event.previous_activation_id,
                pointer.c.generation == event.generation - 1,
            )
            .values(**values)
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise StateConflictError("STALE_ACTIVATION")


def initialize_fixture_model(store: CoreStore, model: ModelArtifact, *, at: datetime) -> Activation:
    """Explicit local bootstrap only; restart never calls this commissioning function."""
    if store.config.evidence_lane == "BROKER_PAPER":
        raise StateConflictError("FIXTURE_BOOTSTRAP_CANNOT_COMMISSION_PAPER")
    if model.estimator is not None or (model.mode == "PREDICT" and not model.fixture_authority):
        raise StateConflictError("BOOTSTRAP_REQUIRES_EXPLICIT_LOCAL_BASELINE")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        if active_in_session(store, session) is not None:
            raise StateConflictError("MODEL_ALREADY_COMMISSIONED")
        put(session, "core_models", model.id, model)
        event = Activation(
            config_id=store.config_id,
            evidence_lane=store.config.evidence_lane,
            generation=1,
            previous_activation_id=None,
            previous_model_id=None,
            model_id=model.id,
            model_digest=model.id,
            receipt_id=None,
            authority_id=None,
            at=at,
            action="INITIALIZE",
            reason="EXPLICIT_LOCAL_FIXTURE_BOOTSTRAP",
        )
        _write_event(session, event)
        return event


def initialize_operational_model(
    store: CoreStore, model: ModelArtifact, *, at: datetime
) -> Activation:
    from quantbot.paper_core.authority import commission_in_session

    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        commission = commission_in_session(store, session)
        if (
            store.config.evidence_lane != "BROKER_PAPER"
            or model.mode != "COLLECT"
            or model.fixture_authority
            or model.estimator is not None
            or model.id != commission.initial_model_id
            or at != commission.approved_at
        ):
            raise StateConflictError("INITIAL_MODEL_NOT_EXACTLY_COMMISSIONED")
        if active_in_session(store, session) is not None:
            raise StateConflictError("MODEL_ALREADY_COMMISSIONED")
        put(session, "core_models", model.id, model)
        event = Activation(
            config_id=store.config_id,
            evidence_lane="BROKER_PAPER",
            generation=1,
            previous_activation_id=None,
            previous_model_id=None,
            model_id=model.id,
            model_digest=model.id,
            receipt_id=None,
            authority_id=None,
            at=at,
            action="INITIALIZE",
            reason="EXPLICIT_OPERATIONAL_COLLECT_COMMISSION:" + commission.id,
        )
        _write_event(session, event)
        return event


def commission_operational_candidate(
    store: CoreStore,
    receipt: LearningReceipt,
    *,
    expected_activation_id: str,
    at: datetime,
    approval_reference: str | None = None,
) -> ActivationAuthority:
    from quantbot.paper_core.authority import commission_in_session

    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        commission = commission_in_session(store, session)
        if (
            receipt.evidence_lane != "BROKER_PAPER"
            or receipt.policy.evidence_lane != "BROKER_PAPER"
        ):
            raise StateConflictError("OPERATIONAL_RECEIPT_REQUIRED")
        if (
            receipt.status != "NO_CHANGE"
            and commission.activation_policy != "DAILY_BOUNDED_V2"
            and not approval_reference
        ):
            raise StateConflictError("AWAITING_EXACT_CANDIDATE_APPROVAL")
        candidate = _document(session, "core_models", receipt.resulting_model_id, ModelArtifact)
        _receipt(session, candidate, receipt.id)
        authority = ActivationAuthority(
            config_id=store.config_id,
            evidence_lane="BROKER_PAPER",
            candidate_id=candidate.id,
            receipt_id=receipt.id,
            expected_activation_id=expected_activation_id,
            commissioned_at=at,
            expires_at=at + timedelta(minutes=5),
            reviewer=approval_reference or "COMMISSIONED_PROCEDURE:" + commission.id,
            runtime_commission_id=commission.id,
        )
        put(session, "core_activation_authorities", authority.id, authority)
        return authority


def commission_fixture_authority(store: CoreStore, authority: ActivationAuthority) -> str:
    if store.config.evidence_lane == "BROKER_PAPER":
        raise StateConflictError("FIXTURE_AUTHORITY_CANNOT_COMMISSION_PAPER")
    if (
        authority.config_id != store.config_id
        or authority.evidence_lane != store.config.evidence_lane
    ):
        raise StateConflictError("ACTIVATION_AUTHORITY_LANE_MISMATCH")
    if authority.reviewer != "EXPLICIT_LOCAL_FIXTURE_COMMISSION":
        raise StateConflictError("ONLY_LOCAL_FIXTURE_COMMISSION_IMPLEMENTED")
    with store.database.transaction() as session:
        put(session, "core_activation_authorities", authority.id, authority)
    return authority.id


def _boundary(session: Session, *, cutoff: datetime) -> None:
    from quantbot.paper_core.outcomes import terminal_evidence
    from quantbot.paper_core.send_guard import definitively_unsent

    repo = StorageRepository(session)
    for link in session.execute(select(table("core_intent_links"))).mappings():
        intent = repo.get_order_intent(link["intent_id"])
        if intent is None:
            raise StateConflictError("ACTIVATION_UNRESOLVED_CLAIM")
        if definitively_unsent(repo, intent.intent_id):
            continue
        if intent.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
            raise StateConflictError("ACTIVATION_UNRESOLVED_SUBMISSION")
        order = repo.get_broker_order_by_client_order_id(intent.client_order_id)
        if order is not None and terminal_evidence(session, order, cutoff=cutoff) is not None:
            continue
        raise StateConflictError("ACTIVATION_UNRESOLVED_CLAIM")


def _receipt(session: Session, model: ModelArtifact, identity: str) -> LearningReceipt:
    receipt: LearningReceipt = _document(session, "core_learning_runs", identity, LearningReceipt)
    mapping = table("core_learning_considerations")
    actual = session.execute(
        select(mapping.c.receipt_id).where(mapping.c.run_id == receipt.run_id)
    ).scalar_one_or_none()
    if actual != receipt.id:
        raise StateConflictError("ACTIVATION_RECEIPT_MAPPING_INVALID")
    if receipt.status == "NO_CHANGE":
        if receipt.parent_model_id != model.id or receipt.resulting_model_id != model.id:
            raise StateConflictError("ACKNOWLEDGEMENT_MODEL_MISMATCH")
    elif (
        receipt.status != "AWAITING_ACTIVATION"
        or receipt.resulting_model_id != model.id
        or receipt.candidate_model_id != model.id
        or model.learning_receipt_id != receipt.run_id
        or receipt.policy.procedure_version != "daily-all-history-walk-forward-v2"
        or len(receipt.fold_results) != 5
        or receipt.final_input_ids != model.input_ids
        or receipt.trial_charge_id is None
        or model.estimator is None
    ):
        raise StateConflictError("ACTIVATION_CANDIDATE_NOT_QUALIFIED")
    if receipt.status == "AWAITING_ACTIVATION":
        metrics = dict(receipt.metrics)
        required = {
            "candidate_mse",
            "constant_mse",
            "incumbent_mse",
            "candidate_worst_session_mse",
            "constant_worst_session_mse",
            "incumbent_worst_session_mse",
        }
        attempt = (
            session.execute(
                select(table("core_learning_attempts")).where(
                    table("core_learning_attempts").c.run_id == receipt.run_id
                )
            )
            .mappings()
            .one_or_none()
        )
        export_payload = session.execute(
            select(table("core_learning_inputs").c.payload).where(
                table("core_learning_inputs").c.id == receipt.input_export_id
            )
        ).scalar_one_or_none()
        charges = table("search_runs")
        charge = (
            session.execute(select(charges).where(charges.c.search_id == receipt.trial_charge_id))
            .mappings()
            .one_or_none()
        )
        if (
            set(metrics) != required
            or any(not math.isfinite(v) or v < 0 for v in metrics.values())
            or metrics["candidate_mse"]
            > 0.95 * min(metrics["constant_mse"], metrics["incumbent_mse"])
            or metrics["candidate_worst_session_mse"]
            > min(metrics["constant_worst_session_mse"], metrics["incumbent_worst_session_mse"])
            or receipt.reason != "PASSED_FIXED_PROCEDURE_GATES"
            or receipt.final_validation_absent_reason != "FINAL_REFIT_HAS_NO_INDEPENDENT_HOLDOUT"
            or model.parent_model_id != receipt.parent_model_id
            or receipt.policy.parent_model.id != receipt.parent_model_id
            or attempt is None
            or attempt["search_id"] != receipt.trial_charge_id
            or charge is None
            or charge["configuration_hash"] != receipt.policy.id
            or charge["actor"] != "paper-core-shadow-trial-charge"
            or charge["actor_version"] != "owned-executed-net-return-v1"
            or charge["candidates_evaluated"] != 1
            or json.loads(charge["detail_json"]).get("run_id") != receipt.run_id
            or json.loads(charge["detail_json"]).get("event") != "PERMANENT_TRIAL_CHARGE"
            or export_payload is None
            or content_id(json.loads(export_payload)) != receipt.input_export_id
        ):
            raise StateConflictError("ACTIVATION_FIXED_VALIDATION_NOT_PASSED")
    return receipt


def activate_model(
    store: CoreStore,
    *,
    expected_model_id: str,
    candidate_id: str,
    authority: ActivationAuthority,
    at: datetime,
) -> Activation:
    if store.config.evidence_lane == "BROKER_PAPER":
        from quantbot.paper_core.canonical import refresh_canonical_metadata

        refresh_canonical_metadata(store)
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        approved = _document(
            session, "core_activation_authorities", authority.id, ActivationAuthority
        )
        selection = active_in_session(store, session)
        if selection is None:
            raise StateConflictError("ACTIVE_MODEL_UNCOMMISSIONED")
        parent, previous = selection
        if parent.id != expected_model_id or previous.id != authority.expected_activation_id:
            raise StateConflictError("STALE_ACTIVATION")
        if (
            approved != authority
            or authority.config_id != store.config_id
            or authority.evidence_lane != store.config.evidence_lane
            or authority.candidate_id != candidate_id
            or not previous.at <= authority.commissioned_at <= at < authority.expires_at
        ):
            raise StateConflictError("ACTIVATION_AUTHORITY_INVALID")
        candidate: ModelArtifact = _document(session, "core_models", candidate_id, ModelArtifact)
        receipt = _receipt(session, candidate, authority.receipt_id)
        if store.config.evidence_lane == "BROKER_PAPER":
            from quantbot.paper_core.operational_costs import pending_cost_revision_source

            if session.execute(
                select(table("core_source_cursors").c.source_id).where(
                    table("core_source_cursors").c.source_id
                    == pending_cost_revision_source(store.config.account_id)
                )
            ).first():
                raise StateConflictError("ACTIVATION_CURRENT_COST_REVISIONS_PENDING")
        if store.config.evidence_lane == "BROKER_PAPER" and receipt.status != "NO_CHANGE":
            from datetime import date

            from quantbot.paper_core.learning import _guard

            ranges = session.execute(
                select(table("core_outcome_revisions").c.payload).where(
                    table("core_outcome_revisions").c.id.in_(receipt.final_input_ids)
                )
            ).scalars()
            for encoded in ranges:
                outcome = json.loads(encoded)
                reason = _guard(
                    session,
                    receipt.policy.dataset,
                    date.fromisoformat(outcome["source_start"][:10]),
                    date.fromisoformat(outcome["source_end"][:10]),
                    now=store.clocks.sample()[0].date(),
                )
                if reason:
                    raise StateConflictError("ACTIVATION_CURRENT_CANONICAL_REFUSAL:" + reason)
        if (
            receipt.parent_model_id != parent.id
            or receipt.evidence_lane != authority.evidence_lane
            or receipt.policy.evidence_lane != authority.evidence_lane
            or receipt.available_at is None
            or receipt.available_at > at
            or receipt.completed_at != receipt.available_at
            or receipt.execution_clock_domain is None
            or not receipt.execution_clock_domain.startswith(authority.evidence_lane + ":")
            or candidate.feature_version != authority.feature_version
            or (candidate.mode == "PREDICT" and candidate.policy_id != authority.policy_id)
            or (
                candidate.mode == "PREDICT"
                and candidate.target_contract != authority.target_contract
            )
            or receipt.policy.exit_contract != authority.exit_contract
        ):
            raise StateConflictError("ACTIVATION_RECEIPT_OR_CONTRACT_INVALID")
        _boundary(session, cutoff=at)
        event = Activation(
            config_id=store.config_id,
            evidence_lane=authority.evidence_lane,
            generation=previous.generation + 1,
            previous_activation_id=previous.id,
            previous_model_id=parent.id,
            model_id=candidate.id,
            model_digest=candidate.id,
            receipt_id=receipt.id,
            authority_id=authority.id,
            at=at,
            action="ACKNOWLEDGE" if receipt.status == "NO_CHANGE" else "ACTIVATE",
            reason=receipt.reason,
            learning_run_id=receipt.run_id,
        )
        _write_event(session, event)
        return event


def rollback_model(
    store: CoreStore,
    *,
    expected_model_id: str,
    target_id: str,
    expected_activation_id: str,
    reason: str,
    at: datetime,
) -> Activation:
    """Select an already reviewed ancestor; never restore or rewrite the trading ledger."""
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        selection = active_in_session(store, session)
        if selection is None:
            raise StateConflictError("ACTIVE_MODEL_UNCOMMISSIONED")
        model, previous = selection
        if (
            model.id != expected_model_id
            or previous.id != expected_activation_id
            or at < previous.at
        ):
            raise StateConflictError("STALE_ACTIVATION")
        ancestors = []
        cursor: Activation | None = previous
        while cursor is not None:
            ancestors.append(cursor)
            cursor = (
                None
                if cursor.previous_activation_id is None
                else _document(
                    session, "core_activations", cursor.previous_activation_id, Activation
                )
            )
        target_event = next((e for e in ancestors if e.model_id == target_id), None)
        if target_event is None or not reason.strip():
            raise StateConflictError("ROLLBACK_TARGET_NOT_REVIEWED")
        target: ModelArtifact = _document(session, "core_models", target_id, ModelArtifact)
        if target.feature_version != model.feature_version:
            raise StateConflictError("ROLLBACK_CONTRACT_MISMATCH")
        _boundary(session, cutoff=at)
        event = Activation(
            config_id=store.config_id,
            evidence_lane=previous.evidence_lane,
            generation=previous.generation + 1,
            previous_activation_id=previous.id,
            previous_model_id=model.id,
            model_id=target.id,
            model_digest=target.id,
            receipt_id=target_event.receipt_id,
            authority_id=target_event.authority_id,
            at=at,
            action="ROLLBACK",
            reason=reason,
            learning_run_id=target_event.learning_run_id,
        )
        _write_event(session, event)
        return event


def decision_model_objections(store: CoreStore, session: Session, decision: Any) -> tuple[str, ...]:
    try:
        selection = active_in_session(store, session)
        if selection is None:
            return () if decision.activation_id is None else ("CORE_MODEL_UNCOMMISSIONED",)
        model, event = selection
        if (
            decision.activation_id != event.id
            or decision.model_generation != event.generation
            or decision.model_id != model.id
            or decision.model_digest != model.id
            or decision.learning_receipt_id != (event.receipt_id or model.learning_receipt_id)
            or decision.recorded_at_utc < event.at
        ):
            return ("CORE_MODEL_SELECTION_STALE",)
    except (ValueError, StateConflictError):
        return ("CORE_MODEL_BINDING_INVALID",)
    return ()


def export_loop_evidence(store: CoreStore, *, decision_id: str) -> dict[str, Any]:
    bundle = store.export_loop(decision_id)
    with store.database.transaction() as session:
        pointer = session.execute(select(table("core_active_model"))).mappings().one_or_none()
        mappings = [
            dict(row)
            for row in session.execute(select(table("core_learning_considerations"))).mappings()
        ]
    bundle.update(
        active_pointer=dict(pointer) if pointer else None,
        activations=store.documents("core_activations"),
        authorities=store.documents("core_activation_authorities"),
        models=store.documents("core_models"),
        learning_receipts=store.documents("core_learning_runs"),
        learning_inputs=store.documents("core_learning_inputs"),
        decisions=store.documents("core_decisions"),
        consideration_receipt_mapping=mappings,
        learning_completions=[
            {
                "status": "ACTIVATED",
                "activation_id": content_id(e),
                "activation_at": e["at"],
                "receipt_id": e["receipt_id"],
                "run_id": e["learning_run_id"],
            }
            for e in store.documents("core_activations")
            if e["action"] == "ACTIVATE"
        ],
    )
    return bundle
