"""Prospective native PAPER close evidence; current balances, never historical cash repair."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from collections.abc import Callable
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers.base import PaperPerformanceAccount
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.authority import (
    RoleHeartbeat,
    commission_in_session,
    executable_in_session,
)
from quantbot.paper_core.contracts import CoreConfig, CycleReceipt, ReconciliationProof
from quantbot.paper_core.funds import PaperFundsEvidence, entry_inventory_objections
from quantbot.paper_core.health import active_health
from quantbot.paper_core.store import CoreStore, latest_certification, table
from quantbot.research.manifest import content_id
from quantbot.storage import Database, StorageRepository


def _guardian_certificate(
    store: CoreStore,
    session: Session,
    proof: ReconciliationProof,
    funds: PaperFundsEvidence,
    *,
    close_at: datetime,
    captured_at: datetime,
) -> tuple[RoleHeartbeat, CycleReceipt] | None:
    # Account-wide accounting cannot certify through a persistent pause/quarantine,
    # even when flatness lets the Guardian's owned-lot checks have no work to do.
    if StorageRepository(session).get_kill_switch_state().engaged or any(
        health.scope != "LEARNING" and health.state in {"INTEGRITY_FAILURE", "MANUAL_PAUSE"}
        for health in active_health(session)
    ):
        return None
    raw = session.execute(
        select(table("core_role_heartbeats").c.payload)
        .join(
            table("core_role_current"),
            table("core_role_current").c.heartbeat_id == table("core_role_heartbeats").c.id,
        )
        .where(table("core_role_current").c.role == "guardian")
    ).scalar_one_or_none()
    if raw is None:
        return None
    hb = RoleHeartbeat.model_validate_json(raw)
    if (
        hb.role != "guardian"
        or hb.pid == os.getpid()
        or hb.process_epoch == store.clocks.process_id
        or funds.process_id != hb.process_epoch
        or hb.release_id != store.runtime_release_id
        or hb.config_id != store.config_id
        or hb.store_identity != store.database.store_identity
        or hb.owner != store.config.owner
        or hb.generation != store.config.generation
        or not close_at
        <= funds.capture_started
        <= proof.recorded_at_utc
        <= hb.recorded_at
        <= captured_at
        < hb.valid_until
        or hb.last_useful_progress is None
        or not proof.recorded_at_utc <= hb.last_useful_progress <= captured_at
        or not 0 <= (captured_at - hb.last_useful_progress).total_seconds() <= 30
        or captured_at - proof.recorded_at_utc > timedelta(minutes=2)
        or set(hb.objections) - {"SESSION_OPENING_UNVERIFIED"}
    ):
        return None
    for identity in hb.evidence_ids:
        raw = session.execute(
            select(table("core_cycle_receipts").c.payload).where(
                table("core_cycle_receipts").c.id == identity
            )
        ).scalar_one_or_none()
        if raw is None:
            continue
        cycle = CycleReceipt.model_validate_json(raw)
        if (
            cycle.proof_id == proof.id
            and cycle.release_id == store.config.release_id
            and cycle.evidence_lane == "BROKER_PAPER"
            and proof.recorded_at_utc <= cycle.recorded_at_utc <= hb.recorded_at
            and not set(cycle.reasons) - {"SESSION_OPENING_UNVERIFIED"}
        ):
            return hb, cycle
    return None


async def wait_for_close_certification(
    store: CoreStore,
    *,
    close_at: datetime,
    deadline: datetime,
) -> None:
    """Consume independent post-close authority; never acquire it with competing writes."""
    while close_at <= (now := store.clocks.sample()[0]) < deadline:
        with Database.read_only(store.database.path) as session:
            latest = latest_certification(session)
            if latest is not None:
                proof = ReconciliationProof.model_validate_json(
                    session.execute(
                        select(table("core_reconciliation_proofs").c.payload).where(
                            table("core_reconciliation_proofs").c.id == latest[1]
                        )
                    ).scalar_one()
                )
                raw = session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == proof.acquisition_id
                    )
                ).scalar_one_or_none()
                if raw is not None:
                    funds = PaperFundsEvidence.model_validate_json(raw)
                    if (
                        not funds.first_open_orders
                        and not funds.second_open_orders
                        and not entry_inventory_objections(store, session, proof, now=now)
                        and _guardian_certificate(
                            store, session, proof, funds, close_at=close_at, captured_at=now
                        )
                        is not None
                    ):
                        return
        # The read transaction is closed before yielding to any other role/work.
        await asyncio.sleep(0.5)
    raise TimeoutError("FRESH_INDEPENDENT_CLOSE_CERTIFICATION_REQUIRED")


def configured_directory(reference: str | None, operational: Path) -> Path | None:
    """Read only existing path settings; never interpolate or export dotenv secrets."""
    if reference is None:
        return None
    source = Path(reference)
    if not source.is_absolute():
        raise ValueError("absolute environment reference required")
    names = {"QUANTBOT_DAILY_PERFORMANCE_DIRECTORY", "QUANTBOT_LEARNER_DIRECTORY"}
    selected = []
    seen = set()
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        key = line.split("=", 1)[0].strip().removeprefix("export ").strip()
        if key in names:
            if key in seen:
                raise ValueError("duplicate performance path setting")
            seen.add(key)
            selected.append(line)
    values = dotenv_values(stream=StringIO("\n".join(selected)), interpolate=False)
    paths = []
    for key in names:
        value = values.get(key)
        if not value:
            continue
        path = Path(value)
        if not path.is_absolute() or "${" in value:
            raise ValueError("explicit absolute performance path required")
        paths.append(
            (path / "performance" if key == "QUANTBOT_LEARNER_DIRECTORY" else path).resolve()
        )
    if not paths:
        return None
    if any(path != paths[0] for path in paths):
        raise ValueError("conflicting performance paths")
    directory = paths[0]
    if directory.is_file() or operational.resolve() in {
        directory,
        (directory / "baselines.db").resolve(),
        (directory / "closes.db").resolve(),
    }:
        raise ValueError("performance store aliases operational store")
    return directory


def native_reconciled(
    store: CoreStore,
    operational: Path,
    account: PaperPerformanceAccount,
    *,
    close_at: datetime,
    captured_at: datetime,
) -> tuple[bool, str, dict[str, str]]:
    """Validate the native counterpart of legacy quantity/current-balance reconciliation.

    Quantity-only certification is permitted with its authenticated stable funds source.
    This does not certify historical independent cash completeness or exact fill fees.
    """
    epoch = hashlib.sha256(
        json.dumps(
            [
                store.database.store_identity,
                account.account.account_id,
                account.created_at.isoformat(),
            ]
        ).encode()
    ).hexdigest()
    refused: tuple[bool, str, dict[str, str]] = (False, epoch, {})
    try:
        if (
            operational.resolve() != Path(store.database.path).resolve()
            or store.config.evidence_lane != "BROKER_PAPER"
            or store.config.clock_profile != "PAPER_TIME_V2"
            or account.account.account_id != store.config.account_id
            or not close_at <= captured_at <= close_at + timedelta(minutes=5)
        ):
            return refused
        with Database.read_only(operational) as session:
            identity = session.execute(
                select(table("schema_version").c.store_identity)
            ).scalar_one()
            rows = session.execute(select(table("core_identities"))).mappings().all()
            if (
                identity != store.database.store_identity
                or len(rows) != 1
                or rows[0]["id"] != store.config_id
                or CoreConfig.model_validate_json(rows[0]["payload"]) != store.config
            ):
                return refused
            commission = commission_in_session(store, session)
            executable = executable_in_session(session)
            latest = latest_certification(session)
            if latest is None:
                return refused
            proof = ReconciliationProof.model_validate_json(
                session.execute(
                    select(table("core_reconciliation_proofs").c.payload).where(
                        table("core_reconciliation_proofs").c.id == latest[1]
                    )
                ).scalar_one()
            )
            if (
                proof.id != latest[1]
                or not close_at <= proof.recorded_at_utc <= captured_at
                or captured_at - proof.recorded_at_utc > timedelta(minutes=2)
            ):
                return refused
            funds = PaperFundsEvidence.model_validate_json(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == proof.acquisition_id
                    )
                ).scalar_one()
            )
            # Initial accounting scope excludes working orders. Besides avoiding
            # economic ambiguity this keeps the existing funds predicate inside
            # this snapshot: its nonempty-order path reads policy in another session.
            if (
                funds.first_open_orders
                or funds.second_open_orders
                or not close_at <= funds.capture_started <= proof.recorded_at_utc
                or entry_inventory_objections(store, session, proof, now=captured_at)
            ):
                return refused
            guardian = _guardian_certificate(
                store, session, proof, funds, close_at=close_at, captured_at=captured_at
            )
            if guardian is None:
                return refused
            repo = StorageRepository(session)
            if any(
                i.state.value in {"SUBMITTING", "SUBMISSION_UNKNOWN"}
                for i in repo.list_unresolved_order_intents()
            ):
                return refused
            for incident in repo.list_incidents(unresolved_only=True):
                if incident.kind == "CORE_EVIDENCE_CONFLICT":
                    authorities = incident.detail.get("disputed_authorities")
                    if (
                        incident.detail.get("account_scope")
                        or not isinstance(authorities, list)
                        or not all(isinstance(authority, str) for authority in authorities)
                        or set(authorities) != {"OUTCOME"}
                    ):
                        return refused
            snapshot_id = (
                "core-certified-" if proof.cash_complete else "core-quantity-"
            ) + proof.id
            snapshot = (
                session.execute(
                    select(table("account_snapshots")).where(
                        table("account_snapshots").c.snapshot_id == snapshot_id
                    )
                )
                .mappings()
                .one()
            )
            if (
                snapshot["account_id"] != account.account.account_id
                or datetime.fromisoformat(snapshot["captured_at"]) != proof.recorded_at_utc
                or proof.account.cash != account.account.cash
                or proof.account.equity != account.account.equity
                or proof.account.currency != "USD"
                or account.account.currency != "USD"
                or str(proof.account.cash) != snapshot["cash"]
                or str(proof.account.equity) != snapshot["equity"]
                or snapshot["currency"] != "USD"
            ):
                return refused
            provenance = dict(
                contract="NATIVE_CORE_CURRENT_BALANCE_CLOSE_V1",
                config_id=store.config_id,
                commission_id=commission.id,
                executable_id=executable.id if executable else None,
                proof_id=proof.id,
                funds_id=funds.id,
                snapshot=dict(snapshot),
                guardian_heartbeat_id=guardian[0].id,
                guardian_cycle_id=guardian[1].id,
            )
            return (
                True,
                epoch,
                dict(
                    id=(
                        "native-core-close-v1:"
                        + proof.id
                        + ":funds:"
                        + funds.id
                        + ":guardian:"
                        + guardian[0].id
                        + ":cycle:"
                        + guardian[1].id
                    ),
                    completed_at=proof.recorded_at_utc.isoformat(),
                    snapshot_hash=content_id(provenance),
                ),
            )
    except Exception:
        # Malformed/missing native authority never falls back to a legacy status row.
        return refused


def close_deadline(directory: Path, now: datetime) -> datetime | None:
    """An authoritative retained session gates all prospective provider work."""
    from quantbot.console.performance_reader import CalendarCache, exchange_today

    calendar = CalendarCache.model_validate_json((directory / "calendar.json").read_bytes())
    today = exchange_today(now)
    if not calendar.start <= today <= calendar.end:
        return None
    session = next((s for s in calendar.sessions if s.session_date == today), None)
    if session is not None and session.close_at <= now <= session.close_at + timedelta(minutes=5):
        return session.close_at + timedelta(minutes=5)
    return None


def close_due(directory: Path, now: datetime) -> bool:
    return close_deadline(directory, now) is not None


async def capture_core_close(
    store: CoreStore,
    broker: AlpacaCoreBroker,
    directory: Path,
    *,
    stage: Callable[[str], None] | None = None,
) -> dict[str, str] | None:
    """One existing outcomes-loop opportunity, using retained authoritative session timing."""
    from quantbot.operations.close_capture import capture_close

    now = store.clocks.sample()[0]
    deadline = close_deadline(directory, now)
    if deadline is None:
        return None
    observed = store.clocks.sample()[0]
    if observed < now or observed >= deadline:
        return None

    async def prepare() -> None:
        if stage is not None:
            stage("WAIT_GUARDIAN_CERTIFICATION")
        await wait_for_close_certification(
            store, close_at=deadline - timedelta(minutes=5), deadline=deadline
        )

    async with asyncio.timeout(min(45, (deadline - observed).total_seconds())):
        return await capture_close(
            directory,
            Path(store.database.path),
            broker.adapter,
            broker.calendar,
            expected_account_id=store.config.account_id,
            now=now,
            clock=lambda: store.clocks.sample()[0],
            prepare_reconciliation=prepare,
            broker_close_reference=True,
            core_store=store,
            stage=stage,
        )


class OutcomesCloseCapture:
    """At most one bounded task; existing outcomes polls supply opportunities."""

    def __init__(self, store: CoreStore, broker: AlpacaCoreBroker, directory: Path) -> None:
        self.store, self.broker, self.directory = store, broker, directory
        self.task: asyncio.Task[dict[str, str] | None] | None = None
        self.completed_session: str | None = None
        self.stage = "IDLE"

    def _set_stage(self, name: str) -> None:
        self.stage = name

    def needs_provider_lane(self) -> bool:
        """Coordinate this role's reads only; never alters other roles or request limits."""
        from quantbot.console.performance_reader import exchange_today

        if self.task is not None and not self.task.done():
            return True
        now = self.store.clocks.sample()[0]
        try:
            return self.completed_session != str(exchange_today(now)) and close_due(
                self.directory, now
            )
        except (OSError, ValueError):
            # An unusable accounting calendar grants no close attempt and must not
            # suppress ordinary provider recovery/learning indefinitely.
            return False

    def poll(self) -> None:
        try:
            if self.task is not None:
                if not self.task.done():
                    return
                try:
                    result = self.task.result()
                    if result is not None:
                        print(json.dumps(dict(event="PAPER_CLOSE_CAPTURE", **result)), flush=True)
                        if result["status"] == "FINAL" or result["reason"] == "BASELINE_MISSING":
                            self.completed_session = result["session_date"]
                finally:
                    self.task = None
            from quantbot.console.performance_reader import exchange_today

            now = self.store.clocks.sample()[0]
            if self.completed_session != str(exchange_today(now)) and close_due(
                self.directory, now
            ):
                self.task = asyncio.create_task(
                    capture_core_close(
                        self.store, self.broker, self.directory, stage=self._set_stage
                    )
                )
        except Exception as error:
            # Accounting diagnostics never become entry/protection authority.
            original = getattr(error, "orig", error)
            statement = str(getattr(error, "statement", "")).lstrip().upper()
            category = (
                "BEGIN_IMMEDIATE"
                if statement.startswith("BEGIN IMMEDIATE")
                else "READ"
                if statement.startswith("SELECT")
                else "WRITE"
                if statement.startswith(("INSERT", "UPDATE", "DELETE"))
                else "UNKNOWN"
            )
            print(
                json.dumps(
                    dict(
                        event="PAPER_CLOSE_CAPTURE_UNAVAILABLE",
                        error=type(error).__name__,
                        error_module=type(error).__module__,
                        stage=self.stage,
                        sqlite_errorcode=getattr(original, "sqlite_errorcode", None),
                        sqlite_errorname=getattr(original, "sqlite_errorname", None),
                        statement_category=category,
                    )
                ),
                flush=True,
            )

    async def aclose(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
