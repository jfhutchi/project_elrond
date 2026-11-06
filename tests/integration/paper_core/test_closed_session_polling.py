"""Offline provider protocol: closed equities polling must not mint entry authority."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from tests.integration.paper_core.test_paper_funds import FundsProvider, prepare

from quantbot.paper_core import roles
from quantbot.paper_core.authority import RoleHeartbeat, guardian_objections, publish_heartbeat
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.store import table
from quantbot.storage import StorageRepository


class SessionProvider(FundsProvider):
    async def request(self, method, url, **kwargs):
        response = await super().request(method, url, **kwargs)
        if url.endswith("/clock"):
            payload = json.loads(response.content)
            opening = self.now.replace(hour=13, minute=30, second=0, microsecond=0)
            closing = opening.replace(hour=20, minute=0)
            payload["is_open"] = opening <= self.now < closing
            return response.model_copy(update={"content": json.dumps(payload).encode()})
        return response


def count(db, name):
    with db.transaction() as session:
        return session.scalar(select(func.count()).select_from(table(name)))


async def fixture(tmp_path, at):
    return await prepare(
        tmp_path,
        provider_factory=SessionProvider,
        initial_wall=at,
        warmup_seconds=0,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
    )


@pytest.mark.asyncio
async def test_closed_idle_admission_uses_real_fresh_proof(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle

    db, store, broker, _, clocks = await fixture(tmp_path, datetime(2026, 10, 14, 22, tzinfo=UTC))
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert broker.closed_session_window is not None
        poll = roles.ClosedSessionPoll()
        poll.observe(store, broker, receipt)
        assert poll.waiting(store), (receipt, poll.__dict__)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_historical_inventory_failure_needs_new_certification_before_idle(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.health import active_health, report

    db, store, broker, _, clocks = await fixture(tmp_path, datetime(2026, 10, 14, 22, tzinfo=UTC))
    try:
        store.runtime_role = "guardian"
        report(
            store,
            role="GUARDIAN",
            state="PROTOCOL_FAILURE",
            reasons=("INVENTORY_PROTOCOL_INVALID",),
            now=clocks.wall,
        )
        assert roles._idle_signature(store) is None
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        poll = roles.ClosedSessionPoll()
        poll.observe(store, broker, receipt)
        assert poll.waiting(store), "A new verified reconciliation permits reduced polling only"
        with db.transaction() as session:
            assert not any(h.state == "PROTOCOL_FAILURE" for h in active_health(session))
        assert any(h.get("recovered_from") for h in store.documents("core_health_events"))
        report(
            store,
            role="GUARDIAN",
            state="PROTOCOL_FAILURE",
            reasons=("INVENTORY_PROTOCOL_INVALID",),
            now=clocks.wall,
        )
        assert not poll.waiting(store), "A current failure must resume rapid recovery"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_closed_settled_polling_is_bounded_and_restart_reconciles(tmp_path, monkeypatch):
    db, store, broker, provider, clocks = await fixture(
        tmp_path, datetime(2026, 10, 14, 22, tzinfo=UTC)
    )

    async def advance(seconds):
        clocks.advance(seconds)

    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    config = broker.config.model_copy(update={"guardian_poll_seconds": 15})
    try:
        proofs = count(db, "core_reconciliation_proofs")
        await roles.run_role(store, broker, config, "guardian", cycles=121)
        assert count(db, "core_reconciliation_proofs") - proofs <= 3
        assert count(db, "core_role_heartbeats") <= 12
        assert not provider.orders
        with db.transaction() as session:
            docs, current = table("core_role_heartbeats"), table("core_role_current")
            raw = session.scalar(
                select(docs.c.payload).join(current, docs.c.id == current.c.heartbeat_id)
            )
        heartbeat = RoleHeartbeat.model_validate_json(raw)
        assert heartbeat.objections == ("CLOSED_SESSION_IDLE",)
        publish_heartbeat(
            store,
            heartbeat.model_copy(
                update={"pid": os.getpid() + 1, "process_epoch": "independent-idle-guardian"}
            ),
        )
        with db.transaction() as session:
            assert guardian_objections(store, session, now=clocks.wall)
        proofs = count(db, "core_reconciliation_proofs")
        await roles.run_role(store, broker, config, "guardian", cycles=1)
        assert count(db, "core_reconciliation_proofs") == proofs + 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_outcomes_defers_provider_but_keeps_learning_and_durable_processing(
    tmp_path, monkeypatch
):
    db, store, broker, provider, clocks = await fixture(
        tmp_path, datetime(2026, 10, 14, 22, tzinfo=UTC)
    )

    async def advance(seconds):
        clocks.advance(seconds)

    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    learned = []

    async def consider(*args):
        learned.append(True)
        return ("SESSION_BUDGET_OR_INPUT_REUSED",)

    monkeypatch.setattr(roles, "daily_consideration", consider)
    try:
        await roles.run_role(store, broker, broker.config, "guardian", cycles=2)
        calls = len(provider.calls)
        await roles.run_role(store, broker, broker.config, "outcomes", cycles=1)
        assert len(provider.calls) == calls
        assert learned, "Learning remains scheduled during the reconciliation wait"
        assert not provider.orders
    finally:
        db.close()


@pytest.mark.asyncio
async def test_preopen_wake_uses_monotonic_deadline_without_extending_evidence(tmp_path):
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.time_evidence import TimeEvidence, evidence_objections

    db, store, broker, _, clocks = await fixture(
        tmp_path, datetime(2026, 10, 14, 13, 20, tzinfo=UTC)
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        poll = roles.ClosedSessionPoll()
        poll.observe(store, broker, receipt)
        assert poll.waiting(store)
        original = store.runtime_time_evidence_id
        clocks.advance(300)
        assert poll.waiting(store)
        assert "TIME_EVIDENCE_EXPIRED" in evidence_objections(
            store.read("core_time_evidence", original, TimeEvidence), clocks
        )
        clocks.advance(181)
        assert not poll.waiting(store)
        assert store.runtime_time_evidence_id == original
        assert clocks.wall.hour == 13 and clocks.wall.minute == 28
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("hour,minute", [(20, 1), (13, 29), (14, 0)])
async def test_close_capture_preopen_and_open_keep_normal_cadence(
    tmp_path, monkeypatch, hour, minute
):
    db, store, broker, _, clocks = await fixture(
        tmp_path, datetime(2026, 10, 14, hour, minute, tzinfo=UTC)
    )

    async def advance(seconds):
        clocks.advance(seconds)

    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    try:
        proofs = count(db, "core_reconciliation_proofs")
        await roles.run_role(store, broker, broker.config, "guardian", cycles=3)
        assert count(db, "core_reconciliation_proofs") == proofs + 3
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["stop", "wall_jump"])
async def test_idle_wakes_on_control_change_or_wall_discontinuity(tmp_path, monkeypatch, change):
    db, store, broker, _, clocks = await fixture(tmp_path, datetime(2026, 10, 14, 22, tzinfo=UTC))
    sleeps = 0

    async def advance(seconds):
        nonlocal sleeps
        sleeps += 1
        clocks.advance(seconds)
        if sleeps == 2:
            if change == "wall_jump":
                clocks.wall += timedelta(seconds=10)
            else:
                with db.transaction() as session:
                    StorageRepository(session).set_kill_switch(
                        True, reason="TEST_STOP", updated_at=clocks.wall
                    )

    monkeypatch.setattr(roles.asyncio, "sleep", advance)
    try:
        proofs = count(db, "core_reconciliation_proofs")
        await roles.run_role(store, broker, broker.config, "guardian", cycles=3)
        assert count(db, "core_reconciliation_proofs") == proofs + 2
    finally:
        db.close()
