"""Pure forward-ledger performance analysis and benchmark comparison."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quantbot.backtest.metrics import TradeOutcome
from quantbot.domain import Fill, OrderSide

BENCHMARK_NAMES = (
    "SPY buy-and-hold",
    "SMA200",
    "Donchian baseline",
    "pure 12-1 momentum",
    "momentum + trend",
    "full strategy",
)


class ReportingDataError(ValueError):
    """Raised when durable forward observations cannot form a valid long-only ledger."""


class PerformanceModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class PositionTransition(PerformanceModel):
    symbol: str
    occurred_at: datetime


class RealizedMovement(PerformanceModel):
    symbol: str
    occurred_at: datetime
    amount: Decimal = Field(allow_inf_nan=False)


class FillLedgerAnalysis(PerformanceModel):
    completed_trades: tuple[TradeOutcome, ...]
    entries: tuple[PositionTransition, ...]
    exits: tuple[PositionTransition, ...]
    realized_movements: tuple[RealizedMovement, ...]


class _OpenCycle:
    def __init__(self, fill: Fill) -> None:
        self.quantity = fill.quantity
        self.average_price = fill.price
        self.opened_at = fill.occurred_at
        self.entry_notional = fill.quantity * fill.price
        self.cycle_gross = Decimal("0")
        self.cycle_costs = fill.fee


def analyze_fills(fills: tuple[Fill, ...]) -> FillLedgerAnalysis:
    """Reconstruct long-only position cycles from immutable broker-confirmed fills."""
    identifiers = [fill.fill_id for fill in fills]
    if len(identifiers) != len(set(identifiers)):
        raise ReportingDataError("fill identifiers must be unique")
    ordered = tuple(sorted(fills, key=lambda fill: (fill.occurred_at, fill.fill_id)))
    open_cycles: dict[str, _OpenCycle] = {}
    entries: list[PositionTransition] = []
    exits: list[PositionTransition] = []
    movements: list[RealizedMovement] = []
    trades: list[TradeOutcome] = []

    for fill in ordered:
        cycle = open_cycles.get(fill.symbol)
        if fill.side is OrderSide.BUY:
            movements.append(
                RealizedMovement(
                    symbol=fill.symbol,
                    occurred_at=fill.occurred_at,
                    amount=-fill.fee,
                )
            )
            if cycle is None:
                open_cycles[fill.symbol] = _OpenCycle(fill)
                entries.append(
                    PositionTransition(symbol=fill.symbol, occurred_at=fill.occurred_at)
                )
                continue
            total_notional = cycle.quantity * cycle.average_price + fill.quantity * fill.price
            cycle.quantity += fill.quantity
            cycle.average_price = total_notional / cycle.quantity
            cycle.entry_notional += fill.quantity * fill.price
            cycle.cycle_costs += fill.fee
            continue

        if cycle is None or fill.quantity > cycle.quantity:
            raise ReportingDataError(
                f"sell fill exceeds the durable long position for {fill.symbol}"
            )
        gross = (fill.price - cycle.average_price) * fill.quantity
        cycle.cycle_gross += gross
        cycle.cycle_costs += fill.fee
        movements.append(
            RealizedMovement(
                symbol=fill.symbol,
                occurred_at=fill.occurred_at,
                amount=gross - fill.fee,
            )
        )
        cycle.quantity -= fill.quantity
        if cycle.quantity != 0:
            continue
        exits.append(PositionTransition(symbol=fill.symbol, occurred_at=fill.occurred_at))
        trades.append(
            TradeOutcome(
                symbol=fill.symbol,
                opened_at=cycle.opened_at,
                closed_at=fill.occurred_at,
                gross_pnl=cycle.cycle_gross,
                net_pnl=cycle.cycle_gross - cycle.cycle_costs,
                costs=cycle.cycle_costs,
                notional=cycle.entry_notional,
            )
        )
        del open_cycles[fill.symbol]

    return FillLedgerAnalysis(
        completed_trades=tuple(trades),
        entries=tuple(entries),
        exits=tuple(exits),
        realized_movements=tuple(movements),
    )


def render_benchmark_comparison(results: dict[str, Decimal | None]) -> str:
    """Render the six immutable research variants without inventing missing results."""
    if set(results) != set(BENCHMARK_NAMES):
        raise ValueError("benchmark comparison must contain exactly the six required variants")
    lines = ["| Variant | Total return |", "|---|---:|"]
    for name in BENCHMARK_NAMES:
        value = results[name]
        rendered = "NOT_YET_OBSERVED" if value is None else f"{value * Decimal('100'):.4f}%"
        lines.append(f"| {name} | {rendered} |")
    return "\n".join(lines) + "\n"
