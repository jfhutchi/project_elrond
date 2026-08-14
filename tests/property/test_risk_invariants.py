from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from quantbot.domain import Account, Position
from quantbot.risk import (
    EntrySizingRequest,
    PortfolioRiskState,
    PositionRiskInput,
    RiskApproval,
    build_liquidation_plan,
    build_portfolio_risk_state,
    calculate_drawdown,
    size_entry,
)
from quantbot.strategy import load_strategy_config

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"
CONFIG = load_strategy_config(CONFIG_PATH)
NEXT_OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=UTC)


def portfolio(
    *,
    equity: Decimal = Decimal("100000"),
    buying_power: Decimal = Decimal("100000"),
    open_risk: Decimal = Decimal("0"),
    gross: Decimal = Decimal("0"),
    symbol_value: Decimal = Decimal("0"),
) -> PortfolioRiskState:
    return PortfolioRiskState(
        account_snapshot_id="snapshot",
        target_symbol="AAPL",
        equity=equity,
        buying_power=buying_power,
        committed_open_risk=open_risk,
        committed_gross_exposure=gross,
        committed_symbol_market_value=symbol_value,
        committed_position_count=0,
        is_new_position=True,
        open_risk_available=True,
        reasons=(),
    )


def quantity_for(
    drawdown_bps: int,
    *,
    portfolio_state: PortfolioRiskState | None = None,
) -> Decimal:
    drawdown = calculate_drawdown(
        Decimal("100000") * (Decimal("1") - Decimal(drawdown_bps) / Decimal("10000")),
        Decimal("100000"),
        CONFIG,
    )
    result = size_entry(
        EntrySizingRequest(
            symbol="AAPL",
            reference_price=Decimal("50"),
            stop_distance=Decimal("4"),
            portfolio=portfolio_state or portfolio(),
            drawdown=drawdown,
        ),
        CONFIG,
    )
    return result.quantity if isinstance(result, RiskApproval) else Decimal("0")


@given(
    equity=st.integers(min_value=1000, max_value=1_000_000),
    price=st.integers(min_value=1, max_value=1000),
    stop=st.integers(min_value=1, max_value=500),
    buying_power=st.integers(min_value=0, max_value=1_000_000),
    open_risk_bps=st.integers(min_value=0, max_value=600),
    gross_bps=st.integers(min_value=0, max_value=11000),
    symbol_bps=st.integers(min_value=0, max_value=1100),
    drawdown_bps=st.integers(min_value=0, max_value=2500),
)
@settings(max_examples=40, deadline=None)
def test_every_approval_respects_all_nominal_caps(
    equity: int,
    price: int,
    stop: int,
    buying_power: int,
    open_risk_bps: int,
    gross_bps: int,
    symbol_bps: int,
    drawdown_bps: int,
) -> None:
    equity_decimal = Decimal(equity)
    state = portfolio(
        equity=equity_decimal,
        buying_power=Decimal(buying_power),
        open_risk=equity_decimal * Decimal(open_risk_bps) / Decimal("10000"),
        gross=equity_decimal * Decimal(gross_bps) / Decimal("10000"),
        symbol_value=equity_decimal * Decimal(symbol_bps) / Decimal("10000"),
    )
    drawdown = calculate_drawdown(
        equity_decimal * (Decimal("1") - Decimal(drawdown_bps) / Decimal("10000")),
        equity_decimal,
        CONFIG,
    )
    result = size_entry(
        EntrySizingRequest(
            symbol="AAPL",
            reference_price=Decimal(price),
            stop_distance=Decimal(stop),
            portfolio=state,
            drawdown=drawdown,
        ),
        CONFIG,
    )

    if not isinstance(result, RiskApproval):
        return
    assert result.quantity >= 1
    assert result.quantity == result.quantity.to_integral_value()
    assert result.quantity * Decimal(price) <= state.buying_power
    assert result.projected_position_value <= equity_decimal * Decimal("0.10")
    assert result.projected_gross_exposure <= equity_decimal
    assert result.projected_open_risk <= equity_decimal * Decimal("0.05")
    assert result.quantity * Decimal(stop) <= result.risk_budget


