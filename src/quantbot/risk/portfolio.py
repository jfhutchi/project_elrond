"""Broker-authoritative portfolio risk aggregation and liquidation planning."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal

from quantbot.domain import Account, OrderSide, Position
from quantbot.risk.models import (
    DrawdownState,
    LiquidationInstruction,
    LiquidationPlan,
    PortfolioRiskState,
    PositionRiskInput,
    RiskReservation,
)


def _validate_symbol(symbol: str) -> None:
    if not symbol or symbol != symbol.strip() or symbol != symbol.upper():
        raise ValueError("target_symbol must be uppercase")


def _deduplicate_reservations(
    reservations: Sequence[RiskReservation],
) -> tuple[RiskReservation, ...]:
    unique: dict[str, RiskReservation] = {}
    for reservation in reservations:
        existing = unique.get(reservation.reservation_id)
        if existing is None:
            unique[reservation.reservation_id] = reservation
        elif existing != reservation:
            raise ValueError(f"conflicting reservation {reservation.reservation_id}")
    return tuple(unique.values())


def build_portfolio_risk_state(
    account_snapshot_id: str,
    account: Account,
    target_symbol: str,
    positions: Sequence[PositionRiskInput],
    reservations: Sequence[RiskReservation],
) -> PortfolioRiskState:
    """Aggregate confirmed holdings and in-flight risk reservations exactly once."""
    if not account_snapshot_id or account_snapshot_id != account_snapshot_id.strip():
        raise ValueError("account_snapshot_id must be nonempty without surrounding whitespace")
    _validate_symbol(target_symbol)

    position_symbols = [item.position.symbol for item in positions]
    if len(position_symbols) != len(set(position_symbols)):
        raise ValueError("positions must contain unique symbols")
    unique_reservations = _deduplicate_reservations(reservations)

    committed_open_risk = Decimal("0")
    committed_gross = Decimal("0")
    committed_symbol_value = Decimal("0")
    missing_stop = False
    unsupported_short = False
    held_symbols: set[str] = set()

    for item in positions:
        broker_position = item.position
        if broker_position.quantity == 0:
            continue
        held_symbols.add(broker_position.symbol)
        market_value = abs(broker_position.market_value)
        committed_gross += market_value
        if broker_position.symbol == target_symbol:
            committed_symbol_value += market_value
        if broker_position.quantity < 0:
            unsupported_short = True
            continue
        if item.active_stop is None:
            missing_stop = True
            continue
        risk_per_share = max(broker_position.market_price - item.active_stop, Decimal("0"))
        committed_open_risk += broker_position.quantity * risk_per_share

    reserved_slot_symbols: set[str] = set()
    for reservation in unique_reservations:
        committed_open_risk += reservation.open_risk
        committed_gross += reservation.gross_exposure
        if reservation.symbol == target_symbol:
            committed_symbol_value += reservation.position_value
        if reservation.reserves_position_slot and reservation.symbol not in held_symbols:
            reserved_slot_symbols.add(reservation.symbol)

    reasons: list[str] = []
    if missing_stop:
        reasons.append("OPEN_RISK_UNAVAILABLE")
    if unsupported_short:
        reasons.append("UNSUPPORTED_SHORT_POSITION")
    committed_symbols = held_symbols | reserved_slot_symbols

    return PortfolioRiskState(
        account_snapshot_id=account_snapshot_id,
        target_symbol=target_symbol,
        equity=account.equity,
        buying_power=account.buying_power,
        committed_open_risk=committed_open_risk,
        committed_gross_exposure=committed_gross,
        committed_symbol_market_value=committed_symbol_value,
        committed_position_count=len(committed_symbols),
        is_new_position=target_symbol not in committed_symbols,
        open_risk_available=not reasons,
        reasons=tuple(reasons),
    )


def build_liquidation_plan(
    drawdown: DrawdownState,
    positions: Sequence[Position],
    next_regular_open: datetime,
) -> LiquidationPlan:
    """Plan a broker-confirmed full liquidation for the next regular-session open."""
    symbols = [position.symbol for position in positions]
    if len(symbols) != len(set(symbols)):
        raise ValueError("positions must contain unique symbols")

    instructions: list[LiquidationInstruction] = []
    if drawdown.liquidation_required:
        for broker_position in sorted(positions, key=lambda item: item.symbol):
            if broker_position.quantity == 0:
                continue
            instructions.append(
                LiquidationInstruction(
                    symbol=broker_position.symbol,
                    side=(OrderSide.SELL if broker_position.quantity > 0 else OrderSide.BUY),
                    quantity=abs(broker_position.quantity),
                    eligible_at=next_regular_open,
                )
            )

    return LiquidationPlan(
        drawdown_fraction=drawdown.drawdown_fraction,
        liquidation_required=drawdown.liquidation_required,
        cancel_open_entry_orders=drawdown.liquidation_required,
        instructions=tuple(instructions),
    )
