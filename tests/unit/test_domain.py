from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantbot.domain import (
    Account,
    Bar,
    BrokerEnvironment,
    BrokerOrder,
    BrokerProvider,
    Fill,
    IntentState,
    MarketClock,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    ReconciliationStatus,
    StrategyIdentity,
    TimeInForce,
    TradingMode,
    build_client_order_id,
)

NOW = datetime(2026, 8, 13, 14, 30, tzinfo=UTC)


def test_enums_have_stable_external_values() -> None:
    assert [member.value for member in BrokerProvider] == ["alpaca", "tradier", "ibkr"]
    assert [member.value for member in BrokerEnvironment] == ["PAPER", "LIVE"]
    assert [member.value for member in TradingMode] == ["PAPER", "LIVE"]
    assert [member.value for member in OrderSide] == ["BUY", "SELL"]
    assert [member.value for member in OrderType] == ["MARKET", "LIMIT", "STOP", "STOP_LIMIT"]
    assert [member.value for member in TimeInForce] == ["DAY", "GTC"]
    assert [member.value for member in IntentState] == [
        "RISK_APPROVED",
        "ORDER_CREATED",
        "SUBMITTING",
        "BROKER_ACCEPTED",
        "PARTIALLY_FILLED",
        "FILLED",
        "REJECTED",
        "CANCEL_PENDING",
        "CANCELLED",
        "EXPIRED",
        "ERROR",
        "SUBMISSION_UNKNOWN",
    ]
    assert [member.value for member in ReconciliationStatus] == [
        "RECONCILED",
        "RECONCILIATION_REQUIRED",
        "ACCOUNT_MISMATCH",
    ]


def test_bar_validates_ohlc_symbol_and_normalizes_timestamp_to_utc() -> None:
    eastern = timezone(timedelta(hours=-4))
    bar = Bar(
        symbol="AAPL",
        timestamp=datetime(2026, 8, 13, 10, 30, tzinfo=eastern),
        open="100",
        high="110",
        low="95",
        close="105",
        volume=1000,
        adjustment="1",
    )

    assert bar.timestamp == NOW
    assert bar.timestamp.tzinfo is UTC
    assert bar.close == Decimal("105")

    with pytest.raises(ValidationError):
        Bar(
            symbol="aapl",
            timestamp=NOW,
            open=100,
            high=110,
            low=95,
            close=105,
            volume=1000,
            adjustment=1,
        )
    with pytest.raises(ValidationError):
        Bar(
            symbol="AAPL",
            timestamp=NOW,
            open=100,
            high=104,
            low=95,
            close=105,
            volume=1000,
            adjustment=1,
        )
    with pytest.raises(ValidationError):
        Bar(
            symbol="AAPL",
            timestamp=NOW,
            open=100,
            high=110,
            low=101,
            close=105,
            volume=1000,
            adjustment=1,
        )
    with pytest.raises(ValidationError):
        Bar(
            symbol="AAPL",
            timestamp=NOW,
            open=100,
            high=110,
            low=95,
            close=105,
            volume=0,
            adjustment=1,
        )


def test_domain_models_reject_naive_datetimes_and_are_frozen() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        MarketClock(
            timestamp=datetime(2026, 8, 13, 14, 30),
            is_open=True,
            next_open=NOW,
            next_close=NOW + timedelta(hours=6),
        )

    account = Account(
        account_id="account-1",
        cash="1000",
        buying_power="2000",
        equity="1500",
        currency="USD",
    )
    with pytest.raises(ValidationError):
        account.cash = Decimal("0")  # type: ignore[misc]


def test_account_position_and_fill_constraints() -> None:
    account = Account(
        account_id="account-1",
        cash=0,
        buying_power=0,
        equity=0,
        currency="USD",
    )
    position = Position(
        symbol="AAPL",
        quantity="2",
        market_price="105",
        market_value="210",
        average_entry_price="100",
    )
    fill = Fill(
        fill_id="fill-1",
        broker_order_id="broker-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        quantity="1",
        price="101.25",
        occurred_at=NOW,
    )

    assert account.cash == Decimal("0")
    assert position.market_value == Decimal("210")
    assert fill.fee == Decimal("0")

    with pytest.raises(ValidationError):
        Account(
            account_id="account-1",
            cash=-1,
            buying_power=0,
            equity=0,
            currency="USD",
        )
    with pytest.raises(ValidationError):
        Fill(
            fill_id="fill-1",
            broker_order_id="broker-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            quantity=0,
            price=1,
            occurred_at=NOW,
        )


def test_order_intent_defaults_and_positive_order_values() -> None:
    intent = OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        strategy_id="strategy-1",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity="5",
        limit_price="100.25",
        created_at=NOW,
    )

    assert intent.extended_hours is False
    assert intent.state is IntentState.RISK_APPROVED

    with pytest.raises(ValidationError):
        OrderIntent(
            intent_id="intent-1",
            client_order_id="client-1",
            strategy_id="strategy-1",
            symbol="AAPL",
            signal_date=date(2026, 8, 13),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            quantity=0,
            limit_price=100,
            created_at=NOW,
        )
    with pytest.raises(ValidationError):
        OrderIntent(
            intent_id="intent-1",
            client_order_id="client-1",
            strategy_id="strategy-1",
            symbol="AAPL",
            signal_date=date(2026, 8, 13),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            quantity=1,
            limit_price=0,
            created_at=NOW,
        )


