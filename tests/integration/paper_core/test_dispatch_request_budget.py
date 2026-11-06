"""One locally exhausted request budget defers the remaining original batch."""

from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider

from quantbot.domain import IntentState
from quantbot.paper_core.account_control import LocalRequestBudgetRefusal
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
async def test_dispatch_stops_after_first_local_budget_refusal(tmp_path):
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
        assert not receipt.reasons
        clocks.advance(2100)
        ready(store)
        cohort = await broker.observation_cohort()
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert len([d for d in decisions if d.action == "BUY"]) == 2
        ready(store)
        links = [link for link in store.links() if link["role"] == "ENTRY"]
        assert len(links) == 2
        attempted = []
        request = provider.request

        async def exhausted(method, url, **kwargs):
            if url.endswith("/quotes/latest"):
                attempted.append(kwargs["params"]["symbols"])
                raise LocalRequestBudgetRefusal("GET")
            return await request(method, url, **kwargs)

        provider.request = exhausted
        reasons = await dispatch_entries(store, broker)
        assert "LOCAL_BROKER_REQUEST_BUDGET" in reasons
        assert len(attempted) == 1
        assert not provider.orders
        with db.transaction() as session:
            states = [
                StorageRepository(session).get_order_intent(link["intent_id"]).state
                for link in links
            ]
            assert all(
                state in {IntentState.RISK_APPROVED, IntentState.ORDER_CREATED} for state in states
            )
            assert IntentState.RISK_APPROVED in states  # Remaining intent was never dispatched.
    finally:
        db.close()
