"""Opening quote context must traverse the real PAPER protocol and ledger guards."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.time_evidence import TimeEvidence
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import StorageRepository


class OpeningProvider(ResourceProvider):
    offset_seconds = 0
    quote_error_seconds = 0

    async def request(self, method, url, **kwargs):
        if url.endswith("/bars"):
            params = kwargs["params"]
            symbols = params["symbols"].split(",")
            bars = {}
            if params["timeframe"] == "1Day":
                stamp = (self.clocks.wall - timedelta(days=1)).replace(
                    hour=4, minute=0, second=0, microsecond=0
                )
                for symbol in symbols:
                    price = 100 if symbol == "SPY" else 98
                    bars[symbol] = [
                        dict(t=stamp.isoformat(), o=price, h=price, l=price, c=price, v=10000)
                    ]
            return BrokerTransportResponse(
                status_code=200,
                headers={},
                content=json.dumps({"bars": bars, "next_page_token": None}).encode(),
            )
        response = await super().request(method, url, **kwargs)
        if url.endswith("/clock") or url.endswith("/quotes/latest"):
            payload = json.loads(response.content)
            if url.endswith("/clock"):
                payload["timestamp"] = (
                    datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
                    + timedelta(seconds=self.offset_seconds)
                ).isoformat()
            else:
                for quote in payload["quotes"].values():
                    quote["t"] = (
                        self.clocks.wall
                        + timedelta(seconds=self.offset_seconds + self.quote_error_seconds)
                    ).isoformat()
            response = response.model_copy(update={"content": json.dumps(payload).encode()})
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-1, 0, 1])
async def test_opening_quotes_execute_before_first_bar_without_duplicates_or_stop_bypass(
    tmp_path, offset
):
    opening = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=OpeningProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=opening + timedelta(seconds=3),
        warmup_seconds=0,
    )
    try:
        provider.offset_seconds = offset
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons, receipt.reasons
        ready(store)
        cohort = await broker.observation_cohort()
        assert cohort.observations and all(
            o.feature_context == "OPENING_QUOTE_GAP_V1" for o in cohort.observations
        ), cohort.unavailable
        assert opening <= cohort.source_end < opening + timedelta(minutes=1)
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert any(d.action == "BUY" for d in decisions), [d.gates for d in decisions]
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert provider.orders, reasons
        count = len(provider.orders)
        original_batches = store.documents("core_batches")
        assert len(original_batches) == 1
        await dispatch_entries(store, broker)
        assert len(provider.orders) == count
        timing = store.read("core_time_evidence", store.runtime_time_evidence_id, TimeEvidence)
        mismatched = timing.model_copy(update={"release_id": "OTHER_RELEASE"})
        store.record("core_time_evidence", mismatched)
        store.runtime_time_evidence_id = mismatched.id
        with pytest.raises(ValueError, match="ENTRY_QUOTE_TIME_UNVERIFIED"):
            await broker.entry_price_evidence("AAPL")

        clocks.advance(10)
        ready(store)
        newer = await broker.observation_cohort()
        assert newer.source_end > cohort.source_end
        await trader_batch_cycle(store, broker, newer, broker.config.initial_model, now=clocks.wall)
        ready(store)
        await dispatch_entries(store, broker)
        assert store.documents("core_batches") == original_batches
        # Reconciliation can allow an original pending batch member to submit.
        # The newer quote must not create another batch or repeat any client ID.
        count = len(provider.orders)
        assert count <= len(original_batches[0]["decision_ids"])
        assert len({o["client_order_id"] for o in provider.orders}) == count

        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="OFFLINE_OPENING_STOP", updated_at=clocks.wall
            )
        clocks.advance(300)
        ready(store)
        later = await broker.observation_cohort()
        stopped = await trader_batch_cycle(
            store, broker, later, broker.config.initial_model, now=clocks.wall
        )
        assert stopped and all(d.action == "WAIT" for d in stopped)
        assert any("MANUAL_ENTRY_STOP" in d.gates for d in stopped)
        await dispatch_entries(store, broker)
        assert len(provider.orders) == count
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("quote_error", [-6, 1])
async def test_provider_relative_future_or_stale_opening_quotes_are_refused(tmp_path, quote_error):
    opening = datetime(2026, 10, 14, 13, 30, tzinfo=UTC)
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=OpeningProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=opening + timedelta(seconds=10),
        warmup_seconds=0,
    )
    try:
        provider.offset_seconds = 1
        provider.quote_error_seconds = quote_error
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons, receipt.reasons
        ready(store)
        cohort = await broker.observation_cohort()
        assert not cohort.observations
        assert {reason for _, reason in cohort.unavailable} == {"OPENING_QUOTES_UNAVAILABLE"}
        with pytest.raises(ValueError, match="ENTRY_PRICE_UNAVAILABLE_OR_STALE"):
            await broker.entry_price_evidence("AAPL")
        assert (
            await trader_batch_cycle(
                store, broker, cohort, broker.config.initial_model, now=clocks.wall
            )
            == ()
        )
        await dispatch_entries(store, broker)
        assert not provider.orders
    finally:
        db.close()
