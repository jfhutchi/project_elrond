"""Whole-share entry sizing under deterministic portfolio caps."""

from decimal import ROUND_FLOOR, Decimal

from quantbot.risk.drawdown import BASIS_POINTS
from quantbot.risk.models import (
    EntrySizingRequest,
    RiskApproval,
    RiskRejection,
    SizingCapName,
    SizingCaps,
)
from quantbot.strategy.config import StrategyConfig

RiskSizingResult = RiskApproval | RiskRejection

#: Brokers that support fractional shares still refuse orders below a small notional.
MINIMUM_FRACTIONAL_NOTIONAL = Decimal("1")

#: Alpaca accepts nine decimal places on ``qty``; six is ample and keeps values legible.
FRACTIONAL_QUANTITY_EXPONENT = Decimal("0.000001")


def quantize_quantity(quantity: Decimal, config: StrategyConfig) -> Decimal:
    """Round an unconstrained share count down to a quantity the broker will accept.

    Always rounds down, so quantization can only ever reduce exposure below a cap.
    """
    if config.allow_fractional_shares:
        return quantity.quantize(FRACTIONAL_QUANTITY_EXPONENT, rounding=ROUND_FLOOR)
    return quantity.to_integral_value(rounding=ROUND_FLOOR)


def _rejection(
    request: EntrySizingRequest,
    reasons: list[str],
    caps: SizingCaps | None = None,
) -> RiskRejection:
    return RiskRejection(
        symbol=request.symbol,
        reasons=tuple(reasons),
        drawdown_fraction=request.drawdown.drawdown_fraction,
        new_risk_multiplier=request.drawdown.new_risk_multiplier,
        caps=caps,
    )


def _invalid_input_reasons(request: EntrySizingRequest) -> list[str]:
    portfolio = request.portfolio
    reasons: list[str] = []
    if not portfolio.equity.is_finite() or portfolio.equity <= 0:
        reasons.append("NON_POSITIVE_EQUITY")
    if not request.reference_price.is_finite() or request.reference_price <= 0:
        reasons.append("NON_POSITIVE_REFERENCE_PRICE")
    if not request.stop_distance.is_finite() or request.stop_distance <= 0:
        reasons.append("NON_POSITIVE_STOP_DISTANCE")
    if not portfolio.buying_power.is_finite() or portfolio.buying_power < 0:
        reasons.append("NEGATIVE_BUYING_POWER")
    if not portfolio.committed_open_risk.is_finite() or portfolio.committed_open_risk < 0:
        reasons.append("NEGATIVE_COMMITTED_OPEN_RISK")
    if not portfolio.committed_gross_exposure.is_finite() or portfolio.committed_gross_exposure < 0:
        reasons.append("NEGATIVE_COMMITTED_GROSS_EXPOSURE")
    if (
        not portfolio.committed_symbol_market_value.is_finite()
        or portfolio.committed_symbol_market_value < 0
    ):
        reasons.append("NEGATIVE_COMMITTED_SYMBOL_VALUE")
    if portfolio.committed_position_count < 0:
        reasons.append("INVALID_POSITION_COUNT")
    for reason in ("OPEN_RISK_UNAVAILABLE", "UNSUPPORTED_SHORT_POSITION"):
        if reason in portfolio.reasons:
            reasons.append(reason)
    if not portfolio.open_risk_available and not any(
        reason in reasons for reason in ("OPEN_RISK_UNAVAILABLE", "UNSUPPORTED_SHORT_POSITION")
    ):
        reasons.append("OPEN_RISK_UNAVAILABLE")
    return reasons