@given(
    first=st.integers(min_value=0, max_value=2500),
    increment=st.integers(min_value=0, max_value=2500),
)
@settings(max_examples=40, deadline=None)
def test_increasing_drawdown_never_increases_quantity(first: int, increment: int) -> None:
    second = min(2500, first + increment)
    assert quantity_for(second) <= quantity_for(first)


@given(
    open_risk=st.integers(min_value=0, max_value=5000),
    gross=st.integers(min_value=0, max_value=100000),
    symbol_value=st.integers(min_value=0, max_value=10000),
    buying_power=st.integers(min_value=0, max_value=100000),
    increment=st.integers(min_value=0, max_value=10000),
)
@settings(max_examples=40, deadline=None)
def test_worsening_any_portfolio_cap_never_increases_quantity(
    open_risk: int,
    gross: int,
    symbol_value: int,
    buying_power: int,
    increment: int,
) -> None:
    baseline = portfolio(
        open_risk=Decimal(open_risk),
        gross=Decimal(gross),
        symbol_value=Decimal(symbol_value),
        buying_power=Decimal(buying_power),
    )
    baseline_quantity = quantity_for(0, portfolio_state=baseline)
    worsened_states = (
        baseline.model_copy(update={"committed_open_risk": Decimal(open_risk + increment)}),
        baseline.model_copy(update={"committed_gross_exposure": Decimal(gross + increment)}),
        baseline.model_copy(
            update={"committed_symbol_market_value": Decimal(symbol_value + increment)}
        ),
        baseline.model_copy(update={"buying_power": Decimal(max(0, buying_power - increment))}),
    )

    assert all(
        quantity_for(0, portfolio_state=worsened) <= baseline_quantity
        for worsened in worsened_states
    )


@given(drawdown_bps=st.integers(min_value=0, max_value=5000))
def test_liquidation_requirement_is_exactly_twenty_percent(drawdown_bps: int) -> None:
    state = calculate_drawdown(
        Decimal("100000") * (Decimal("1") - Decimal(drawdown_bps) / Decimal("10000")),
        Decimal("100000"),
        CONFIG,
    )
    assert state.liquidation_required is (drawdown_bps >= 2000)


POSITIONS = (
    Position(
        symbol="AAPL",
        quantity=Decimal("2"),
        market_price=Decimal("100"),
        market_value=Decimal("200"),
        average_entry_price=Decimal("90"),
    ),
    Position(
        symbol="HYG",
        quantity=Decimal("3"),
        market_price=Decimal("50"),
        market_value=Decimal("150"),
        average_entry_price=Decimal("45"),
    ),
    Position(
        symbol="QQQ",
        quantity=Decimal("1"),
        market_price=Decimal("400"),
        market_value=Decimal("400"),
        average_entry_price=Decimal("350"),
    ),
)


@given(permuted=st.permutations(POSITIONS))
def test_position_permutation_cannot_change_totals_or_liquidation(
    permuted: list[Position],
) -> None:
    account = Account(
        account_id="PA-123",
        cash=Decimal("90000"),
        buying_power=Decimal("90000"),
        equity=Decimal("100000"),
        currency="USD",
    )
    baseline_inputs = tuple(
        PositionRiskInput(position=item, active_stop=item.market_price - Decimal("10"))
        for item in POSITIONS
    )
    permuted_inputs = tuple(
        PositionRiskInput(position=item, active_stop=item.market_price - Decimal("10"))
        for item in permuted
    )
    baseline = build_portfolio_risk_state("snapshot", account, "GLD", baseline_inputs, ())
    changed = build_portfolio_risk_state("snapshot", account, "GLD", permuted_inputs, ())
    breached = calculate_drawdown(Decimal("80000"), Decimal("100000"), CONFIG)

    assert changed == baseline
    assert build_liquidation_plan(breached, permuted, NEXT_OPEN) == build_liquidation_plan(
        breached,
        POSITIONS,
        NEXT_OPEN,
    )
