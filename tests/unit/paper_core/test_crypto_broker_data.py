"""Operational crypto acquisition exercised without a store, account or network."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import unquote

import pytest
from pydantic import SecretStr

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportResponse
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.market_data.crypto import AlpacaCryptoDataClient
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker


class Provider:
    def __init__(self, now):
        self.now, self.calls = now, []
        self.account = {
            "id": "paper",
            "status": "ACTIVE",
            "crypto_status": "ACTIVE",
            "trading_blocked": False,
            "account_blocked": False,
            "trade_suspended_by_user": False,
        }
        self.asset = {
            "class": "crypto",
            "status": "active",
            "tradable": True,
            "fractionable": True,
            "min_order_size": "0.0001",
            "min_trade_increment": "0.00001",
            "price_increment": "0.01",
        }
        self.missing = None
        self.quote_age = 0
        self.fail_crypto_bars = False
        self.fail_crypto_quotes = False
        self.fail_stock_bars = False
        self.market_open = False
        self.opening_quotes = False
        self.asset_errors = set()
        self.asset_overrides = {}
        self.positions = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("params")))
        if url.endswith("/account"):
            payload = self.account
        elif "/assets/" in url:
            symbol = unquote(url.rsplit("/", 1)[1])
            if symbol in self.asset_errors:
                from quantbot.brokers.base import BrokerTransportError

                raise BrokerTransportError("offline asset lookup unavailable")
            payload = {**self.asset, "symbol": symbol}
            if symbol == "XLE":
                payload = {
                    "symbol": symbol,
                    "class": "us_equity",
                    "status": "active",
                    "tradable": True,
                    "fractionable": True,
                }
            payload.update(self.asset_overrides.get(symbol, {}))
        elif url.endswith("/positions"):
            payload = self.positions
        elif url.endswith("/clock"):
            payload = {
                "timestamp": self.now.isoformat(),
                "is_open": self.market_open,
                "next_open": (self.now + timedelta(days=2)).isoformat(),
                "next_close": (self.now + timedelta(days=2, hours=7)).isoformat(),
            }
        elif url.endswith("/calendar"):
            payload = [
                {
                    "date": (self.now.date() + timedelta(days=d)).isoformat(),
                    "open": "09:30",
                    "close": "16:00",
                }
                for d in (-3, 0, 1)
            ]
        elif url.endswith("/bars"):
            if self.fail_crypto_bars and "/crypto/" in url:
                from quantbot.market_data.base import MarketDataRequestError

                raise MarketDataRequestError("crypto unavailable")
            if self.fail_stock_bars and "/stocks/" in url:
                from quantbot.market_data.base import MarketDataRequestError

                raise MarketDataRequestError("stocks unavailable")
            end = self.now.replace(second=0, microsecond=0)
            end -= timedelta(minutes=end.minute % 5)
            payload = {
                "bars": {
                    symbol: [
                        dict(
                            t=(end - timedelta(minutes=35 - i * 5)).isoformat(),
                            o=100 + i,
                            h=100 + i,
                            l=100 + i,
                            c=100 + i,
                            v=100,
                        )
                        for i in range(7)
                        if (symbol, i) != self.missing
                    ]
                    for symbol in kwargs["params"]["symbols"].split(",")
                }
            }
            if self.opening_quotes and "/stocks/" in url:
                if kwargs["params"]["timeframe"] == "5Min":
                    payload = {"bars": {}}
                else:
                    previous = (self.now - timedelta(days=3)).replace(
                        hour=5, minute=0, second=0, microsecond=0
                    )
                    payload = {
                        "bars": {
                            symbol: [dict(t=previous.isoformat(), o=99, h=100, l=99, c=100, v=1000)]
                            for symbol in kwargs["params"]["symbols"].split(",")
                        }
                    }
        elif url.endswith("/latest/quotes") or url.endswith("/quotes/latest"):
            if self.fail_crypto_quotes and "/crypto/" in url:
                from quantbot.market_data.base import MarketDataRequestError

                raise MarketDataRequestError("crypto unavailable")
            at = self.now - timedelta(seconds=self.quote_age)
            payload = {
                "quotes": {
                    s: {"t": at.isoformat(), "ap": 100, "bp": 99.99}
                    for s in kwargs["params"]["symbols"].split(",")
                }
            }
        else:
            raise AssertionError("unexpected HTTP endpoint " + url)
        return BrokerTransportResponse(
            status_code=200, headers={}, content=json.dumps(payload).encode()
        )


def broker_fixture(now=None):
    now = now or datetime(2026, 11, 7, 0, 0, 3, tzinfo=UTC)
    provider = Provider(now)
    policy = SimpleNamespace(
        crypto=SimpleNamespace(symbols=("BTC/USD", "ETH/USD", "SOL/USD")),
        id="a" * 64,
        dynamic=False,
        permits=lambda s: s in {"BTC/USD", "ETH/USD", "SOL/USD", "XLE"},
    )
    config = SimpleNamespace(
        entry_policy=policy, core=SimpleNamespace(release_id="fixture"), entry_symbols=("XLE",)
    )
    clocks = SimpleNamespace(sample=lambda: (now, 10.0), process_id="crypto-fixture")
    kwargs = dict(
        api_key=SecretStr("offline"),
        api_secret=SecretStr("offline"),
        transport=provider,
        max_retries=0,
    )
    broker = AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(**kwargs),
        AlpacaMarketDataClient(**kwargs),
        AlpacaMarketCalendar(**kwargs),
        config,
        clocks,
        account_id="paper",
        crypto_market=AlpacaCryptoDataClient(**kwargs),
    )
    return broker, provider


async def test_native_crypto_eligibility_retains_lot_rules_and_account_status():
    broker, provider = broker_fixture()
    asset = await broker.entry_asset_eligibility("BTC/USD")
    assert asset.asset_class == "crypto" and asset.crypto_status == "ACTIVE"
    assert asset.minimum_quantity == Decimal("0.0001")
    assert asset.quantity_step == Decimal("0.00001")
    assert asset.price_increment == Decimal("0.01")
    assert asset.time_in_force.value == "GTC"
    assert all(method == "GET" for method, _, _ in provider.calls)


@pytest.mark.parametrize(
    "change",
    [
        {"crypto_status": "INACTIVE"},
        {"id": "other"},
        {"trading_blocked": True},
        {"account_blocked": True},
        {"trade_suspended_by_user": True},
    ],
)
async def test_crypto_account_refusals_are_not_hidden_by_active_asset(change):
    broker, provider = broker_fixture()
    provider.account.update(change)
    with pytest.raises(ValueError, match="CRYPTO_ACCOUNT_INELIGIBLE"):
        await broker.entry_asset_eligibility("BTC/USD")


@pytest.mark.parametrize(
    "change",
    [
        {"min_trade_increment": "0"},
        {"min_order_size": "NaN"},
        {"min_order_size": None},
        {"fractionable": False},
        {"class": "us_equity"},
    ],
)
async def test_crypto_eligibility_rejects_unverified_quantity_metadata(change):
    broker, provider = broker_fixture()
    provider.asset.update(change)
    with pytest.raises(ValueError, match="ASSET_ELIGIBILITY_INVALID"):
        await broker.entry_asset_eligibility("BTC/USD")


async def test_weekend_crypto_features_use_actual_cross_midnight_bars_and_btc():
    broker, provider = broker_fixture()
    cohort = await broker.acquire_crypto_cohort(("ETH/USD",))
    (observation,) = cohort.observations
    assert observation.feed == "alpaca-crypto"
    assert observation.feature_version == "crypto-five-minute-price-alpha-v1"
    assert observation.dependencies == ("BTC/USD",)
    assert observation.source_start == datetime(2026, 11, 6, 23, 25, tzinfo=UTC)
    assert observation.source_end == datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
    assert dict(observation.features)["relative_strength_15m"] == 0
    assert observation.crypto_session.asset_eligibility_id == observation.asset_eligibility.id
    assert observation.crypto_session.observed_at == observation.source_end
    assert all(
        "/stocks/" not in url and not url.endswith("/calendar") for _, url, _ in provider.calls
    )


async def test_missing_crypto_benchmark_bar_does_not_create_observation_or_mark():
    broker, provider = broker_fixture()
    provider.missing = ("BTC/USD", 3)
    cohort = await broker.acquire_crypto_cohort(("ETH/USD",))
    assert not cohort.observations
    assert cohort.unavailable == (("ETH/USD", "CRYPTO_CONTIGUOUS_HISTORY_UNAVAILABLE"),)
    assert broker.mark("ETH/USD") is None


async def test_crypto_guardian_marks_use_crypto_data_when_stock_market_is_closed():
    broker, provider = broker_fixture()
    await broker.refresh_marks(("BTC/USD", "ETH/USD"))
    assert broker.mark("BTC/USD") == (Decimal("99.99"), provider.now)
    assert all("/stocks/" not in url for _, url, _ in provider.calls)


async def test_crypto_asset_alias_initialization_survives_entry_suspension():
    broker, provider = broker_fixture()
    provider.account["crypto_status"] = "INACTIVE"
    await broker.initialize_crypto_assets()
    assert not any(url.endswith("/account") for _, url, _ in provider.calls)
    assert len(provider.calls) == 3


async def test_inactive_coin_identity_supports_reads_but_never_entry_permission():
    broker, provider = broker_fixture()
    provider.asset_overrides["BTC/USD"] = {"status": "inactive", "tradable": False}
    await broker.initialize_crypto_assets()
    assert broker.adapter.normalize_crypto_payload({"symbol": "BTCUSD"}) == {"symbol": "BTC/USD"}
    assert (await broker.entry_asset_eligibility("XLE")).asset_class == "us_equity"
    with pytest.raises(ValueError, match="ASSET_ELIGIBILITY_INVALID"):
        await broker.entry_asset_eligibility("BTC/USD")


async def test_missing_crypto_metadata_is_scoped_and_retries_during_reads():
    broker, provider = broker_fixture()
    monotonic = [10.0]
    broker.clocks = SimpleNamespace(sample=lambda: (provider.now, monotonic[0]))
    provider.asset_errors.add("BTC/USD")
    failures = await broker.initialize_crypto_assets()
    assert failures == (("BTC/USD", "CRYPTO_ASSET_METADATA_UNAVAILABLE:BrokerTransportError"),)
    assert broker.adapter.normalize_crypto_payload({"symbol": "BTCUSD"}) == {"symbol": "BTCUSD"}
    assert broker.adapter.normalize_crypto_payload({"symbol": "ETHUSD"}) == {"symbol": "ETH/USD"}
    assert (await broker.entry_asset_eligibility("XLE")).asset_class == "us_equity"
    calls = len(provider.calls)
    assert await broker.initialize_crypto_assets() == failures
    assert len(provider.calls) == calls, "failed metadata must back off, not hammer the provider"
    provider.asset_errors.clear()
    monotonic[0] += 30
    assert await broker.get_positions() == ()
    assert broker.crypto_metadata_objections == ()
    assert broker.adapter.normalize_crypto_payload({"symbol": "BTCUSD"}) == {"symbol": "BTC/USD"}


@pytest.mark.parametrize("payload", [{"symbol": "DOGE/USD"}, {"class": "us_equity"}])
async def test_invalid_crypto_metadata_never_creates_guessed_alias(payload):
    broker, provider = broker_fixture()
    provider.asset_overrides["BTC/USD"] = payload
    failures = await broker.initialize_crypto_assets()
    assert failures == (("BTC/USD", "CRYPTO_ASSET_METADATA_UNAVAILABLE:ValueError"),)
    assert broker.adapter.normalize_crypto_payload({"symbol": "BTCUSD"}) == {"symbol": "BTCUSD"}
    assert broker.adapter.normalize_crypto_payload({"symbol": "DOGEUSD"}) == {"symbol": "DOGEUSD"}


def attach_quote_store(broker):
    documents = []
    broker.store = SimpleNamespace(
        config=SimpleNamespace(release_id="release", policy_id="collect-owned-30m-stop100-v1"),
        database=SimpleNamespace(store_identity="fixture"),
        config_id="config",
        runtime_time_evidence_id=None,
        record=lambda kind, doc: documents.append((kind, doc)),
    )
    return documents


async def test_crypto_entry_quote_has_native_provenance_and_no_stock_endpoint():
    broker, provider = broker_fixture()
    documents = attach_quote_store(broker)
    quote = await broker.entry_price_evidence("BTC/USD")
    assert quote.feed == "crypto"
    assert quote.endpoint == "https://data.alpaca.markets/v1beta3/crypto/us/latest/quotes"
    assert quote.ask == Decimal(100)
    assert documents == [("core_provider_acquisitions", quote)]
    assert all("/stocks/" not in url for _, url, _ in provider.calls)


async def test_stale_crypto_entry_quote_never_becomes_price_authority():
    broker, provider = broker_fixture()
    documents = attach_quote_store(broker)
    provider.quote_age = 6
    with pytest.raises(ValueError, match="ENTRY_PRICE_UNAVAILABLE_OR_STALE"):
        await broker.entry_price_evidence("BTC/USD")
    assert not documents


async def test_crypto_data_failure_does_not_starve_equity_protection_marks():
    from quantbot.market_data.base import MarketDataRequestError

    broker, provider = broker_fixture()
    provider.fail_crypto_quotes = True
    with pytest.raises(MarketDataRequestError, match="crypto unavailable"):
        await broker.refresh_marks(("BTC/USD", "XLE"))
    assert broker.mark("XLE") == (Decimal(106), datetime(2026, 11, 7, tzinfo=UTC))
    assert broker.mark("BTC/USD") is None


async def test_crypto_protection_uses_fresh_bid_between_five_minute_bars():
    broker, provider = broker_fixture(datetime(2026, 11, 7, 0, 3, tzinfo=UTC))
    await broker.refresh_marks(("BTC/USD",))
    assert broker.mark("BTC/USD") == (Decimal("99.99"), provider.now)
    assert any(url.endswith("/latest/quotes") for _, url, _ in provider.calls)
    provider.quote_age = 20
    broker._marks.clear()
    await broker.refresh_marks(("BTC/USD",))
    assert broker.mark("BTC/USD") is None


async def test_combined_cohort_retains_both_markets_and_shared_account_day():
    broker, provider = broker_fixture(datetime(2026, 11, 9, 15, 5, 3, tzinfo=UTC))
    provider.market_open = True
    cohort = await broker.observation_cohort()
    assert {o.symbol for o in cohort.observations} == {"XLE", "BTC/USD", "ETH/USD", "SOL/USD"}
    assert cohort.crypto_symbols == ("BTC/USD", "ETH/USD", "SOL/USD")
    assert cohort.session_open == datetime(2026, 11, 9, 5, tzinfo=UTC)
    assert cohort.session_close == datetime(2026, 11, 10, 5, tzinfo=UTC)
    assert all(o.source_end == cohort.source_end for o in cohort.observations)
    assert next(o for o in cohort.observations if o.symbol == "XLE").dependencies == ("SPY",)
    assert not cohort.market_gaps


async def test_weekend_cohort_has_crypto_and_no_invented_stock_members():
    broker, _ = broker_fixture()
    cohort = await broker.observation_cohort()
    assert {o.symbol for o in cohort.observations} == {"BTC/USD", "ETH/USD", "SOL/USD"}
    assert cohort.discovery is None
    assert "EQUITY:MARKET_CLOSED" in cohort.market_gaps
    assert not cohort.unavailable


@pytest.mark.parametrize("failed", ["crypto", "stock"])
async def test_combined_cohort_keeps_the_other_market_on_provider_failure(failed):
    broker, provider = broker_fixture(datetime(2026, 11, 9, 15, 5, 3, tzinfo=UTC))
    provider.market_open = True
    provider.fail_stock_bars = failed == "stock"
    provider.fail_crypto_bars = failed == "crypto"
    cohort = await broker.observation_cohort()
    assert {o.symbol for o in cohort.observations} == (
        {"XLE"} if failed == "crypto" else {"BTC/USD", "ETH/USD", "SOL/USD"}
    )
    assert cohort.market_gaps
    assert {s for s, _ in cohort.unavailable} == (
        {"BTC/USD", "ETH/USD", "SOL/USD"} if failed == "crypto" else set()
    )


def attach_discovery(broker, provider, *, stale=False):
    from quantbot.paper_core.crypto_policy import CryptoPaperPolicy
    from quantbot.paper_core.discovery import DiscoveryEvidence, DiscoverySample
    from quantbot.paper_core.entry_policy import EntryPolicy

    broker.config.entry_policy = EntryPolicy(
        contract="RESOURCE_BOUNDED_PAPER_V1",
        universe="ALPACA_ASSETS_V1",
        symbols=(),
        crypto=CryptoPaperPolicy(),
    )
    at = provider.now
    evidence = DiscoveryEvidence(
        catalog_id="catalog",
        catalog_at=at,
        eligible_count=1,
        recorded_at=at,
        symbols=("XLE",),
        scan_receipt_ids=("scan",),
        samples=(
            DiscoverySample(
                symbol="XLE",
                source_at=at,
                received_at=at,
                dollar_volume=Decimal(1000000),
                spread_bps=Decimal(1),
                score=Decimal(1),
                raw_digest="raw",
                scan_receipt_id="scan",
            ),
        ),
    )

    def select_candidates(**kwargs):
        if stale:
            raise ValueError("DISCOVERY_CATALOG_STALE")
        return evidence

    broker._discovery_state = SimpleNamespace(select_candidates=select_candidates)


async def test_real_mixed_policy_validator_accepts_bound_equity_and_crypto_members():
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.selection import validate_cohort

    broker, provider = broker_fixture(datetime(2026, 11, 9, 15, 5, 3, tzinfo=UTC))
    provider.market_open = True
    attach_discovery(broker, provider)
    cohort = await broker.observation_cohort()
    validate_cohort(
        CoreConfig(account_id="paper", symbol="XLE"),
        cohort,
        entry_policy=broker.config.entry_policy,
    )
    assert cohort.discovery.symbols == ("XLE",)
    assert {o.symbol for o in cohort.observations} == {"XLE", "BTC/USD", "ETH/USD", "SOL/USD"}


async def test_stale_discovery_does_not_block_crypto_or_claim_fresh_equity_coverage():
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.selection import validate_cohort

    broker, provider = broker_fixture()
    attach_discovery(broker, provider, stale=True)
    cohort = await broker.observation_cohort()
    validate_cohort(
        CoreConfig(account_id="paper", symbol="XLE"),
        cohort,
        entry_policy=broker.config.entry_policy,
    )
    assert cohort.discovery is None
    assert cohort.market_gaps == ("EQUITY:DISCOVERY_CATALOG_STALE",)
    assert len(cohort.observations) == 3


async def test_opening_receipt_boundary_does_not_retimestamp_crypto_bars():
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.selection import validate_cohort

    broker, provider = broker_fixture(datetime(2026, 11, 9, 14, 35, 3, tzinfo=UTC))
    provider.market_open = True
    provider.opening_quotes = True
    attach_discovery(broker, provider)
    cohort = await broker.observation_cohort()
    validate_cohort(
        CoreConfig(account_id="paper", symbol="XLE"),
        cohort,
        entry_policy=broker.config.entry_policy,
    )
    assert cohort.source_end == provider.now
    (observed,) = cohort.observations
    assert observed.symbol == "XLE" and observed.feature_context == "OPENING_QUOTE_GAP_V1"
    assert observed.source_end == provider.now
    assert cohort.unavailable == tuple(
        (symbol, "SOURCE_BOUNDARY_MISMATCH") for symbol in ("BTC/USD", "ETH/USD", "SOL/USD")
    )
