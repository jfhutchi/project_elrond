from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from quantbot.domain import Account, OrderSide, OrderType, Position, TimeInForce
from quantbot.risk import (
    PositionRiskInput,
    RiskReservation,
    build_liquidation_plan,
    build_portfolio_risk_state,
    calculate_drawdown,
)
from quantbot.strategy import load_strategy_config

ROOT = Path(__file__).parents[3]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"
NEXT_OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=UTC)


def account() -> Account:
    return Account(
        account_id="PA-123",
        cash=Decimal("70000"),
        buying_power=Decimal("80000"),
        equity=Decimal("100000"),
        currency="USD",
    )


def position(
    symbol: str,
    quantity: str,
    market_price: str,
    market_value: str,
) -> Position:
    return Position(
        symbol=symbol,
        quantity=Decimal(quantity),
        market_price=Decimal(market_price),
        market_value=Decimal(market_value),
        average_entry_price=Decimal(market_price),
    )


def test_portfolio_state_aggregates_positions_and_reservations_exactly_once() -> None:
    reservations = (
        RiskReservation(
            reservation_id="entry-aapl",
            symbol="AAPL",
            position_value=Decimal("500"),
            gross_exposure=Decimal("500"),
            open_risk=Decimal("50"),
            reserves_position_slot=False,
        ),
        RiskReservation(
            reservation_id="entry-hyg",
            symbol="HYG",
            position_value=Decimal("100"),
            gross_exposure=Decimal("100"),
            open_risk=Decimal("20"),
            reserves_position_slot=True,
        ),
    )
    state = build_portfolio_risk_state(
        account_snapshot_id="snapshot-1",
        account=account(),
        target_symbol="AAPL",
        positions=(
            PositionRiskInput(
                position=position("AAPL", "10", "100", "1000"),
                active_stop=Decimal("90"),
            ),
            PositionRiskInput(
                position=position("QQQ", "5", "200", "1000"),
                active_stop=Decimal("180"),
            ),
        ),
        reservations=(*reservations, reservations[1]),
    )

    assert state.account_snapshot_id == "snapshot-1"
    assert state.target_symbol == "AAPL"
    assert state.equity == Decimal("100000")
    assert state.buying_power == Decimal("80000")
    assert state.committed_open_risk == Decimal("270")
    assert state.committed_gross_exposure == Decimal("2600")
    assert state.committed_symbol_market_value == Decimal("1500")
    assert state.committed_position_count == 3
    assert state.is_new_position is False
    assert state.open_risk_available is True
    assert state.reasons == ()


def test_new_target_has_zero_symbol_value_and_reserves_unique_slots() -> None:
    state = build_portfolio_risk_state(
        account_snapshot_id="snapshot-2",
        account=account(),
        target_symbol="GLD",
        positions=(
            PositionRiskInput(
                position=position("AAPL", "10", "100", "1000"),
                active_stop=Decimal("90"),
            ),
        ),
        reservations=(
            RiskReservation(
                reservation_id="one",
                symbol="HYG",
                position_value=Decimal("100"),
                gross_exposure=Decimal("100"),
                open_risk=Decimal("20"),
                reserves_position_slot=True,
            ),
            RiskReservation(
                reservation_id="two",
                symbol="HYG",
                position_value=Decimal("50"),
                gross_exposure=Decimal("50"),
                open_risk=Decimal("10"),
                reserves_position_slot=True,
            ),
        ),
    )

    assert state.committed_symbol_market_value == 0
    assert state.committed_position_count == 2
    assert state.is_new_position is True


def test_conflicting_reservation_id_and_duplicate_position_symbol_are_rejected() -> None:
    first = RiskReservation(
        reservation_id="same",
        symbol="AAPL",
        position_value=Decimal("100"),
        gross_exposure=Decimal("100"),
        open_risk=Decimal("10"),
        reserves_position_slot=True,
    )
    conflicting = first.model_copy(update={"open_risk": Decimal("11")})

    with pytest.raises(ValueError, match="conflicting reservation"):
        build_portfolio_risk_state(
            "snapshot",
            account(),
            "AAPL",
            (),
            (first, conflicting),
        )

    duplicate = PositionRiskInput(
        position=position("AAPL", "1", "100", "100"),
        active_stop=Decimal("90"),
    )
    with pytest.raises(ValueError, match="unique symbols"):
        build_portfolio_risk_state(
            "snapshot",
            account(),
            "AAPL",
            (duplicate, duplicate),
            (),
        )


