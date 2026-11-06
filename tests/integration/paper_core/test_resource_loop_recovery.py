"""Offline broker-protocol exercise of the deployed resource-bounded entry policy."""

import json
from datetime import UTC, datetime

import pytest
from tests.integration.paper_core.test_paper_funds import FundsProvider, prepare, ready

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.health import active_health, report
from quantbot.paper_core.portfolio import portfolio_budget
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import StorageRepository


class ResourceProvider(FundsProvider):
    async def request(self, method, url, **kwargs):
        if url.endswith("/quotes/latest"):
            symbols = kwargs["params"]["symbols"].split(",")
            return BrokerTransportResponse(
                status_code=200,
                headers={},
                content=json.dumps(
                    {
                        "quotes": {
                            symbol: {"ap": 100, "bp": 99.99, "t": self.clocks.wall.isoformat()}
                            for symbol in symbols
                        }
                    }
                ).encode(),
            )
        response = await super().request(method, url, **kwargs)
        if "/account/activities" in url:
            rows = json.loads(response.content)
            for row in rows:
                row["cum_qty"] = row["qty"]  # Fixture orders each have one full execution.
            return response.model_copy(update={"content": json.dumps(rows).encode()})
        return response


@pytest.mark.asyncio
async def test_opening_recovery_repeated_entries_and_manual_stop(tmp_path):
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
        assert len(store.documents("core_session_openings")) == 1
        with db.transaction() as session:
            budget = portfolio_budget(store, session, store.latest_proof(), now=clocks.wall)
        assert not budget.objections, budget.objections
        report(
            store,
            role="GUARDIAN",
            state="RETRYABLE_FAILURE",
            reasons=("INVENTORY_ACQUISITION_RETRYABLE",),
            now=clocks.wall,
            scope="ACCOUNT",
            scope_id=store.config.account_id,
        )
        clocks.advance(2100)  # Sufficient post-opening completed bars for the entry signal.
        ready(store)
        cohort = await broker.observation_cohort()
        assert cohort.observations, cohort.unavailable
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert decisions and any(d.action == "BUY" for d in decisions), decisions
        with db.transaction() as session:
            assert not active_health(session), "Fresh certified authority must recover the retry"
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) >= 1, reasons
        first_count = len(provider.orders)
        await dispatch_entries(store, broker)
        assert len(provider.orders) == first_count, "Duplicate dispatch must not duplicate orders"
        clocks.advance(300)
        ready(store)
        cohort = await broker.observation_cohort()
        next_decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert any(d.action == "BUY" for d in next_decisions), next_decisions
        assert len(provider.orders) > first_count, reasons
        first_count = len(provider.orders)
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="OFFLINE_EMERGENCY_STOP", updated_at=clocks.wall
            )
        clocks.advance(300)
        cohort = await broker.observation_cohort()
        await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        ready(store)
        await dispatch_entries(store, broker)
        assert len(provider.orders) == first_count
        with db.transaction() as session:
            budget = portfolio_budget(store, session, store.latest_proof(), now=clocks.wall)
        assert "MANUAL_ENTRY_STOP" in budget.objections
    finally:
        db.close()