@pytest.mark.parametrize(
    ("order_type", "price_fields"),
    [
        (OrderType.MARKET, {}),
        (OrderType.LIMIT, {"limit_price": "100.25"}),
        (OrderType.STOP, {"stop_price": "99.75"}),
        (
            OrderType.STOP_LIMIT,
            {"limit_price": "100.25", "stop_price": "99.75"},
        ),
    ],
)
def test_order_intent_accepts_only_the_prices_required_by_order_type(
    order_type: OrderType,
    price_fields: dict[str, str],
) -> None:
    intent = OrderIntent(
        intent_id="intent-1",
        client_order_id="client-1",
        strategy_id="strategy-1",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        order_type=order_type,
        time_in_force=TimeInForce.DAY,
        quantity="5",
        created_at=NOW,
        **price_fields,
    )

    assert intent.order_type is order_type


@pytest.mark.parametrize(
    ("order_type", "price_fields"),
    [
        (OrderType.MARKET, {"limit_price": "100.25"}),
        (OrderType.MARKET, {"stop_price": "99.75"}),
        (OrderType.LIMIT, {}),
        (
            OrderType.LIMIT,
            {"limit_price": "100.25", "stop_price": "99.75"},
        ),
        (OrderType.STOP, {}),
        (
            OrderType.STOP,
            {"limit_price": "100.25", "stop_price": "99.75"},
        ),
        (OrderType.STOP_LIMIT, {}),
        (OrderType.STOP_LIMIT, {"limit_price": "100.25"}),
        (OrderType.STOP_LIMIT, {"stop_price": "99.75"}),
    ],
)
def test_order_intent_rejects_prices_incompatible_with_order_type(
    order_type: OrderType,
    price_fields: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="price"):
        OrderIntent(
            intent_id="intent-1",
            client_order_id="client-1",
            strategy_id="strategy-1",
            symbol="AAPL",
            signal_date=date(2026, 8, 13),
            side=OrderSide.BUY,
            order_type=order_type,
            time_in_force=TimeInForce.DAY,
            quantity="5",
            created_at=NOW,
            **price_fields,
        )


def test_broker_order_rejects_filled_quantity_above_order_quantity() -> None:
    order = BrokerOrder(
        broker_order_id="broker-1",
        client_order_id="client-1",
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity="5",
        filled_quantity="2",
        status="partially_filled",
        submitted_at=NOW,
        filled_average_price="101.50",
    )

    assert order.filled_quantity == Decimal("2")

    with pytest.raises(ValidationError, match="cannot exceed"):
        BrokerOrder(
            broker_order_id="broker-1",
            client_order_id="client-1",
            symbol="AAPL",
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            quantity=5,
            filled_quantity=6,
            status="filled",
            submitted_at=NOW,
        )


def test_market_clock_and_strategy_identity_normalize_datetimes() -> None:
    eastern = timezone(timedelta(hours=-4))
    clock = MarketClock(
        timestamp=datetime(2026, 8, 13, 10, 30, tzinfo=eastern),
        is_open=True,
        next_open=datetime(2026, 8, 14, 9, 30, tzinfo=eastern),
        next_close=datetime(2026, 8, 13, 16, 0, tzinfo=eastern),
    )
    identity = StrategyIdentity(
        strategy_id="mean-reversion",
        version="1.2.3",
        git_commit="abc1234",
        configuration_hash="def5678",
        deployment_timestamp=datetime(2026, 8, 13, 10, 30, tzinfo=eastern),
    )

    assert clock.timestamp.tzinfo is UTC
    assert clock.next_open.tzinfo is UTC
    assert clock.next_close.tzinfo is UTC
    assert identity.deployment_timestamp == NOW


def test_client_order_id_is_canonical_bounded_and_deterministic() -> None:
    first = build_client_order_id(
        strategy_version="1.2.3",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        sequence=7,
    )
    second = build_client_order_id(
        strategy_version="1.2.3",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        sequence=7,
    )

    assert first == "qb-9ff3803455bec3551ab1859bc9baff62e6d69903"
    assert second == first
    assert len(first) <= 128


@pytest.mark.parametrize("strategy_version", [" 1.2.3", "1.2.3 "])
def test_client_order_id_rejects_strategy_version_surrounding_whitespace(
    strategy_version: str,
) -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        build_client_order_id(
            strategy_version,
            "AAPL",
            date(2026, 8, 13),
            OrderSide.BUY,
            0,
        )


def test_client_order_id_changes_with_payload_and_rejects_negative_sequence() -> None:
    baseline = build_client_order_id("1.2.3", "AAPL", date(2026, 8, 13), OrderSide.BUY, 0)
    changed = build_client_order_id("1.2.3", "AAPL", date(2026, 8, 13), OrderSide.SELL, 0)

    assert changed != baseline
    with pytest.raises(ValueError, match="nonnegative"):
        build_client_order_id("1.2.3", "AAPL", date(2026, 8, 13), OrderSide.BUY, -1)
