"""A temporarily empty provider cohort must not consume the evaluation boundary."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core import roles
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle


class OpeningResourceProvider(ResourceProvider):
    """Sparse premarket bars and actual fresh quote/daily source responses."""

    async def request(self, method, url, **kwargs):
        if url.endswith("/bars") and kwargs["params"]["timeframe"] == "1Day":
            stamp = (self.clocks.wall - timedelta(days=1)).replace(
                hour=4, minute=0, second=0, microsecond=0
            )
            return BrokerTransportResponse(
                status_code=200,
                headers={},
                content=json.dumps(
                    {
                        "bars": {
                            symbol: [
                                {
                                    "t": stamp.isoformat(),
                                    "o": price,
                                    "h": price,
                                    "l": price,
                                    "c": price,
                                    "v": 10000,
                                }
                            ]
                            for symbol, price in (("AAPL", 99), ("MSFT", 99), ("SPY", 100))
                        },
                        "next_page_token": None,
                    }
                ).encode(),
            )
        response = await super().request(method, url, **kwargs)
        if url.endswith("/bars"):
            payload = json.loads(response.content)
            payload["bars"] = {}  # No contiguous premarket window is available.
            return response.model_copy(update={"content": json.dumps(payload).encode()})
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("first_poll", ["empty", "capital_wait", "opening_capital_wait", "horizon"])
async def test_empty_then_usable_cohort_retries_same_boundary_without_duplicate_orders(
    tmp_path, monkeypatch, first_poll
):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=OpeningResourceProvider
        if first_poll == "opening_capital_wait"
        else ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons
        if first_poll != "opening_capital_wait":
            clocks.advance(2100)
        ready(store)
        acquire = broker.observation_cohort
        acquired_at = []
        captured_cohorts = []
        sleeps = []
        order_counts = []

        async def delayed_first_cohort():
            acquired_at.append(clocks.wall)
            captured = await acquire()
            captured_cohorts.append(captured)
            assert captured.observations
            if len(acquired_at) == 1 and first_poll in {"empty", "horizon"}:
                return captured.model_copy(
                    update={
                        "observations": (),
                        "unavailable": tuple(
                            (
                                observation.symbol,
                                "STOCK_SIGNAL_HORIZON_EXCEEDS_SESSION"
                                if first_poll == "horizon"
                                else "FRESH_COMPLETE_BAR_UNAVAILABLE",
                            )
                            for observation in captured.observations
                        ),
                    }
                )
            return captured

        async def advance(seconds):
            sleeps.append(seconds)
            order_counts.append(len(provider.orders))
            clocks.advance(seconds)
            # Existing offline fixture's independent Guardian attestation; actual
            # acquisition, claims, dispatch, ledger and broker protocol stay real.
            ready(store)

        broker.observation_cohort = delayed_first_cohort
        if first_poll in {"capital_wait", "opening_capital_wait"}:
            from quantbot.paper_core import batch

            actual_budget = batch.portfolio_budget

            def temporarily_unavailable_budget(*args, **kwargs):
                budget = actual_budget(*args, **kwargs)
                return (
                    replace(budget, objections=("SESSION_CAPITAL_UNVERIFIED",))
                    if len(acquired_at) == 1
                    else budget
                )

            monkeypatch.setattr(batch, "portfolio_budget", temporarily_unavailable_budget)
        monkeypatch.setattr(roles.asyncio, "sleep", advance)
        config = broker.config.model_copy(update={"entry_poll_seconds": 15})
        await roles.run_role(store, broker, config, "trader", cycles=5)
        if first_poll == "horizon":
            assert len(acquired_at) == 1
            assert not provider.orders
            assert sleeps and all(1 <= seconds <= 15 for seconds in sleeps)
            return
        if first_poll in {"capital_wait", "opening_capital_wait"}:
            assert any(
                d["action"] == "WAIT" and d["gates"] == ["SESSION_CAPITAL_UNVERIFIED"]
                for d in store.documents("core_decisions")
            ), "The first attempt must actually evaluate WAIT, not fail with an exception"
        assert len(acquired_at) == 2, "Retry the empty poll; consume the usable evaluation once"
        assert len({int(at.timestamp() // 300) for at in acquired_at}) == 1
        assert sleeps and all(1 <= seconds <= 15 for seconds in sleeps)
        assert provider.orders, "The second same-boundary observation must reach execution"
        assert order_counts[-1] == len(provider.orders), "Further polls must not duplicate orders"
        clients = [order["client_order_id"] for order in provider.orders]
        assert len(clients) == len(set(clients))
        if first_poll == "opening_capital_wait":
            from quantbot.paper_core.selection import boundary_id
            from quantbot.paper_core.trader import trader_batch_cycle

            first, second = captured_cohorts
            assert first.source_end != second.source_end
            assert boundary_id(store.config, first) == boundary_id(store.config, second)
            assert all(
                observation.feature_context == "OPENING_QUOTE_GAP_V1"
                for captured in captured_cohorts
                for observation in captured.observations
            )
            # Even a restarted role obtaining a newer quote in this boundary must
            # reuse the existing durable claim, never issue another batch.
            original_batches = store.documents("core_batches")
            original_reservations = store.documents("core_entry_reservations")
            ready(store)
            later = await acquire()
            assert later.source_end > second.source_end
            assert boundary_id(store.config, later) == boundary_id(store.config, second)
            await trader_batch_cycle(store, broker, later, config.initial_model, now=clocks.wall)
            ready(store)
            await roles.dispatch_entries(store, broker)
            assert store.documents("core_batches") == original_batches
            assert store.documents("core_entry_reservations") == original_reservations
            assert len(original_batches) == 1
            clients_after = [order["client_order_id"] for order in provider.orders]
            assert len(clients_after) == len(set(clients_after))
            assert len(clients_after) <= len(original_batches[0]["decision_ids"])
            assert set(clients_after) <= {
                reservation["client_order_id"] for reservation in original_reservations
            }
    finally:
        db.close()


@pytest.mark.parametrize(
    "action,gates,age,retry",
    [
        ("WAIT", ("CORE_ENTRY_FENCED",), 15, True),
        ("WAIT", ("CORE_INDEPENDENT_GUARDIAN_NOT_READY",), 15, True),
        ("WAIT", ("SESSION_CAPITAL_UNVERIFIED",), 121, False),
        ("WAIT", ("SESSION_CAPITAL_UNVERIFIED",), -1, False),
        ("WAIT", ("SESSION_CAPITAL_UNVERIFIED", "CORE_MANUAL_ENTRY_PAUSE"), 15, False),
        ("WAIT", ("CORE_EVIDENCE_QUARANTINED",), 15, False),
        ("WAIT", ("PREDICT_NET_RESERVE",), 15, False),
        ("BUY", ("CORE_ENTRY_FENCED",), 15, False),
    ],
)
def test_only_fresh_unclaimed_transient_waits_are_reevaluated(action, gates, age, retry):
    source_end = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    decisions = (SimpleNamespace(action=action, gates=gates),)
    assert (
        roles._retry_transient_wait(
            decisions, source_end=source_end, now=source_end + timedelta(seconds=age)
        )
        is retry
    )
