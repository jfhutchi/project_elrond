"""Durable PAPER entries must fit one final acquisition into the real request budget."""

from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider

from quantbot.domain import IntentState
from quantbot.paper_core.account_control import (
    AccountRequestControl,
    BudgetedTransport,
    initialize_account_control,
    observed_host_identity,
)
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
async def test_prepared_entry_sends_with_remaining_final_acquisition_budget(tmp_path, monkeypatch):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons, receipt.reasons
        clocks.advance(2100)
        ready(store)
        cohort = await broker.observation_cohort()
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert any(decision.action == "BUY" for decision in decisions), [d.gates for d in decisions]
        ready(store)
        control_path = tmp_path / "account-control.db"
        initialize_account_control(control_path)
        epoch = "e" * 64
        monkeypatch.setattr(
            "quantbot.paper_core.account_control.observed_host_epoch", lambda: epoch
        )
        control = AccountRequestControl(
            control_path=str(control_path),
            account_scope="a" * 64,
            host_identity=observed_host_identity(),
            host_epoch=epoch,
        )
        transport = BudgetedTransport(provider, control, "trader", clocks)
        broker.adapter._transport = transport
        # Acquisition/discovery already spent most of this minute's allowance.
        # Six remaining trading requests cover final clock, asset, account,
        # positions, open orders and the reserved POST, without a duplicate pass.
        for _ in range(34):
            assert transport.budgets["trading"].acquire(
                "trader", purpose="COHORT_ACQUISITION", now_monotonic=clocks.mono
            )
        provider.delay = 0.4
        started = clocks.mono
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) == 1, reasons
        assert 0 < clocks.mono - started < 5
        order = provider.orders[0]
        with db.transaction() as session:
            repo = StorageRepository(session)
            stored = repo.get_broker_order_by_client_order_id(order["client_order_id"])
            assert stored is not None
            entries = [repo.get_order_intent(link["intent_id"]) for link in store.links()]
            assert any(entry.state is IntentState.FILLED for entry in entries)
        await dispatch_entries(store, broker)
        assert len(provider.orders) == 1
        status = transport.budgets["trading"].status(now_monotonic=clocks.mono)
        assert status["role_used"]["trader"] <= 40
    finally:
        db.close()