def test_reservation_cannot_understate_gross_exposure() -> None:
    with pytest.raises(ValidationError, match="gross_exposure"):
        RiskReservation(
            reservation_id="understated",
            symbol="AAPL",
            position_value=Decimal("100"),
            gross_exposure=Decimal("99"),
            open_risk=Decimal("10"),
            reserves_position_slot=True,
        )


@pytest.mark.parametrize("field", ["position_value", "gross_exposure", "open_risk"])
def test_risk_increasing_reservation_requires_positive_commitments(field: str) -> None:
    payload = {
        "reservation_id": "zero-commitment",
        "symbol": "AAPL",
        "position_value": Decimal("100"),
        "gross_exposure": Decimal("100"),
        "open_risk": Decimal("10"),
        "reserves_position_slot": True,
    }
    payload[field] = Decimal("0")

    with pytest.raises(ValidationError, match=field):
        RiskReservation.model_validate(payload)


def test_missing_stop_and_short_position_make_open_risk_unavailable() -> None:
    missing_stop = build_portfolio_risk_state(
        "snapshot",
        account(),
        "AAPL",
        (
            PositionRiskInput(
                position=position("AAPL", "10", "100", "1000"),
                active_stop=None,
            ),
        ),
        (),
    )
    short = build_portfolio_risk_state(
        "snapshot",
        account(),
        "AAPL",
        (
            PositionRiskInput(
                position=position("HYG", "-2", "100", "-200"),
                active_stop=Decimal("110"),
            ),
        ),
        (),
    )

    assert missing_stop.open_risk_available is False
    assert missing_stop.reasons == ("OPEN_RISK_UNAVAILABLE",)
    assert short.committed_gross_exposure == Decimal("200")
    assert short.open_risk_available is False
    assert short.reasons == ("UNSUPPORTED_SHORT_POSITION",)


def test_stop_above_market_contributes_zero_long_open_risk() -> None:
    state = build_portfolio_risk_state(
        "snapshot",
        account(),
        "AAPL",
        (
            PositionRiskInput(
                position=position("AAPL", "10", "100", "1000"),
                active_stop=Decimal("110"),
            ),
        ),
        (),
    )

    assert state.committed_open_risk == 0
    assert state.open_risk_available is True


def test_max_drawdown_builds_complete_symbol_sorted_liquidation_plan() -> None:
    drawdown = calculate_drawdown(
        Decimal("80000"),
        Decimal("100000"),
        load_strategy_config(CONFIG_PATH),
    )
    plan = build_liquidation_plan(
        drawdown,
        (
            position("HYG", "-2.25", "100", "-225"),
            position("AAPL", "1.5", "200", "300"),
            position("ZERO", "0", "10", "0"),
        ),
        NEXT_OPEN,
    )

    assert plan.liquidation_required is True
    assert plan.cancel_open_entry_orders is True
    assert tuple(item.symbol for item in plan.instructions) == ("AAPL", "HYG")
    long_exit, short_exit = plan.instructions
    assert long_exit.side is OrderSide.SELL
    assert long_exit.quantity == Decimal("1.5")
    assert short_exit.side is OrderSide.BUY
    assert short_exit.quantity == Decimal("2.25")
    assert all(item.order_type is OrderType.MARKET for item in plan.instructions)
    assert all(item.time_in_force is TimeInForce.DAY for item in plan.instructions)
    assert all(item.eligible_at == NEXT_OPEN for item in plan.instructions)
    assert all(item.extended_hours is False for item in plan.instructions)
    assert all(item.reason == "MAX_DRAWDOWN_LIQUIDATION" for item in plan.instructions)


def test_sub_threshold_drawdown_does_not_create_liquidation_instructions() -> None:
    drawdown = calculate_drawdown(
        Decimal("81000"),
        Decimal("100000"),
        load_strategy_config(CONFIG_PATH),
    )
    plan = build_liquidation_plan(
        drawdown,
        (position("AAPL", "1", "100", "100"),),
        NEXT_OPEN,
    )

    assert plan.liquidation_required is False
    assert plan.cancel_open_entry_orders is False
    assert plan.instructions == ()


def test_liquidation_plan_rejects_naive_next_open() -> None:
    drawdown = calculate_drawdown(
        Decimal("80000"),
        Decimal("100000"),
        load_strategy_config(CONFIG_PATH),
    )
    with pytest.raises(ValidationError, match="timezone-aware"):
        build_liquidation_plan(
            drawdown,
            (position("AAPL", "1", "100", "100"),),
            datetime(2026, 8, 14, 13, 30),
        )