def size_entry(request: EntrySizingRequest, config: StrategyConfig) -> RiskSizingResult:
    """Return an approval only when every nominal risk cap permits at least one share."""
    invalid_reasons = _invalid_input_reasons(request)
    if invalid_reasons:
        return _rejection(request, invalid_reasons)
    if request.drawdown.entry_halted:
        return _rejection(request, ["DRAWDOWN_ENTRY_HALT"])

    portfolio = request.portfolio
    if portfolio.is_new_position and portfolio.committed_position_count >= config.max_positions:
        return _rejection(request, ["POSITION_LIMIT_REACHED"])
    if not portfolio.is_new_position and not config.allow_pyramiding:
        return _rejection(request, ["PYRAMIDING_DISABLED"])

    risk_fraction = Decimal(config.risk_per_trade_bps) / BASIS_POINTS
    max_open_risk_fraction = Decimal(config.max_open_risk_bps) / BASIS_POINTS
    max_position_fraction = Decimal(config.max_position_value_bps) / BASIS_POINTS
    max_gross_fraction = Decimal(config.max_gross_exposure_bps) / BASIS_POINTS
    effective_risk_fraction = risk_fraction * request.drawdown.new_risk_multiplier
    risk_budget = portfolio.equity * effective_risk_fraction

    open_risk_remaining = max(
        Decimal("0"),
        portfolio.equity * max_open_risk_fraction - portfolio.committed_open_risk,
    )
    position_value_remaining = max(
        Decimal("0"),
        portfolio.equity * max_position_fraction - portfolio.committed_symbol_market_value,
    )
    gross_remaining = max(
        Decimal("0"),
        portfolio.equity * max_gross_fraction - portfolio.committed_gross_exposure,
    )
    caps = SizingCaps(
        per_trade_risk=risk_budget / request.stop_distance,
        portfolio_open_risk=open_risk_remaining / request.stop_distance,
        position_value=position_value_remaining / request.reference_price,
        gross_exposure=gross_remaining / request.reference_price,
        buying_power=max(Decimal("0"), portfolio.buying_power) / request.reference_price,
    )
    ordered_caps = (
        (SizingCapName.PER_TRADE_RISK, caps.per_trade_risk),
        (SizingCapName.PORTFOLIO_OPEN_RISK, caps.portfolio_open_risk),
        (SizingCapName.POSITION_VALUE, caps.position_value),
        (SizingCapName.GROSS_EXPOSURE, caps.gross_exposure),
        (SizingCapName.BUYING_POWER, caps.buying_power),
    )
    minimum = min(value for _, value in ordered_caps)
    quantity = quantize_quantity(minimum, config)

    exhausted_reasons = {
        SizingCapName.PER_TRADE_RISK: "TRADE_RISK_BUDGET_EXHAUSTED",
        SizingCapName.PORTFOLIO_OPEN_RISK: "PORTFOLIO_OPEN_RISK_LIMIT_REACHED",
        SizingCapName.POSITION_VALUE: "POSITION_VALUE_LIMIT_REACHED",
        SizingCapName.GROSS_EXPOSURE: "GROSS_EXPOSURE_LIMIT_REACHED",
        SizingCapName.BUYING_POWER: "BUYING_POWER_EXHAUSTED",
    }
    reasons = [exhausted_reasons[name] for name, value in ordered_caps if value <= 0]
    if config.allow_fractional_shares:
        if quantity * request.reference_price < MINIMUM_FRACTIONAL_NOTIONAL:
            reasons.append("NOTIONAL_BELOW_MINIMUM")
    elif quantity < 1:
        reasons.append("QUANTITY_BELOW_ONE_SHARE")
    if reasons:
        return _rejection(request, reasons, caps)

    limiting_caps = tuple(name for name, value in ordered_caps if value == minimum)
    position_value = quantity * request.reference_price
    return RiskApproval(
        symbol=request.symbol,
        quantity=quantity,
        fractional_shares=config.allow_fractional_shares,
        stop_distance=request.stop_distance,
        drawdown_fraction=request.drawdown.drawdown_fraction,
        new_risk_multiplier=request.drawdown.new_risk_multiplier,
        effective_risk_fraction=effective_risk_fraction,
        risk_budget=risk_budget,
        caps=caps,
        limiting_caps=limiting_caps,
        projected_position_value=portfolio.committed_symbol_market_value + position_value,
        projected_gross_exposure=portfolio.committed_gross_exposure + position_value,
        projected_open_risk=portfolio.committed_open_risk + quantity * request.stop_distance,
    )
