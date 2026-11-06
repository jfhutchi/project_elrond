"""OFFLINE_PROTOCOL_FIXTURE: real PAPER paths, scripted transport, no external broker."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import unquote

from pydantic import SecretStr
from tests.integration.paper_core.test_paper_funds import FundsProvider, prepare, ready

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.market_data.crypto import AlpacaCryptoDataClient
from quantbot.paper_core.contracts import ReconciliationProof, RoundTrip
from quantbot.paper_core.crypto_custody import scoped_quantity_objections
from quantbot.paper_core.crypto_policy import CryptoPaperPolicy
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.portfolio import portfolio_budget
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.storage import StorageRepository


class CryptoProtocolProvider(FundsProvider):
    """Full fills with an immediately debited coin fee reported only after protection."""

    def __init__(self, clocks):
        super().__init__(clocks)
        self.account_overrides.update(crypto_status="ACTIVE", non_marginable_buying_power="100000")
        self.fee_quantity = Decimal(".00001")
        self.fee_visible = False
        self.fee_row = None

    async def request(self, method, url, **kwargs):
        params = kwargs.get("params") or {}
        payload = None
        symbol = unquote(url.rsplit("/", 1)[-1])
        if "/assets/" in url and symbol == "BTC/USD":
            payload = dict(
                symbol=symbol,
                **{"class": "crypto"},
                status="active",
                tradable=True,
                fractionable=True,
                min_order_size=".00001",
                min_trade_increment=".00001",
                price_increment=".01",
            )
        elif url.endswith("/clock"):
            now = self.clocks.wall
            opening = now.replace(hour=14, minute=30, second=0, microsecond=0)
            closing = now.replace(hour=21, minute=0, second=0, microsecond=0)
            payload = dict(
                timestamp=now.isoformat(),
                is_open=opening <= now < closing,
                next_open=(opening if now < opening else opening + timedelta(days=1)).isoformat(),
                next_close=(closing if now < closing else closing + timedelta(days=1)).isoformat(),
            )
        elif "/crypto/" in url and url.endswith("/bars"):
            end = self.clocks.wall.replace(second=0, microsecond=0)
            end -= timedelta(minutes=end.minute % 5)
            payload = {
                "bars": {
                    s: [
                        dict(
                            t=(end - timedelta(minutes=35 - i * 5)).isoformat(),
                            o=94 + i,
                            h=94 + i,
                            l=94 + i,
                            c=94 + i,
                            v=100000,
                        )
                        for i in range(7)
                    ]
                    for s in params["symbols"].split(",")
                },
                "next_page_token": None,
            }
        elif url.endswith("/latest/quotes") or url.endswith("/quotes/latest"):
            payload = {
                "quotes": {
                    s: dict(ap=100, bp=99.99, t=self.clocks.wall.isoformat())
                    for s in params["symbols"].split(",")
                }
            }
        elif url.endswith("/orders") and method == "GET" and params.get("status") == "all":
            payload = self.orders
        elif "/account/activities" in url:
            if params.get("order_id"):
                payload = [self.fee_row] if self.fee_visible and self.fee_row else []
            else:
                until = datetime.fromisoformat(str(params["until"]).replace("Z", "+00:00"))
                payload = [
                    dict(row, cum_qty=row["qty"])
                    for row in self.fills
                    if datetime.fromisoformat(row["transaction_time"]) <= until
                ]
                if self.fee_visible and self.fee_row:
                    payload.append(self.fee_row)
        if payload is not None:
            self.clocks.advance(self.delay)
            self.now = self.clocks.wall
            self.calls.append((method, url, params))
            return BrokerTransportResponse(
                status_code=200, headers={}, content=json.dumps(payload).encode()
            )
        response = await super().request(method, url, **kwargs)
        if method == "POST" and url.endswith("/orders"):
            order = self.orders[-1]
            if order["symbol"] == "BTC/USD" and order["side"] == "buy":
                self.holdings["BTC/USD"] -= self.fee_quantity
                self.fee_row = dict(
                    id="delayed-coin-fee",
                    activity_type="CFEE",
                    date=self.now.date().isoformat(),
                    net_amount="0",
                    symbol="BTCUSD",
                    qty=str(-self.fee_quantity),
                    price="100",
                    status="executed",
                    order_id=order["id"],
                )
        return response


async def prepared_crypto(tmp_path):
    values = await prepare(
        tmp_path,
        provider_factory=CryptoProtocolProvider,
        entry_policy=EntryPolicy(
            contract="RESOURCE_BOUNDED_PAPER_V1",
            universe="ALPACA_ASSETS_V1",
            symbols=(),
            crypto=CryptoPaperPolicy(symbols=("BTC/USD",)),
        ),
        initial_wall=datetime(2026, 11, 6, 14, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    db, store, broker, provider, clocks = values
    broker.store = store
    key = SecretStr("OFFLINE_PROTOCOL_FIXTURE")
    broker.crypto_market = AlpacaCryptoDataClient(api_key=key, api_secret=key, transport=provider)
    assert await broker.initialize_crypto_assets() == ()
    store.runtime_role = "guardian"
    opening = await guardian_cycle(store, broker, now=clocks.wall)
    assert not opening.reasons, opening.reasons
    assert len(store.documents("core_session_openings")) == 1
    target = datetime(2026, 11, 6, 21, 5, 3, tzinfo=UTC)
    clocks.advance((target - clocks.wall).total_seconds())
    # Actual activity pagination/cursor catches up across the six-hour query bound.
    for _ in range(3):
        await synchronize(store, broker, now=clocks.wall)
    ready(store)
    return values


async def test_after_close_crypto_round_trip_uses_custody_and_delayed_coin_fee(tmp_path):
    db, store, broker, provider, clocks = await prepared_crypto(tmp_path)
    try:
        cohort = await broker.observation_cohort()
        assert {o.symbol for o in cohort.observations} == {"BTC/USD"}
        assert any(gap.startswith("EQUITY:") for gap in cohort.market_gaps)
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert len(decisions) == 1 and decisions[0].action == "BUY", decisions
        decision = decisions[0]
        assert decision.research_signal_contract == "CRYPTO_COST_SCREEN_V1"
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) == 1, reasons
        buy = provider.orders[0]
        assert (buy["symbol"], buy["side"], buy["type"], buy["time_in_force"]) == (
            "BTC/USD",
            "buy",
            "market",
            "gtc",
        )
        await dispatch_entries(store, broker)
        assert len(provider.orders) == 1, "Repeated dispatch must retain the original client ID"
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="OFFLINE_EMERGENCY_STOP", updated_at=clocks.wall
            )
        clocks.advance(1801)
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        latest = max(
            (
                ReconciliationProof.model_validate(d)
                for d in store.documents("core_reconciliation_proofs")
            ),
            key=lambda proof: proof.recorded_at_utc,
        )
        with db.transaction() as session:
            scope_reasons = scoped_quantity_objections(
                store, session, latest, now=clocks.wall, latest=False
            )
        assert len(provider.orders) == 2, (
            receipt.reasons,
            scope_reasons,
            latest.expected_quantities,
            latest.actual_quantities,
            latest.quarantined_symbols,
        )
        assert latest.quarantined_symbols == ("BTC/USD",) and latest.custody_evidence_ids
        assert not latest.complete and latest.expected_quantities != latest.actual_quantities
        sell = provider.orders[1]
        assert (sell["side"], sell["type"], sell["time_in_force"]) == ("sell", "market", "gtc")
        assert Decimal(sell["qty"]) == Decimal(buy["qty"]) - provider.fee_quantity
        assert provider.holdings["BTC/USD"] == 0
        process_outcomes(store, cutoff=clocks.wall)
        assert not store.documents("core_round_trips"), "Unposted fee cannot close gross ownership"
        await guardian_cycle(store, broker, now=clocks.wall)
        assert len(provider.orders) == 2, "No second sale of the unposted fee residual"
        provider.fee_visible = True
        clocks.advance(301)
        await synchronize(store, broker, now=clocks.wall)
        process_outcomes(store, cutoff=clocks.wall)
        trips = [RoundTrip.model_validate(d) for d in store.documents("core_round_trips")]
        assert len(trips) == 1, store.documents("core_health_events")
        trip = trips[0]
        assert trip.lot_id == decision.id and trip.open_quantity == 0
        assert trip.asset_fee_quantity == provider.fee_quantity
        assert trip.asset_fee_allocation_ids and trip.gross_pnl == 0
        assert trip.actual_fees is None and trip.net_pnl is None and trip.status == "COSTS_PENDING"
        assert any(params.get("order_id") == buy["id"] for _, _, params in provider.calls if params)
        assert not store.documents("core_cost_items"), "Coin debit does not establish a USD fee"
        fees = store.documents("core_external_owner_events")
        assert len(fees) == 1 and fees[0]["occurred_at"] is None
        with db.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged
            budget = portfolio_budget(store, session, store.latest_proof(), now=clocks.wall)
        assert "MANUAL_ENTRY_STOP" in budget.objections
        ready(store)
        await trader_batch_cycle(
            store,
            broker,
            await broker.observation_cohort(),
            broker.config.initial_model,
            now=clocks.wall,
        )
        await dispatch_entries(store, broker)
        assert len(provider.orders) == 2
    finally:
        db.close()


async def test_equity_day_order_still_uses_existing_protocol(tmp_path):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=CryptoProtocolProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 11, 6, 14, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons, receipt.reasons
        clocks.advance(2100)
        ready(store)
        decisions = await trader_batch_cycle(
            store,
            broker,
            await broker.observation_cohort(),
            broker.config.initial_model,
            now=clocks.wall,
        )
        assert any(d.action == "BUY" for d in decisions), decisions
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) == 1, reasons
        assert "PORTFOLIO_CANONICAL_FILLS_PENDING" in reasons
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) == 2, reasons
        assert {(o["symbol"], o["time_in_force"]) for o in provider.orders} == {
            ("AAPL", "day"),
            ("MSFT", "day"),
        }
        assert not any("/crypto/" in url for _, url, _ in provider.calls)
    finally:
        db.close()
