"""Offline capability evidence; simulator arithmetic does not commission crypto trading."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.market_data.calendar import crypto_session_sequence, crypto_sessions
from quantbot.paper_core.contracts import CoreConfig, LearningCalendar
from quantbot.paper_core.entry_policy import AssetEligibility, EntryPolicy
from quantbot.paper_core.simulation import SyntheticBroker


@pytest.mark.asyncio
async def test_lower_level_simulator_shares_cash_and_fractional_positions_across_asset_symbols():
    now = datetime(2026, 11, 5, 15, tzinfo=UTC)
    broker = SyntheticBroker(account_id="offline-mixed-capability", now=now)
    broker.set_mark("AAPL", Decimal(100), as_of=now)
    broker.set_mark("BTC/USD", Decimal(50000), as_of=now)

    for identity, symbol, quantity, tif in (
        ("equity", "AAPL", Decimal(2), TimeInForce.DAY),
        ("crypto", "BTC/USD", Decimal(".01"), TimeInForce.GTC),
    ):
        await broker.submit_order(
            OrderIntent(
                intent_id=identity,
                client_order_id=identity,
                strategy_id="offline-capability-only",
                signal_date=now.date(),
                symbol=symbol,
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                time_in_force=tif,
                quantity=quantity,
                created_at=now,
            )
        )

    assert broker.cash == Decimal(99300)
    assert broker.snapshot().account.equity == Decimal(100000)
    assert {p.symbol: p.quantity for p in broker.position_snapshot()} == {
        "AAPL": Decimal(2),
        "BTC/USD": Decimal(".01"),
    }
    assert len(broker.fills) == 2
    assert all(fill.fee is None and fill.fee_source == "UNKNOWN" for fill in broker.fills)


def test_commissioned_core_entry_contracts_do_not_claim_crypto_capability():
    with pytest.raises(ValidationError, match="supported stock universe"):
        CoreConfig(
            account_id="offline-mixed-capability",
            symbol="AAPL",
            symbols=("AAPL", "BTC/USD"),
            selection_policy="SIGNAL_OR_EXPLORE_V1",
        )
    with pytest.raises(ValidationError, match="supported stocks"):
        EntryPolicy(symbols=("BTC/USD",))
    dynamic = EntryPolicy(
        symbols=(), contract="RESOURCE_BOUNDED_PAPER_V1", universe="ALPACA_ASSETS_V1"
    )
    assert not dynamic.permits("BTC/USD")
    with pytest.raises(ValidationError, match="asset_class"):
        AssetEligibility(
            symbol="BTC/USD",
            asset_class="crypto",
            entry_policy_id=dynamic.id,
            fractionable=True,
            observed_at=datetime(2026, 11, 5, 15, tzinfo=UTC),
            raw_digest="a" * 64,
        )


def test_core_has_no_truthful_crypto_learning_calendar_authority():
    sequence = crypto_session_sequence(crypto_sessions(date(2026, 11, 4), date(2026, 11, 5)))
    with pytest.raises(ValidationError, match="authority"):
        LearningCalendar(sequence=sequence, authority="EXPLICIT_SYNTHETIC_CRYPTO24_V1")
