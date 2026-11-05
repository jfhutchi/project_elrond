"""Account-scoped read model for the workspace; no broker or execution capability."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import Position
from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission, executable_in_session
from quantbot.storage import StorageRepository
from quantbot.storage.schema import metadata

# Only reviewed constants cross the browser boundary. A code-shaped string is
# not sufficient: first_blocker can also contain exception text or credentials.
BLOCKER_DETAILS = {
    "CORE_INDEPENDENT_GUARDIAN_NOT_READY": (
        "Waiting for guardian readiness",
        "Inspect current guardian evidence and its blockers.",
    ),
    "CURRENT_PROOF_UNAVAILABLE": (
        "Waiting for reconciled inventory",
        "Inspect inventory reconciliation receipts.",
    ),
    "CORE_MANUAL_ENTRY_PAUSE": (
        "Manual entry pause is engaged",
        "Preserve the stop until audited resume is authorized.",
    ),
    "CORE_ENTRY_FENCED": (
        "Entry is fenced",
        "Inspect the runtime transition receipt; resume only through audited readiness checks.",
    ),
    "INVENTORY_ACQUISITION_RETRYABLE": (
        "Inventory acquisition is retrying",
        "Inspect provider retry receipts; retain independent protection and current safety limits.",
    ),
    "INVENTORY_PROTOCOL_INVALID": (
        "Inventory evidence failed protocol validation",
        "Inspect private validation receipts; do not treat invalid evidence as reconciled.",
    ),
    "SESSION_OPENING_UNVERIFIED": (
        "Session opening evidence is unverified",
        "Inspect the session-opening and time-proof receipts before relying on session authority.",
    ),
    "MARKET_CLOSED": (
        "Market is closed",
        "Entries wait for an open session; check guardian and outcomes separately.",
    ),
    "CORE_PROTECTION_DEADLINE_DUE": (
        "A protection deadline is due",
        "Inspect the guardian's owned exit and submission evidence.",
    ),
    "CORE_GUARDIAN_RECEIPT_EXPIRED": (
        "Guardian readiness evidence expired",
        "Inspect current guardian heartbeat and receipts.",
    ),
    "CORE_INVENTORY_AUTHORITY_UNAVAILABLE": (
        "Inventory authority is unavailable",
        "Inspect current ownership and quantity proof.",
    ),
    "CORE_CASH_AUTHORITY_UNAVAILABLE": (
        "Cash authority is unavailable",
        "Inspect the commissioned funds evidence and its blockers.",
    ),
    "CORE_COMPETING_OPEN_ORDER": (
        "A competing open order blocks entry",
        "Reconcile order ownership before any new entry.",
    ),
    "CORE_UNEXPLAINED_OPEN_ORDER": (
        "An unexplained open order blocks entry",
        "Reconcile order ownership before any new entry.",
    ),
    "CORE_ENTRY_DECISION_EXPIRED": (
        "Entry decision expired",
        "Inspect the next decision and fresh evidence; do not reuse this one.",
    ),
    "PAPER_CLOSE_CAPTURE_PENDING": (
        "Daily close capture is pending",
        "Inspect close-capture receipts; final daily net remains pending.",
    ),
}
TIME_BLOCKERS = frozenset(
    {
        "TIME_EVIDENCE_EXPIRED",
        "TIME_EVIDENCE_MISSING",
        "TIME_ACQUISITION_RETRYABLE",
        "TIME_ACQUISITION_BUDGET",
        "TIME_PROTOCOL_INVALID",
        "TIME_DISCONTINUITY",
        "TIME_SKEW_OUT_OF_BOUNDS",
        "TIME_SAMPLES_INCOMPLETE",
        "TIME_SAMPLES_NOT_ADVANCING",
        "TIME_IDENTITY_CHANGED",
        "TIME_SAMPLES_DISAGREE",
        "TIME_PROCESS_CHANGED",
        "TIME_PROJECTION_INVALID",
        "TIME_ACCOUNT_MISMATCH",
        "TIME_SESSION_BOUNDARY",
        "TIME_CALENDAR_MISMATCH",
        "TIME_OBSERVATION_BOUNDARY",
    }
)


class RoleStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: str
    status: str
    recorded_at: datetime | None = None
    reason: str | None = None
    blocker_code: str | None = None
    action: str | None = None
    heartbeat_age_seconds: float | None = None
    valid_until: datetime | None = None
    expired: bool | None = None
    last_useful_progress_at: datetime | None = None
    progress_age_seconds: float | None = None


class OverviewPanel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    snapshot_at: datetime | None = None
    positions: tuple[Position, ...] | None = None
    core_phase: str = "Not commissioned"
    roles: tuple[RoleStatus, ...] = ()


def read_overview(session: Session, *, account_id: str | None, now: datetime) -> OverviewPanel:
    snapshot = (
        StorageRepository(session).get_latest_account_snapshot(account_id) if account_id else None
    )
    base = OverviewPanel(
        snapshot_at=snapshot.captured_at if snapshot else None,
        positions=snapshot.positions if snapshot else None,
    )
    if not account_id:
        return base
    commissions = metadata.tables["core_runtime_commissions"]
    payloads = session.execute(select(commissions.c.payload).limit(2)).scalars().all()
    if not payloads:
        return base
    if len(payloads) != 1:
        return base.model_copy(update={"core_phase": "Evidence unavailable"})
    try:
        commission = RuntimeCommission.model_validate_json(payloads[0])
        if commission.account_id != account_id:
            return base.model_copy(update={"core_phase": "Account mismatch"})
        binding = executable_in_session(session)
        if binding and binding.commission_id != commission.id:
            return base.model_copy(update={"core_phase": "Evidence unavailable"})
        if (
            binding is None
            and session.execute(
                select(metadata.tables["core_runtime_config_history"].c.id).limit(1)
            ).first()
        ):
            return base.model_copy(update={"core_phase": "Evidence unavailable"})
        release_id = binding.release.id if binding else commission.release_id
        current = metadata.tables["core_role_current"]
        heartbeats = metadata.tables["core_role_heartbeats"]
        records = {
            h.role: h
            for h in (
                RoleHeartbeat.model_validate_json(raw)
                for raw in session.execute(
                    select(heartbeats.c.payload).join(
                        current, current.c.heartbeat_id == heartbeats.c.id
                    )
                ).scalars()
            )
        }
    except ValueError:
        return base.model_copy(update={"core_phase": "Evidence unavailable"})
    roles = []
    for role in ("trader", "guardian", "outcomes"):
        heartbeat = records.get(role)
        if heartbeat and (
            heartbeat.config_id != commission.config_id
            or heartbeat.store_identity != commission.store_identity
            or heartbeat.owner != commission.owner
            or heartbeat.generation != commission.generation
            or heartbeat.release_id != release_id
        ):
            roles.append(
                RoleStatus(
                    role=role,
                    status="Unverified",
                    recorded_at=heartbeat.recorded_at,
                    reason="Heartbeat does not match the commissioned runtime",
                )
            )
            continue
        if heartbeat is None:
            roles.append(RoleStatus(role=role, status="Not started"))
            continue
        status = "Reporting"
        if not heartbeat.recorded_at <= now < heartbeat.valid_until:
            status = "Stale"
        elif heartbeat.objections or heartbeat.last_useful_progress is None:
            status = "Waiting"
        blocker = heartbeat.first_blocker or next(iter(heartbeat.objections), None)
        # roles.claim_objections appends a private manual-stop reason. Normalize
        # only this documented prefix; never render or generalize its suffix.
        if blocker is not None and blocker.startswith("CORE_MANUAL_ENTRY_PAUSE:"):
            blocker = "CORE_MANUAL_ENTRY_PAUSE"
        code = reason = action = None
        if blocker is not None and blocker in BLOCKER_DETAILS:
            code = blocker
            reason, action = BLOCKER_DETAILS[blocker]
        elif blocker in TIME_BLOCKERS:
            code = blocker
            reason = "Current time evidence is unavailable or invalid"
            action = "Inspect time-proof receipts and clock health; preserve timing limits."
        elif blocker:
            code = "UNRECOGNIZED_BLOCKER"
            reason = "Recorded blocker details are withheld from the dashboard"
            action = "Inspect private role receipts; preserve current safety controls."
        elif heartbeat.last_useful_progress is None:
            reason = "No useful progress has been recorded"
            action = "Inspect the role's recent receipts and scheduler state."
        progress = heartbeat.last_useful_progress
        roles.append(
            RoleStatus(
                role=role,
                status=status,
                recorded_at=heartbeat.recorded_at,
                reason=reason,
                blocker_code=code,
                action=action,
                heartbeat_age_seconds=(now - heartbeat.recorded_at).total_seconds()
                if heartbeat.recorded_at <= now
                else None,
                valid_until=heartbeat.valid_until,
                expired=now >= heartbeat.valid_until,
                last_useful_progress_at=progress,
                progress_age_seconds=(now - progress).total_seconds()
                if progress is not None and progress <= now
                else None,
            )
        )
    return base.model_copy(update={"core_phase": "Commissioned", "roles": tuple(roles)})
