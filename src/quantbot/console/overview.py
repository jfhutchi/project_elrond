"""Account-scoped read model for the workspace; no broker or execution capability."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.domain import Position
from quantbot.paper_core.authority import RoleHeartbeat, RuntimeCommission
from quantbot.storage import StorageRepository
from quantbot.storage.schema import metadata


class RoleStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: str
    status: str
    recorded_at: datetime | None = None
    reason: str | None = None


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
        ):
            heartbeat = None
        if heartbeat is None:
            roles.append(RoleStatus(role=role, status="Not started"))
            continue
        status = "Reporting"
        if not heartbeat.recorded_at <= now < heartbeat.valid_until:
            status = "Stale"
        elif heartbeat.objections or heartbeat.last_useful_progress is None:
            status = "Waiting"
        # Provider exception text may contain identity or configuration inputs. Only
        # fixed display messages cross the browser boundary, never raw error strings.
        messages = {
            "CORE_INDEPENDENT_GUARDIAN_NOT_READY": "Waiting for guardian readiness",
            "CURRENT_PROOF_UNAVAILABLE": "Waiting for reconciled inventory",
            "CORE_MANUAL_ENTRY_PAUSE": "Manual entry pause is engaged",
            "MARKET_CLOSED": "Market is closed",
        }
        reason = (
            messages.get(heartbeat.first_blocker, "A readiness check is waiting")
            if heartbeat.first_blocker
            else None
        )
        roles.append(
            RoleStatus(role=role, status=status, recorded_at=heartbeat.recorded_at, reason=reason)
        )
    return base.model_copy(update={"core_phase": "Commissioned", "roles": tuple(roles)})
