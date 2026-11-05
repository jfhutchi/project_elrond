"""Opt-in synthetic core mechanics only; no crypto provider or profitability claim."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from quantbot.domain import Account, TimeInForce
from quantbot.market_data.calendar import crypto_sessions
from quantbot.paper_core.asset_capability import AssetCapability, AssetSession, account_risk_day
from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.runtime import initialize_fixture
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.trader import trader_cycle
from quantbot.research.manifest import content_id
from quantbot.storage import Database


def capabilities():
    return (
        AssetCapability(
            symbol="AAPL",
            asset_class="us_equity",
            feed="iex",
            calendar="XNYS",
            benchmark="SPY",
            quantity_step=".000001",
            minimum_quantity=".000001",
            time_in_force=TimeInForce.DAY,
        ),
        AssetCapability(
            symbol="BTC/USD",
            asset_class="crypto",
            feed="crypto",
            calendar="CRYPTO24",
            benchmark="BTC/USD",
            quantity_step=".00000001",
            minimum_quantity=".00000001",
            time_in_force=TimeInForce.GTC,
        ),
    )


def observation(capability, at, sequence):
    if capability.calendar == "CRYPTO24":
        session = crypto_sessions(at.date(), at.date())[0]
        opening, closing = session.open_at, session.close_at
    else:
        opening = at.replace(hour=14, minute=30)
        closing = at.replace(hour=21, minute=0)
    asset_session = AssetSession(
        capability_id=capability.id,
        symbol=capability.symbol,
        calendar=capability.calendar,
        feed=capability.feed,
        observed_at=at,
        session_open=opening,
        session_close=closing,
        account_risk_day=account_risk_day(at),
    )
    return Observation(
        recorded_at_utc=at,
        symbol=capability.symbol,
        source_start=at - timedelta(minutes=5),
        source_end=at,
        available_at=at,
        raw_data_digest="offline-" + str(sequence),
        price=100 if capability.asset_class == "us_equity" else 50000,
        features=(("return", 0.01),),
        dependencies=(capability.benchmark,),
        dependency_available_at=(at,),
        sequence=sequence,
        session_open=opening,
        session_close=closing,
        asset_session=asset_session,
    )


def test_capabilities_are_explicit_offline_and_old_documents_keep_identity():
    legacy = CoreConfig(account_id="fixture", symbol="AAPL")
    assert "asset_capabilities" not in legacy.model_dump()
    assert (
        legacy.model_dump() == CoreConfig.model_validate_json(legacy.model_dump_json()).model_dump()
    )
    old = observation(capabilities()[0], datetime(2026, 11, 5, 15, tzinfo=UTC), 1)
    payload = old.model_dump(mode="python")
    payload.pop("asset_session")
    decoded = Observation.model_validate(payload)
    assert decoded.model_dump(mode="python") == payload
    assert decoded.id == content_id(payload)
    with pytest.raises(ValidationError, match="synthetic"):
        CoreConfig(
            account_id="fixture",
            symbol="AAPL",
            evidence_lane="BROKER_PAPER",
            asset_capabilities=capabilities(),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"feed": "iex"},
        {"calendar": "XNYS"},
        {"time_in_force": "DAY"},
        {"benchmark": "SPY"},
        {"quantity_step": "0"},
    ],
)
def test_crypto_capability_rejects_cross_asset_contracts(change):
    payload = capabilities()[1].model_dump()
    with pytest.raises(ValidationError):
        AssetCapability.model_validate({**payload, **change})


def test_crypto_session_retains_utc_market_day_and_one_et_account_risk_day():
    at = datetime(2026, 11, 8, 1, tzinfo=UTC)
    observed = observation(capabilities()[1], at, 1)
    assert observed.asset_session.session_open.date() == date(2026, 11, 8)
    assert observed.asset_session.account_risk_day == date(2026, 11, 7)
    assert account_risk_day(datetime(2026, 11, 8, 5, tzinfo=UTC)) == date(2026, 11, 8)
    payload = observed.asset_session.model_dump()
    with pytest.raises(ValidationError, match="risk day"):
        AssetSession.model_validate({**payload, "account_risk_day": date(2026, 11, 8)})


def test_non_utc_crypto_observation_normalizes_before_utc_session_validation():
    local = datetime(2026, 11, 7, 20, tzinfo=ZoneInfo("America/New_York"))
    utc = local.astimezone(UTC)
    session = crypto_sessions(utc.date(), utc.date())[0]
    crypto = capabilities()[1]
    observed = AssetSession(
        capability_id=crypto.id,
        symbol=crypto.symbol,
        calendar=crypto.calendar,
        feed=crypto.feed,
        observed_at=local,
        session_open=session.open_at,
        session_close=session.close_at,
        account_risk_day=local.date(),
    )
    assert observed.observed_at == datetime(2026, 11, 8, 1, tzinfo=UTC)
    assert observed.session_open.date() == date(2026, 11, 8)
    assert observed.account_risk_day == date(2026, 11, 7)


@pytest.mark.asyncio
async def test_capability_precision_preserves_valid_submicro_crypto_entry(tmp_path):
    at = datetime(2026, 11, 8, 1, tzinfo=UTC)
    config = CoreConfig(
        account_id="offline-submicro", symbol="BTC/USD", asset_capabilities=capabilities()
    )
    broker = SyntheticBroker(
        account_id=config.account_id,
        now=at,
        initial_account=Account(
            account_id=config.account_id, cash=1, buying_power=1, equity=1, currency="USD"
        ),
    )
    broker.set_mark("BTC/USD", Decimal(50000), as_of=at)
    with Database.create(tmp_path / "submicro.db") as db:
        store = CoreStore(db, config)
        proof = initialize_fixture(store, broker, now=at, entry_enabled=True)
        observed = observation(capabilities()[1], at, 1)
        decision = await trader_cycle(store, broker, observed, ModelArtifact(), proof, now=at)
        assert decision.action == "BUY", decision.gates
        assert decision.quantity == Decimal(".0000001")
        assert broker.posts[0].quantity == Decimal(".0000001")
        assert broker.posts[0].quantity * observed.price == Decimal(".005")
        broker.now = at + timedelta(minutes=30)
        broker.set_mark("BTC/USD", Decimal(50000), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        assert broker.posts[-1].quantity == Decimal(".0000001")
        assert broker.position_snapshot() == ()
        assert store.export_loop(decision.id)["round_trip"]["net_pnl"] is None


@pytest.mark.parametrize("fault", ["missing", "feed", "identity", "benchmark"])
def test_candidate_refuses_mismatched_asset_session_evidence(tmp_path, fault):
    from quantbot.paper_core.trader import evaluate_entry

    at = datetime(2026, 11, 8, 1, tzinfo=UTC)
    equity, crypto = capabilities()
    config = CoreConfig(
        account_id="offline-refusal", symbol="BTC/USD", asset_capabilities=(equity, crypto)
    )
    observed = observation(crypto, at, 1)
    if fault == "missing":
        observed = observed.model_copy(update={"asset_session": None})
    elif fault == "benchmark":
        observed = observed.model_copy(update={"dependencies": ("SPY",)})
    else:
        changed = {"feed": "iex"} if fault == "feed" else {"capability_id": equity.id}
        observed = observed.model_copy(
            update={"asset_session": observed.asset_session.model_copy(update=changed)}
        )
    broker = SyntheticBroker(account_id=config.account_id, now=at)
    with Database.create(tmp_path / "refusal.db") as db:
        store = CoreStore(db, config)
        proof = initialize_fixture(store, broker, now=at, entry_enabled=True)
        decision = evaluate_entry(config, observed, ModelArtifact(), proof)
        assert decision.action == "WAIT"
        assert any("ASSET_CAPABILITY_OR_SESSION" in gate for gate in decision.gates)


@pytest.mark.asyncio
async def test_weekend_crypto_uses_real_virtual_instant_and_unknown_fees_stay_unknown(tmp_path):
    at = datetime(2026, 11, 8, 1, tzinfo=UTC)
    config = CoreConfig(
        account_id="offline-weekend", symbol="BTC/USD", asset_capabilities=capabilities()
    )
    broker = SyntheticBroker(account_id=config.account_id, now=at)
    broker.set_mark("BTC/USD", Decimal(50000), as_of=at)
    with Database.create(tmp_path / "weekend.db") as db:
        store = CoreStore(db, config)
        proof = initialize_fixture(store, broker, now=at, entry_enabled=True)
        observed = observation(capabilities()[1], at, 1)
        first = await trader_cycle(store, broker, observed, ModelArtifact(), proof, now=at)
        assert first.action == "BUY", first.gates
        broker.now = at + timedelta(minutes=30)
        broker.set_mark("BTC/USD", Decimal(50000), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        trip = store.export_loop(first.id)["round_trip"]
        assert trip["status"] == "COSTS_PENDING" and trip["net_pnl"] is None
        assert trip["evidence_lane"] == "SYNTHETIC_TEST"
        assert store.config.asset_capabilities[1].bound_quantity(Decimal(".000000001")) == 0


@pytest.mark.asyncio
async def test_equity_crypto_round_trips_use_one_core_ledger_and_existing_guardian(tmp_path):
    class ExplicitSyntheticFeeBroker(SyntheticBroker):
        """Artificial 25-cent fills, confined to synthetic test evidence."""

        async def submit_order(self, intent):
            order = await super().submit_order(intent)
            self.cash -= Decimal(".25")
            self.fills[-1] = self.fills[-1].model_copy(
                update={
                    "fee": Decimal(".25"),
                    "fee_source": "BROKER_REPORTED",
                    "fee_reference": "EXPLICIT_SYNTHETIC_FEE_FIXTURE_ONLY",
                }
            )
            return order

    at = datetime(2026, 11, 5, 15, tzinfo=UTC)
    equity, crypto = capabilities()
    config = CoreConfig(
        account_id="offline-mixed", symbol="AAPL", asset_capabilities=(equity, crypto)
    )
    broker = ExplicitSyntheticFeeBroker(account_id=config.account_id, now=at)
    broker.set_mark("AAPL", Decimal(100), as_of=at)
    broker.set_mark("BTC/USD", Decimal(50000), as_of=at)
    path = tmp_path / "single-core.db"
    with Database.create(path) as db:
        store = CoreStore(db, config)
        proof = initialize_fixture(store, broker, now=at, entry_enabled=True)
        first = await trader_cycle(
            store, broker, observation(equity, at, 1), ModelArtifact(), proof, now=at
        )
        assert first.action == "BUY", first.gates
        # The second asset competes for the existing single-position risk slot.
        blocked = await trader_cycle(
            store, broker, observation(crypto, at, 2), ModelArtifact(), store.latest_proof(), now=at
        )
        assert len(broker.posts) == 1
        broker.now = at + timedelta(minutes=30)
        broker.set_mark("AAPL", Decimal(100), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        assert Decimal(store.export_loop(first.id)["round_trip"]["net_pnl"]) == Decimal("-.5")
        assert len(broker.posts) == 2
        assert blocked.symbol == "BTC/USD"
    # Reopen the same ledger and immutable config before the crypto entry.
    at = at + timedelta(minutes=35)
    broker.now = at
    broker.set_mark("BTC/USD", Decimal(50000), as_of=at)
    with Database(path) as db:
        store = CoreStore(db, config)
        await guardian_cycle(store, broker, now=at)
        second = await trader_cycle(
            store, broker, observation(crypto, at, 3), ModelArtifact(), store.latest_proof(), now=at
        )
        assert second.action == "BUY", second.gates
        assert broker.posts[-1].time_in_force is TimeInForce.GTC
        assert Decimal(0) < broker.posts[-1].quantity <= Decimal(".01")
        broker.now = at + timedelta(minutes=30)
        broker.set_mark("BTC/USD", Decimal(50000), as_of=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        assert len(broker.posts) == 4
        assert broker.posts[-1].time_in_force is TimeInForce.GTC
        assert broker.position_snapshot() == ()
        assert broker.cash == Decimal(99999)
        trips = store.documents("core_round_trips")
        assert len(trips) == 2
        assert all(trip["status"] == "COMPLETE" for trip in trips)
        assert all(Decimal(trip["net_pnl"]) == Decimal("-.5") for trip in trips)
