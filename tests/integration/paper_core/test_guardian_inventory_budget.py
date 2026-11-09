"""Guardian flat polling respects the existing protection request reserve."""

import sqlite3
from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare

from quantbot.paper_core import account_control as control
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.roles import run_role
from quantbot.research.manifest import content_id


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_guardian_does_not_exhaust_protection_reserve(tmp_path, monkeypatch, unknown):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        if unknown:
            from quantbot.domain import OrderIntent
            from quantbot.storage import StorageRepository

            with db.transaction() as session:
                StorageRepository(session).create_order_intent(
                    OrderIntent(
                        intent_id="unknown",
                        client_order_id="unknown-client",
                        strategy_id="test",
                        signal_date=clocks.wall.date(),
                        symbol="AAPL",
                        side="BUY",
                        order_type="MARKET",
                        time_in_force="DAY",
                        quantity="1",
                        state="SUBMISSION_UNKNOWN",
                        created_at=clocks.wall,
                    )
                )
        path = tmp_path / "budget.db"
        control.initialize_account_control(path)
        monkeypatch.setattr(control, "observed_host_identity", lambda: "h" * 64)
        monkeypatch.setattr(control, "observed_host_epoch", lambda: "e" * 64)
        config = control.AccountRequestControl(
            control_path=str(path),
            account_scope="a" * 64,
            host_identity="h" * 64,
            host_epoch="e" * 64,
        )
        wrapped = control.BudgetedTransport(provider, config, "guardian", clocks)
        broker.adapter._transport = wrapped
        # Earlier recovery work already consumed part of this rolling minute.
        for _ in range(10):
            await broker.get_account()
        # Other independent roles retain their own existing account allowances.
        trader = control.BudgetedTransport(provider, config, "trader", clocks)
        outcomes = control.BudgetedTransport(provider, config, "outcomes", clocks)
        for transport in (trader, outcomes):
            await transport.request("GET", "https://paper-api.alpaca.markets/v2/positions")

        async def advance(seconds):
            clocks.advance(seconds)

        monkeypatch.setattr("quantbot.paper_core.roles.asyncio.sleep", advance)
        before = {content_id(r) for r in store.documents("core_cycle_receipts")}
        await run_role(store, broker, broker.config, "guardian", cycles=4)
        receipts = [
            r for r in store.documents("core_cycle_receipts") if content_id(r) not in before
        ]
        assert not any("INVENTORY_ACQUISITION_RETRYABLE" in r["reasons"] for r in receipts)
        assert len({r["proof_id"] for r in receipts if r["proof_id"]}) == 2
        pending = [r for r in receipts if "INVENTORY_REFRESH_PENDING" in r["reasons"]]
        assert len(pending) == (2 if unknown else 0)
        assert all(r["proof_id"] is None for r in pending)
        with sqlite3.connect(path) as c:
            assert c.execute("SELECT count(*) FROM refusals").fetchone()[0] == 0
        # Noncritical work still cannot consume the independent owned-exit reserve.
        budget = wrapped.budgets["trading"]
        while budget.acquire("guardian", purpose="RECOVERY GET", now_monotonic=clocks.mono):
            pass
        assert budget.acquire("guardian", purpose="OWNED_EXIT GET", now_monotonic=clocks.mono)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    ["manual_stop", "health", "certification", "revocation", "pending", "wall_jump", "deadline"],
)
async def test_flat_hint_invalidates_on_authority_or_work_change(tmp_path, change):
    from datetime import timedelta

    from quantbot.domain import OrderIntent
    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.health import report
    from quantbot.paper_core.roles import FlatInventoryPoll
    from quantbot.paper_core.store import table
    from quantbot.storage import StorageRepository

    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        poll = FlatInventoryPoll()
        poll.observe(store, broker, receipt)
        assert poll.current(store) == receipt.proof_id
        if change == "manual_stop":
            with db.transaction() as session:
                StorageRepository(session).set_kill_switch(
                    True, reason="test", updated_at=clocks.wall
                )
        elif change == "health":
            report(
                store,
                role="GUARDIAN",
                state="INTEGRITY_FAILURE",
                reasons=("INVENTORY_MISMATCH",),
                now=clocks.wall,
            )
        elif change == "certification":
            await synchronize(store, broker, now=clocks.wall)
        elif change == "revocation":
            with db.transaction() as session:
                session.execute(
                    table("core_cash_authority_revocations")
                    .insert()
                    .values(snapshot_id="core-certified-" + receipt.proof_id, reason="test")
                )
        elif change == "pending":
            with db.transaction() as session:
                StorageRepository(session).create_order_intent(
                    OrderIntent(
                        intent_id="pending",
                        client_order_id="pending-client",
                        strategy_id="test",
                        signal_date=clocks.wall.date(),
                        symbol="AAPL",
                        side="BUY",
                        order_type="MARKET",
                        time_in_force="DAY",
                        quantity="1",
                        state="SUBMISSION_UNKNOWN",
                        created_at=clocks.wall,
                    )
                )
        elif change == "wall_jump":
            clocks.wall += timedelta(seconds=1)
        else:
            clocks.advance(15)
        assert poll.current(store) is None
        assert not FlatInventoryPoll().current(store), "Restart always reacquires"
    finally:
        db.close()
