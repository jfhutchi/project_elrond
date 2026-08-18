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

    multiplier = multipliers[tier]
    halted = drawdown >= thresholds[2]
    floor = Decimal(config.drawdown_halt_floor_bps) / BASIS_POINTS
    if halted and floor > 0:
        # Trade on at reduced size instead of stopping. A halted account holds nothing, so its
        # equity is frozen, so its drawdown is frozen — no drawdown-based release can ever fire
        # and only a deposit or a human can free it. Keeping some exposure preserves the only
        # mechanism that can earn the drawdown back. Liquidation at the deepest tier is
        # deliberately left untouched: that is a hard stop, not a throttle.
        multiplier = max(multiplier, floor)
        halted = False

    return DrawdownState(
        current_equity=current_equity,
        high_water_equity=high_water,
        drawdown_fraction=drawdown,
        new_risk_multiplier=multiplier,
        entry_halted=halted,
        liquidation_required=drawdown >= thresholds[3],
    )
