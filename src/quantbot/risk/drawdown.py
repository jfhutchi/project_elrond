"""High-water drawdown policy with deterministic risk tiers."""

from decimal import Decimal

from quantbot.risk.models import DrawdownState
from quantbot.strategy.config import StrategyConfig

BASIS_POINTS = Decimal("10000")


class DrawdownInputError(ValueError):
    """Raised when a drawdown state cannot be calculated safely."""


def calculate_drawdown(
    current_equity: Decimal,
    previous_high_water: Decimal | None,
    config: StrategyConfig,
) -> DrawdownState:
    """Calculate current drawdown without persisted halt state or hysteresis."""
    if not current_equity.is_finite() or current_equity < 0:
        raise DrawdownInputError("current_equity must be finite and nonnegative")
    if previous_high_water is not None and (
        not previous_high_water.is_finite() or previous_high_water <= 0
    ):
        raise DrawdownInputError("previous_high_water must be finite and positive")

    high_water = max(previous_high_water or current_equity, current_equity)
    if high_water <= 0:
        raise DrawdownInputError("high-water equity is unavailable")

    drawdown = (high_water - current_equity) / high_water
    thresholds = tuple(Decimal(value) / BASIS_POINTS for value in config.drawdown_thresholds_bps)
    multipliers = tuple(Decimal(value) / BASIS_POINTS for value in config.drawdown_multipliers_bps)
    tier = sum(drawdown >= threshold for threshold in thresholds)

    return DrawdownState(
        current_equity=current_equity,
        high_water_equity=high_water,
        drawdown_fraction=drawdown,
        new_risk_multiplier=multipliers[tier],
        entry_halted=drawdown >= thresholds[2],
        liquidation_required=drawdown >= thresholds[3],
    )
