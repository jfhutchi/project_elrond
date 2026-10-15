"""OFFLINE provider protocol: affordable whole shares, fresh eligibility and owned exits."""

import json
from decimal import Decimal

import pytest
from tests.integration.paper_core.test_paper_funds import (
    FundsProvider,
    candidate,
    close,
    prepare,
    ready,
)

from quantbot.domain import IntentState
from quantbot.paper_core.contracts import ProtectionPlan
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.trader import claim_entry
from quantbot.storage import StorageRepository


class WholeProvider(FundsProvider):
    def __init__(self, clocks):
        super().__init__(clocks)
        self.cash = Decimal("96.92")
        self.fill_price = Decimal("12.32")
        self.account_overrides = {"equity": "495"}
        self.fractionable = False

    async def request(self, method, url, **kwargs):
        response = await super().request(method, url, **kwargs)
        if "/assets/" in url:
            payload = json.loads(response.content)
            payload["fractionable"] = self.fractionable
        elif url.endswith("/bars"):
            payload = json.loads(response.content)
            payload["bars"]["F"] = [dict(r) for r in payload["bars"]["XLE"]]
            for symbol, price in (("AAPL", 342), ("MSFT", 539), ("F", 12.32)):
                for bar in payload["bars"][symbol]:
                    bar.update({k: price for k in ("o", "h", "l", "c")})
        elif url.endswith("/positions"):
            payload = json.loads(response.content)
            for row in payload:
                row.update(
                    avg_entry_price=str(self.fill_price),
                    current_price=str(self.fill_price),
                    market_value=str(Decimal(row["qty"]) * self.fill_price),
                )
        else:
            return response
        return response.model_copy(update={"content": json.dumps(payload).encode()})


async def prepared(tmp_path):
    return await prepare(
        tmp_path,
        provider_factory=WholeProvider,
        entry_policy=EntryPolicy(symbols=("AAPL", "MSFT", "F")),
    )


@pytest.mark.asyncio
async def test_affordable_whole_entry_preserves_loss_limits_and_owned_exit(tmp_path):
    db, store, broker, provider, clocks = await prepared(tmp_path)
    try:
        decision = await candidate(store, broker)
        assert (decision.symbol, decision.quantity) == ("F", Decimal(1))
        assert decision.entry_policy_id == store.entry_policy.id
        assert "BUYING_POWER_RESERVE" in dict(decision.selection.exclusions)["AAPL"]
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        ready(store)
        assert await dispatch_entries(store, broker) == ()
        assert len(provider.orders) == 1 and Decimal(provider.orders[0]["qty"]) == 1
        await close(store, broker, clocks)
        assert len(provider.orders) == 2
        assert provider.orders[-1]["side"] == "sell" and Decimal(provider.orders[-1]["qty"]) == 1
        trip = store.documents("core_round_trips")[0]
        assert trip["entry_policy_id"] == store.entry_policy.id
        assert trip["open_quantity"] == "0" and trip["net_pnl"] is None
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_fractionable", [True, False])
async def test_capability_loss_before_post_releases_original_fractional_intent(
    tmp_path, initial_fractionable
):
    db, store, broker, provider, clocks = await prepared(tmp_path)
    try:
        provider.fractionable = initial_fractionable
        decision = await candidate(store, broker)
        assert 0 < decision.quantity < 1 if initial_fractionable else decision.quantity == 1
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        ready(store)
        provider.fractionable = not initial_fractionable
        reasons = await dispatch_entries(store, broker)
        assert "ASSET_CAPABILITY_CHANGED" in reasons
        assert not provider.orders
        receipts = [
            d
            for d in store.documents("core_provider_acquisitions")
            if d.get("source") == "ALPACA_PAPER_ASSET_V1"
        ]
        assert any(r["fractionable"] is (not initial_fractionable) for r in receipts)
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent(entry.intent.intent_id).state is IntentState.ERROR
            assert definitively_unsent(repo, entry.intent.intent_id)
            event = next(
                e
                for e in repo.list_order_events(intent_id=entry.intent.intent_id)
                if e.event_type == "CORE_ENTRY_SEND_REFUSED"
            )
            assert any(
                r["raw_digest"] and r["entry_policy_id"] == store.entry_policy.id
                for r in receipts
                if __import__("quantbot.research.manifest", fromlist=["content_id"]).content_id(r)
                in event.detail["evidence_ids"]
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_fractional_orders_remain_available_under_amendment(tmp_path):
    db, store, broker, provider, clocks = await prepared(tmp_path)
    try:
        provider.fractionable = True
        decision = await candidate(store, broker)
        assert 0 < decision.quantity < 1
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry
        ready(store)
        assert not await dispatch_entries(store, broker)
        assert len(provider.orders) == 1
        assert Decimal(provider.orders[0]["qty"]) == decision.quantity
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [None, "false"])
async def test_unknown_fractional_capability_is_not_a_whole_share_permission(tmp_path, unknown):
    db, store, broker, provider, clocks = await prepared(tmp_path)
    try:
        provider.fractionable = unknown
        cohort = await broker.observation_cohort()
        assert not cohort.observations
        assert all("ASSET_ELIGIBILITY" in reason for _, reason in cohort.unavailable)
        assert not provider.orders
    finally:
        db.close()
