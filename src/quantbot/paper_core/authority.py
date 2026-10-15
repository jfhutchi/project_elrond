"""Explicit operational commission and independent role attestation."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import Field
from sqlalchemy import select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from quantbot.brokers.base import BrokerModel
from quantbot.paper_core.release import PAPER_ENDPOINT, PreparedRelease
from quantbot.paper_core.store import CoreStore, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError


class RuntimeCommission(BrokerModel):
    kind: Literal["BROKER_PAPER_EXPLICIT_COMMISSION"] = "BROKER_PAPER_EXPLICIT_COMMISSION"
    release_id: str
    config_id: str
    store_identity: str
    store_path: str
    account_id: str
    endpoint: Literal["https://paper-api.alpaca.markets"] = PAPER_ENDPOINT
    owner: str
    generation: int = Field(ge=1)
    approved_at: datetime
    approval_reference: str = Field(min_length=1)
    initial_model_id: str
    activation_policy: Literal["REVIEW_EACH_CANDIDATE", "DAILY_BOUNDED_V2"]
    learning_origin: datetime
    legacy_owners: tuple[tuple[str, str], ...]
    baseline_source_ids: tuple[str, ...] = Field(min_length=1)
    canonical_metadata_id: str = Field(min_length=64, max_length=64)
    time_contract: Literal["PAPER_TIME_V2"] = "PAPER_TIME_V2"

    @property
    def id(self) -> str:
        return content_id(self)


class RuntimeEvent(BrokerModel):
    commission_id: str
    sequence: int = Field(ge=1)
    at: datetime
    action: Literal["COMMISSION", "FENCE_ENTRY", "RESUME_ENTRY"]
    evidence_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)

    @property
    def id(self) -> str:
        return content_id(self)


class RoleHeartbeat(BrokerModel):
    role: Literal["trader", "guardian", "outcomes"]
    release_id: str
    config_id: str
    store_identity: str
    owner: str
    generation: int
    process_epoch: str
    pid: int
    recorded_at: datetime
    valid_until: datetime
    last_useful_progress: datetime | None
    objections: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    proof_age_seconds: float | None
    guardian_receipt_id: str | None = None
    first_blocker: str | None = None
    time_proof_age_seconds: float | None = None
    cycle_seconds: float = 0.0

    @property
    def id(self) -> str:
        return content_id(self)


class ExecutableBinding(BrokerModel):
    kind: Literal["CODE_ONLY_EXECUTABLE_BINDING_V1"] = "CODE_ONLY_EXECUTABLE_BINDING_V1"
    commission_id: str
    sequence: int = Field(ge=1)
    previous_id: str | None
    release: PreparedRelease
    approved_at: datetime
    approval_reference: str = Field(min_length=1)
    fence_id: str | None
    activation_id: str
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @property
    def id(self) -> str:
        return content_id(self)


def executable_in_session(session: Session) -> ExecutableBinding | None:
    documents, pointer = table("core_runtime_config_history"), table("core_runtime_current")
    payload = session.execute(
        select(documents.c.payload).join(pointer, documents.c.id == pointer.c.document_id)
    ).scalar_one_or_none()
    return ExecutableBinding.model_validate_json(payload) if payload else None


def current_runtime_event(session: Session) -> RuntimeEvent:
    events = [
        RuntimeEvent.model_validate_json(p)
        for p in session.execute(select(table("core_runtime_events").c.payload)).scalars()
    ]
    if not events or len({e.sequence for e in events}) != len(events):
        raise StateConflictError("CORE_RUNTIME_EVENT_INVALID")
    return max(events, key=lambda e: e.sequence)


def commission_in_session(store: CoreStore, session: Session) -> RuntimeCommission:
    rows = session.execute(select(table("core_runtime_commissions"))).mappings().all()
    if len(rows) != 1:
        raise StateConflictError("CORE_RUNTIME_UNCOMMISSIONED")
    commission = RuntimeCommission.model_validate_json(rows[0]["payload"])
    executable = executable_in_session(session)
    if (
        commission.id != rows[0]["id"]
        or commission.config_id != store.config_id
        or commission.release_id != store.config.release_id
        or commission.account_id != store.config.account_id
        or commission.owner != store.config.owner
        or commission.generation != store.config.generation
        or commission.store_identity != store.database.store_identity
        or Path(commission.store_path).resolve() != Path(store.database.path).resolve()
        or executable is not None
        and (
            executable.commission_id != commission.id
            or executable.release.id != store.runtime_release_id
        )
        or executable is None
        and store.runtime_release_id != commission.release_id
    ):
        raise StateConflictError("CORE_RUNTIME_BINDING_MISMATCH")
    return commission


def runtime_objections(store: CoreStore, session: Session, *, entry: bool) -> tuple[str, ...]:
    try:
        commission = commission_in_session(store, session)
    except StateConflictError as error:
        return (str(error),)
    try:
        current = current_runtime_event(session)
    except StateConflictError:
        return ("CORE_RUNTIME_EVENT_INVALID",)
    if current.commission_id != commission.id:
        return ("CORE_RUNTIME_EVENT_INVALID",)
    return ("CORE_ENTRY_FENCED",) if entry and current.action == "FENCE_ENTRY" else ()


def commission_runtime(
    store: CoreStore,
    release: PreparedRelease,
    commission: RuntimeCommission,
    *,
    baseline: dict[str, Any],
) -> str:
    """Explicit approved write path. Never called by role startup or prepare/verify."""
    if store.config.evidence_lane != "BROKER_PAPER":
        raise StateConflictError("OPERATIONAL_COMMISSION_REQUIRES_BROKER_PAPER")
    from quantbot.paper_core.release import verify_release

    if verify_release(release)["objections"]:
        raise StateConflictError("COMMISSION_EXECUTABLE_IDENTITY_UNVERIFIED")
    if (
        release.id != commission.release_id
        or commission.config_id != store.config_id
        or release.config.store_identity != store.database.store_identity
        or release.config.activation_policy != commission.activation_policy
        or release.config.initial_model.id != commission.initial_model_id
        or baseline.get("source") != "BROKER_PAPER_APPROVED_INITIAL_STATE"
        or tuple(baseline.get("source_ids", ())) != commission.baseline_source_ids
    ):
        raise StateConflictError("COMMISSION_RELEASE_OR_BASELINE_MISMATCH")
    from quantbot.paper_core.__main__ import derived_core

    if store.config != derived_core(release, commission.account_id):
        raise StateConflictError("COMMISSION_DERIVED_CONFIG_MISMATCH")
    from quantbot.domain import Account, Position

    account = Account.model_validate(baseline["account"])
    positions = tuple(Position.model_validate(p) for p in baseline["positions"])
    if (
        not baseline.get("fenced_writer_evidence_ids")
        or baseline.get("unknown_orders") != []
        or not isinstance(baseline.get("provider_acquisition"), dict)
        or content_id(baseline["provider_acquisition"]) != commission.baseline_source_ids[0]
        or not 0 <= (store.clocks.sample()[0] - commission.approved_at).total_seconds() <= 120
    ):
        raise StateConflictError("COMMISSION_FRESH_ACQUISITION_OR_ENTRY_FENCE_MISSING")
    if (
        account.account_id != commission.account_id
        or baseline.get("provider") != "alpaca"
        or baseline.get("endpoint") != commission.endpoint
        or baseline.get("captured_at") is None
        or not 0
        <= (
            commission.approved_at - datetime.fromisoformat(str(baseline["captured_at"]))
        ).total_seconds()
        <= 5
        or set(dict(commission.legacy_owners)) != {p.symbol for p in positions if p.quantity != 0}
        or any(not owner for _, owner in commission.legacy_owners)
    ):
        raise StateConflictError("COMMISSION_FRESH_BASELINE_OR_OWNER_MAP_MISSING")
    canonical = baseline.get("canonical_metadata")
    if (
        not isinstance(canonical, dict)
        or content_id(canonical) != commission.canonical_metadata_id
        or canonical.get("source_path") != release.config.holdout_source
        or tuple(canonical.get("canonical_datasets", ())) != release.config.canonical_datasets
    ):
        raise StateConflictError("COMMISSION_CANONICAL_METADATA_MISSING")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        prior = session.execute(select(table("core_runtime_commissions").c.id)).scalar_one_or_none()
        if prior is not None:
            if prior != commission.id or commission_in_session(store, session) != commission:
                raise StateConflictError("COMMISSION_ALREADY_EXISTS")
            stored = session.execute(select(table("core_fixture_baselines").c.payload)).scalar_one()
            from quantbot.research.manifest import canonical_result_json

            if stored != canonical_result_json(baseline):
                raise StateConflictError("COMMISSION_BASELINE_MISMATCH")
            from quantbot.paper_core.activation import initialize_operational_model_in_session

            initialize_operational_model_in_session(
                store, session, release.config.initial_model, at=commission.approved_at
            )
            return commission.id
        put(session, "core_runtime_commissions", commission.id, commission)
        commission_in_session(store, session)
        if session.execute(select(table("core_fixture_baselines").c.id)).first():
            raise StateConflictError("COMMISSION_BASELINE_ALREADY_EXISTS")
        put(session, "core_fixture_baselines", content_id(baseline), baseline)
        from quantbot.paper_core.canonical import import_canonical_metadata

        import_canonical_metadata(session, canonical)
        event = RuntimeEvent(
            commission_id=commission.id,
            sequence=1,
            at=commission.approved_at,
            action="COMMISSION",
            evidence_ids=commission.baseline_source_ids,
            reason=commission.approval_reference,
        )
        put(session, "core_runtime_events", event.id, event)
        session.info["core_initial_commission"] = True
        from quantbot.paper_core.activation import initialize_operational_model_in_session

        initial = initialize_operational_model_in_session(
            store, session, release.config.initial_model, at=commission.approved_at
        )
        binding = ExecutableBinding(
            commission_id=commission.id,
            sequence=1,
            previous_id=None,
            release=release,
            approved_at=commission.approved_at,
            approval_reference=commission.approval_reference,
            fence_id=None,
            activation_id=initial.id,
            evidence_ids=commission.baseline_source_ids,
        )
        put(session, "core_runtime_config_history", binding.id, binding)
        session.execute(
            table("core_runtime_current").insert().values(id="current", document_id=binding.id)
        )
    return commission.id


def guardian_objections(
    store: CoreStore,
    session: Session,
    *,
    now: datetime,
    receipt_id: str | None = None,
) -> tuple[str, ...]:
    if (
        getattr(store, "runtime_role", None) is None
        and store.config.evidence_lane != "BROKER_PAPER"
    ):
        return ()  # Existing explicit in-process fixtures preserve their accepted contract.
    pointer, documents = table("core_role_current"), table("core_role_heartbeats")
    payload = session.execute(
        select(documents.c.payload)
        .join(pointer, documents.c.id == pointer.c.heartbeat_id)
        .where(pointer.c.role == "guardian")
    ).scalar_one_or_none()
    latest = RoleHeartbeat.model_validate_json(payload) if payload else None
    if (
        latest is None
        or latest.pid == os.getpid()
        or latest.process_epoch == store.clocks.process_id
        or latest.release_id != store.runtime_release_id
        or latest.config_id != store.config_id
        or latest.store_identity != store.database.store_identity
        or latest.owner != store.config.owner
        or latest.generation != store.config.generation
        or not latest.recorded_at <= now < latest.valid_until
        or latest.last_useful_progress is None
        or latest.objections
        or receipt_id is not None
        and latest.guardian_receipt_id != receipt_id
    ):
        return ("CORE_INDEPENDENT_GUARDIAN_NOT_READY",)
    return ()


def publish_heartbeat(store: CoreStore, heartbeat: RoleHeartbeat) -> None:
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        put(session, "core_role_heartbeats", heartbeat.id, heartbeat)
        pointer = table("core_role_current")
        if session.execute(select(pointer.c.role).where(pointer.c.role == heartbeat.role)).first():
            session.execute(
                update(pointer)
                .where(pointer.c.role == heartbeat.role)
                .values(heartbeat_id=heartbeat.id)
            )
        else:
            session.execute(pointer.insert().values(role=heartbeat.role, heartbeat_id=heartbeat.id))


def fence_entries(store: CoreStore, *, reason: str, evidence_ids: tuple[str, ...]) -> str:
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        commission = commission_in_session(store, session)
        events = [
            RuntimeEvent.model_validate_json(p)
            for p in session.execute(select(table("core_runtime_events").c.payload)).scalars()
        ]
        event = RuntimeEvent(
            commission_id=commission.id,
            sequence=max(e.sequence for e in events) + 1,
            at=store.clocks.sample()[0],
            action="FENCE_ENTRY",
            evidence_ids=evidence_ids,
            reason=reason,
        )
        put(session, "core_runtime_events", event.id, event)
        return event.id


def transition_release(
    store: CoreStore, successor: PreparedRelease, approval: dict[str, Any]
) -> str:
    """Exact same-schema code-only CAS; owned economics and the entry fence are untouched."""
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.release import verify_release

    if verify_release(successor)["objections"]:
        raise StateConflictError("TRANSITION_EXECUTABLE_UNVERIFIED")
    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        commission = commission_in_session(store, session)
        prior = executable_in_session(session)
        event = current_runtime_event(session)
        selection = active_in_session(store, session)
        if prior is None or selection is None:
            raise StateConflictError("TRANSITION_PROTOCOL_NOT_COMMISSIONED")
        if (
            approval.get("expected_binding_id") != prior.id
            or approval.get("old_release_id") != prior.release.id
            or approval.get("new_release_id") != successor.id
            or approval.get("commission_id") != commission.id
            or approval.get("expected_fence_id") != event.id
            or event.action != "FENCE_ENTRY"
            or approval.get("expected_activation_id") != selection[1].id
            or not approval.get("approval_reference")
            or not approval.get("old_writer_fence_evidence_ids")
            or prior.release.schema_version != successor.schema_version
            or prior.release.config != successor.config
            or prior.release.id == successor.id
            or not 0
            <= (
                store.clocks.sample()[0] - datetime.fromisoformat(approval["approved_at"])
            ).total_seconds()
            <= 120
        ):
            raise StateConflictError("TRANSITION_EXACT_CODE_ONLY_APPROVAL_REQUIRED")
        binding = ExecutableBinding(
            commission_id=commission.id,
            sequence=prior.sequence + 1,
            previous_id=prior.id,
            release=successor,
            approved_at=datetime.fromisoformat(approval["approved_at"]),
            approval_reference=approval["approval_reference"],
            fence_id=event.id,
            activation_id=selection[1].id,
            evidence_ids=tuple(approval["old_writer_fence_evidence_ids"]),
        )
        put(session, "core_runtime_config_history", binding.id, binding)
        pointer = table("core_runtime_current")
        result = session.execute(
            update(pointer).where(pointer.c.document_id == prior.id).values(document_id=binding.id)
        )
        if cast(CursorResult[Any], result).rowcount != 1:
            raise StateConflictError("TRANSITION_STALE_BINDING")
    return binding.id


def resume_entries(store: CoreStore, approval: dict[str, Any]) -> str:
    """Fresh independent readiness authorizes only an explicit exact-current entry fence."""
    from quantbot.paper_core.activation import _boundary, active_in_session
    from quantbot.paper_core.health import entry_health_objections
    from quantbot.paper_core.time_evidence import TimeEvidence
    from quantbot.paper_core.work import current_receipts
    from quantbot.storage import StorageRepository

    with store.database.transaction() as session:
        session.execute(text("BEGIN IMMEDIATE"))
        now = store.clocks.sample()[0]
        commission = commission_in_session(store, session)
        current = current_runtime_event(session)
        selection = active_in_session(store, session)
        if (
            approval.get("commission_id") != commission.id
            or approval.get("expected_fence_id") != current.id
            or current.action != "FENCE_ENTRY"
            or not approval.get("approval_reference")
            or approval.get("runtime_release_id") != store.runtime_release_id
            or selection is None
            or approval.get("expected_activation_id") != selection[1].id
            or not 0
            <= (now - datetime.fromisoformat(approval["approved_at"])).total_seconds()
            <= 120
        ):
            raise StateConflictError("RESUME_EXACT_CURRENT_APPROVAL_REQUIRED")
        if StorageRepository(session).list_unresolved_order_intents():
            raise StateConflictError("RESUME_UNRESOLVED_SUBMISSION")
        receipts = current_receipts(session)
        if not receipts or receipts[0].id != approval.get("guardian_receipt_id"):
            raise StateConflictError("RESUME_FRESH_GUARDIAN_REQUIRED")
        receipt = receipts[0]
        heartbeat_payload = session.execute(
            select(table("core_role_heartbeats").c.payload)
            .join(
                table("core_role_current"),
                table("core_role_current").c.heartbeat_id == table("core_role_heartbeats").c.id,
            )
            .where(table("core_role_current").c.role == "guardian")
        ).scalar_one_or_none()
        heartbeat = (
            RoleHeartbeat.model_validate_json(heartbeat_payload) if heartbeat_payload else None
        )
        if (
            heartbeat is None
            or heartbeat.id != approval.get("guardian_heartbeat_id")
            or guardian_objections(store, session, now=now, receipt_id=receipt.id)
            or not receipt.recorded_at_utc <= now < receipt.valid_until
        ):
            raise StateConflictError("RESUME_FRESH_GUARDIAN_REQUIRED")
        proof = store.latest_proof()
        if (
            proof.id != approval.get("proof_id")
            or not proof.complete
            or not proof.cash_complete
            or proof.ledger_state_id != store.ledger_state_id(session)
            or not 0 <= (now - proof.recorded_at_utc).total_seconds() <= 120
        ):
            raise StateConflictError("RESUME_FRESH_PROOF_REQUIRED")
        time_payload = session.execute(
            select(table("core_time_evidence").c.payload).where(
                table("core_time_evidence").c.id == approval.get("time_evidence_id")
            )
        ).scalar_one_or_none()
        time_proof = TimeEvidence.model_validate_json(time_payload) if time_payload else None
        if (
            time_proof is None
            or time_proof.id not in heartbeat.evidence_ids
            or time_proof.process_id != heartbeat.process_epoch
            or time_proof.objections
            or not time_proof.samples
            or not 0 <= (now - time_proof.wall_anchor).total_seconds() <= 30
            or time_proof.samples[-1].provider.account_id != commission.account_id
            or time_proof.offset_upper - time_proof.offset_lower > 2
            or now + timedelta(seconds=time_proof.offset_upper)
            >= time_proof.samples[-1].provider.session_close
            - timedelta(seconds=store.config.exit_buffer_seconds)
            or now + timedelta(seconds=time_proof.offset_lower)
            < time_proof.samples[-1].provider.session_open
        ):
            raise StateConflictError("RESUME_FRESH_TIME_REQUIRED")
        if entry_health_objections(store, session, store.config.symbol):
            raise StateConflictError("RESUME_MANUAL_STOP_OR_QUARANTINE")
        _boundary(session, cutoff=now)
        evidence_id = content_id(approval)
        put(session, "core_runtime_config_history", evidence_id, approval)
        event = RuntimeEvent(
            commission_id=commission.id,
            sequence=current.sequence + 1,
            at=now,
            action="RESUME_ENTRY",
            evidence_ids=(
                current.id,
                evidence_id,
                proof.id,
                receipt.id,
                heartbeat.id,
                time_proof.id,
            ),
            reason=approval["approval_reference"],
        )
        put(session, "core_runtime_events", event.id, event)
        return event.id
