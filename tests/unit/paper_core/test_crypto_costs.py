"""Crypto activity evidence never invents fee allocation or custody reconciliation."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.contract.test_alpaca_paper_broker import (
    ScriptedBrokerTransport,
    account_payload,
    adapter,
    intent,
    order_payload,
    response,
)

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerProtocolError

NOW = datetime(2026, 11, 6, 16, tzinfo=UTC)
ASSET = {"class": "crypto", "symbol": "BTC/USD", "status": "active", "tradable": True}


def test_nonmarginable_power_is_retained_and_changes_spending_authority():
    first = AlpacaPaperBrokerAdapter._account_from_payload(
        account_payload(non_marginable_buying_power="17.25")
    ).account
    second = AlpacaPaperBrokerAdapter._account_from_payload(
        account_payload(non_marginable_buying_power="9.25")
    ).account
    assert getattr(first, "non_marginable_buying_power", None) == Decimal("17.25")
    from quantbot.paper_core.funds import same_spending_authority

    assert not same_spending_authority(first, second)


def test_missing_nonmarginable_power_stays_unknown_and_preserves_legacy_document():
    account = AlpacaPaperBrokerAdapter._account_from_payload(account_payload()).account
    assert "non_marginable_buying_power" not in account.model_dump(mode="json")
    assert getattr(account, "non_marginable_buying_power", "absent") is None


@pytest.mark.parametrize("value", ["NaN", "-1", "Infinity"])
def test_bad_nonmarginable_power_is_a_protocol_error(value):
    with pytest.raises(BrokerProtocolError):
        AlpacaPaperBrokerAdapter._account_from_payload(
            account_payload(non_marginable_buying_power=value)
        )


@pytest.mark.asyncio
async def test_verified_crypto_aliases_normalize_positions_orders_and_fills():
    position = dict(
        symbol="BTCUSD",
        qty=".01",
        side="long",
        current_price="50000",
        market_value="500",
        avg_entry_price="49000",
    )
    fill = dict(
        activity_type="FILL",
        id="fill",
        order_id="order",
        symbol="BTCUSD",
        side="buy",
        qty=".01",
        price="49000",
        transaction_time=NOW.isoformat(),
    )
    transport = ScriptedBrokerTransport(
        [
            response(200, [position]),
            response(200, order_payload(intent(), symbol="BTCUSD")),
            response(200, [fill]),
        ]
    )
    broker = adapter(transport)
    assert hasattr(broker, "register_crypto_asset"), "verified alias registration is missing"
    broker.register_crypto_asset(ASSET)
    assert (await broker.get_positions())[0].symbol == "BTC/USD"
    assert (await broker.get_order("order")).symbol == "BTC/USD"
    assert (await broker.get_fills())[0].symbol == "BTC/USD"


def test_crypto_alias_registration_rejects_unverified_equity_and_non_usd_pair():
    broker = adapter(ScriptedBrokerTransport([]))
    assert hasattr(broker, "register_crypto_asset"), "verified alias registration is missing"
    for changes in ({"class": "us_equity"}, {"tradable": False}, {"symbol": "ETH/BTC"}):
        with pytest.raises(BrokerProtocolError):
            broker.register_crypto_asset({**ASSET, **changes})


@pytest.mark.asyncio
async def test_single_position_encodes_verified_pair_without_accepting_arbitrary_slashes():
    broker = adapter(ScriptedBrokerTransport([response(404)]))
    broker.register_crypto_asset(ASSET)
    assert await broker.get_position("BTC/USD") is None
    with pytest.raises(ValueError, match="unsupported characters"):
        await broker.get_position("UNKNOWN/USD")


def fee_row(**changes):
    return dict(
        id="fee-1",
        activity_type="CFEE",
        date="2026-11-06",
        net_amount="0",
        symbol="BTCUSD",
        qty="-.000025",
        price="50000",
        status="executed",
        **changes,
    )


def test_date_only_cfee_preserves_asset_debit_without_effective_time_or_fill_allocation():
    from quantbot.paper_core import crypto_costs

    evidence = crypto_costs.parse_fee_activity(
        fee_row(), symbol_aliases={"BTCUSD": "BTC/USD"}, source_document_id="source"
    )
    assert evidence.activity_id == "fee-1"
    assert evidence.raw_symbol == "BTCUSD" and evidence.symbol == "BTC/USD"
    assert evidence.asset_currency == "BTC"
    assert evidence.quantity_delta == Decimal("-.000025")
    assert evidence.net_cash == 0 and evidence.effective_at is None
    assert evidence.time_precision == "DATE"
    assert evidence.fill_id is None and evidence.order_id is None
    assert evidence.external_quantity_event(account_id="fixture", baseline_at=NOW) is None


def test_posting_timestamp_does_not_manufacture_effective_fee_time():
    from quantbot.paper_core import crypto_costs

    evidence = crypto_costs.parse_fee_activity(
        fee_row(created_at=NOW.isoformat()),
        symbol_aliases={"BTCUSD": "BTC/USD"},
        source_document_id="source",
    )
    assert evidence.posted_at == NOW and evidence.effective_at is None


def test_precise_fee_quantity_event_is_account_level_and_not_a_cash_fee():
    from quantbot.paper_core import crypto_costs

    evidence = crypto_costs.parse_fee_activity(
        fee_row(transaction_time=NOW.isoformat()),
        symbol_aliases={"BTCUSD": "BTC/USD"},
        source_document_id="source",
    )
    event = evidence.external_quantity_event(
        account_id="fixture", baseline_at=datetime(2026, 11, 6, 15, tzinfo=UTC)
    )
    assert event["symbol"] == "BTC/USD" and event["quantity_delta"] == "-0.000025"
    assert event["net_cash"] == "0" and event["owner"] == "ACCOUNT_CRYPTO_FEE"
    assert "fill_id" not in event and "order_id" not in event
    assert evidence.external_quantity_event(account_id="fixture", baseline_at=NOW) is None


def test_unknown_symbol_never_creates_quantity_authority():
    from quantbot.paper_core import crypto_costs

    evidence = crypto_costs.parse_fee_activity(
        fee_row(transaction_time=NOW.isoformat()), symbol_aliases={}, source_document_id="source"
    )
    assert evidence.symbol is None and evidence.asset_currency is None
    assert evidence.quantity_delta == Decimal("-.000025")
    assert evidence.external_quantity_event(account_id="fixture", baseline_at=NOW) is None


def test_zero_dollar_cfee_still_contradicts_zero_cost_contract():
    from quantbot.paper_core import crypto_costs

    assert crypto_costs.nonzero_fee_activity(fee_row())
    assert not crypto_costs.nonzero_fee_activity({"activity_type": "DIV", "net_amount": "5"})
    assert not crypto_costs.nonzero_fee_activity({"activity_type": "FEE", "net_amount": "0"})


@pytest.mark.parametrize("field,value", [("qty", "NaN"), ("net_amount", "Infinity")])
def test_nonfinite_fee_evidence_is_rejected(field, value):
    from quantbot.paper_core import crypto_costs

    with pytest.raises(BrokerProtocolError):
        crypto_costs.parse_fee_activity(
            {**fee_row(), field: value}, symbol_aliases={}, source_document_id="source"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("precise", [False, True])
async def test_owned_activity_ingestion_retains_fee_and_debits_only_proven_account_quantity(
    tmp_path, monkeypatch, precise
):
    from datetime import timedelta
    from types import SimpleNamespace

    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.contracts import CoreConfig
    from quantbot.paper_core.runtime import initialize_fixture
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.paper_core.store import CoreStore
    from quantbot.storage import Database, StateConflictError

    baseline_at = NOW - timedelta(hours=1)
    database = Database.create(tmp_path / "crypto-fee-fixture.db")
    try:
        store = CoreStore(database, CoreConfig(account_id="fixture", symbol="XLE"))
        simulated = SyntheticBroker(
            account_id="fixture", now=baseline_at, inherited={"BTC/USD": Decimal(".01")}
        )
        initialize_fixture(store, simulated, now=baseline_at)
        row = fee_row(**({"transaction_time": NOW.isoformat()} if precise else {}))
        transport = ScriptedBrokerTransport(
            [
                response(200, [row]),
                response(200, [row]),
                response(200, [{**row, "qty": "-.000050"}]),
            ]
        )
        native = adapter(transport)
        native.register_crypto_asset(ASSET)
        broker = AlpacaCoreBroker(
            native,
            None,
            None,
            SimpleNamespace(cost_contract=None),
            SimpleNamespace(sample=lambda: (NOW, 1.0)),
            account_id="fixture",
        )
        monkeypatch.setattr(
            "quantbot.paper_core.authority.commission_in_session",
            lambda *_: SimpleNamespace(approved_at=baseline_at, legacy_owners=()),
        )
        assert not await broker.owned_events(store)
        assert not await broker.owned_events(store)
        receipts = [
            r
            for r in store.documents("core_provider_acquisitions")
            if r.get("contract") == "ALPACA_FEE_ACTIVITY_V1"
        ]
        assert len(receipts) == 1, "fee evidence must survive idempotent activity replay"
        assert receipts[0]["quantity_delta"] == "-0.000025"
        with database.transaction() as session:
            quantities, cash, _ = store.ledger(session)
        assert quantities["BTC/USD"] == Decimal(".009975" if precise else ".01")
        assert cash == simulated.cash, "base-asset fees must not be deducted from USD cash"
        assert not store.documents("core_cost_items"), "account CFEE has no exact-fill authority"
        with pytest.raises(StateConflictError, match="FEE_ACTIVITY_CONFLICT"):
            await broker.owned_events(store)
    finally:
        database.close()


def test_zero_cash_asset_fee_invalidates_contract_through_real_revision_path(tmp_path):
    from types import SimpleNamespace

    from tests.fixtures.core_m2 import prepare

    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker

    database, store, simulated, *_ = prepare(tmp_path)
    try:
        broker = AlpacaCoreBroker(
            adapter(ScriptedBrokerTransport([])),
            None,
            None,
            SimpleNamespace(cost_contract=SimpleNamespace(id="fixture-zero-contract")),
            SimpleNamespace(sample=lambda: (NOW, 1.0)),
            account_id=simulated.account_id,
        )
        document = {"rows": [fee_row()]}
        with database.transaction() as session:
            assert broker._revise_conflicted_contract(store, session, document, None, {}, [])
        assert document["cost_conflict_contract_id"] == "fixture-zero-contract"
    finally:
        database.close()
