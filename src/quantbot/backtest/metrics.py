"""Known-formula performance and trade statistics for deterministic backtests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class MetricModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class EquityObservation(MetricModel):
    timestamp: datetime
    equity: Decimal = Field(ge=0, allow_inf_nan=False)
    cash: Decimal = Field(allow_inf_nan=False)
    gross_exposure: Decimal = Field(ge=0, allow_inf_nan=False)
    traded_notional: Decimal = Field(ge=0, allow_inf_nan=False)


class TradeOutcome(MetricModel):
    symbol: str
    opened_at: datetime
    closed_at: datetime
    gross_pnl: Decimal = Field(allow_inf_nan=False)
    net_pnl: Decimal = Field(allow_inf_nan=False)
    costs: Decimal = Field(ge=0, allow_inf_nan=False)
    notional: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_trade(self) -> TradeOutcome:
        if self.closed_at < self.opened_at:
            raise ValueError("trade cannot close before it opens")
        if (
            not self.symbol
            or self.symbol != self.symbol.strip()
            or self.symbol != self.symbol.upper()
        ):
            raise ValueError("trade symbol must be uppercase")
        if self.net_pnl != self.gross_pnl - self.costs:
            raise ValueError("trade net_pnl must equal gross_pnl less costs")
        return self


class BacktestMetrics(MetricModel):
    total_return: Decimal
    cagr_equivalent: Decimal
    sharpe: Decimal | None
    sortino: Decimal | None
    maximum_drawdown: Decimal
    profit_factor: Decimal | None
    expectancy: Decimal | None
    win_rate: Decimal | None
    winners: int
    losers: int
    exposure_fraction: Decimal
    turnover: Decimal
    total_costs: Decimal


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal("0")) / Decimal(len(values))


def _population_deviation(values: tuple[Decimal, ...], mean: Decimal) -> Decimal:
    variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(len(values))
    return variance.sqrt()


def _maximum_drawdown(
    equity: tuple[EquityObservation, ...],
    initial_equity: Decimal | None = None,
) -> Decimal:
    high_water = initial_equity if initial_equity is not None else equity[0].equity
    maximum = Decimal("0")
    for point in equity:
        high_water = max(high_water, point.equity)
        if high_water > 0:
            maximum = max(maximum, (high_water - point.equity) / high_water)
    return maximum


def calculate_metrics(
    equity: tuple[EquityObservation, ...],
    trades: tuple[TradeOutcome, ...],
    *,
    periods_per_year: int = 252,
    total_costs_override: Decimal | None = None,
    initial_equity_override: Decimal | None = None,
) -> BacktestMetrics:
    """Calculate explicitly defined population statistics from ordered observations."""
    if not equity:
        raise ValueError("at least one equity observation is required")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if any(
        current.timestamp <= previous.timestamp
        for previous, current in zip(equity, equity[1:], strict=False)
    ):
        raise ValueError("equity observations must be strictly increasing")
    initial = initial_equity_override if initial_equity_override is not None else equity[0].equity
    final = equity[-1].equity
    if not initial.is_finite() or initial <= 0:
        raise ValueError("initial equity must be positive")
    total_return = final / initial - Decimal("1")
    periods = len(equity) if initial_equity_override is not None else len(equity) - 1
    if periods == 0:
        cagr = Decimal("0")
    elif final == 0:
        cagr = Decimal("-1")
    else:
        years = Decimal(periods) / Decimal(periods_per_year)
        cagr = ((final / initial).ln() / years).exp() - Decimal("1")

    observed_equity = (
        (initial, *(point.equity for point in equity))
        if initial_equity_override is not None
        else tuple(point.equity for point in equity)
    )
    returns = tuple(
        current / previous - Decimal("1")
        for previous, current in zip(observed_equity, observed_equity[1:], strict=False)
        if previous > 0
    )
    sharpe: Decimal | None = None
    sortino: Decimal | None = None
    if returns:
        average_return = _mean(returns)
        deviation = _population_deviation(returns, average_return)
        annualizer = Decimal(periods_per_year).sqrt()
        if deviation > 0:
            sharpe = average_return / deviation * annualizer
        downside = (
            sum((min(value, Decimal("0")) ** 2 for value in returns), Decimal("0"))
            / Decimal(len(returns))
        ).sqrt()
        if downside > 0:
            sortino = average_return / downside * annualizer

    winners = tuple(trade for trade in trades if trade.net_pnl > 0)
    losers = tuple(trade for trade in trades if trade.net_pnl < 0)
    profit_factor = (
        sum((trade.net_pnl for trade in winners), Decimal("0"))
        / abs(sum((trade.net_pnl for trade in losers), Decimal("0")))
        if losers
        else None
    )
    expectancy = _mean(tuple(trade.net_pnl for trade in trades)) if trades else None
    win_rate = Decimal(len(winners)) / Decimal(len(trades)) if trades else None
    exposure = _mean(
        tuple(
            point.gross_exposure / point.equity if point.equity > 0 else Decimal("0")
            for point in equity
        )
    )
    average_equity = _mean(tuple(point.equity for point in equity))
    traded_notional = sum((point.traded_notional for point in equity), Decimal("0"))
    turnover = traded_notional / average_equity if average_equity > 0 else Decimal("0")
    trade_costs = sum((trade.costs for trade in trades), Decimal("0"))
    total_costs = total_costs_override if total_costs_override is not None else trade_costs
    if not total_costs.is_finite() or total_costs < 0:
        raise ValueError("total costs must be nonnegative and finite")
    return BacktestMetrics(
        total_return=total_return,
        cagr_equivalent=cagr,
        sharpe=sharpe,
        sortino=sortino,
        maximum_drawdown=_maximum_drawdown(equity, initial_equity_override),
        profit_factor=profit_factor,
        expectancy=expectancy,
        win_rate=win_rate,
        winners=len(winners),
        losers=len(losers),
        exposure_fraction=exposure,
        turnover=turnover,
        total_costs=total_costs,
    )
