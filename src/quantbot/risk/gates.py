"""Operational and risk-policy gates for automatic order purposes."""

from quantbot.config import Settings, validate_trading_safety
from quantbot.risk.models import DrawdownState, OrderGateDecision, OrderPurpose


def evaluate_order_gate(
    settings: Settings,
    reported_account_id: str | None,
    purpose: OrderPurpose,
    durable_kill_switch_engaged: bool,
    market_eligible: bool,
    *,
    drawdown: DrawdownState | None,
    risk_approved: bool | None,
) -> OrderGateDecision:
    """Compose operational safety with purpose-specific risk policy."""
    effective_settings = settings.model_copy(
        update={"KILL_SWITCH": settings.KILL_SWITCH or durable_kill_switch_engaged}
    )
    safety = validate_trading_safety(effective_settings, reported_account_id)
    reasons = list(safety.reasons)
    if not market_eligible:
        reasons.append("MARKET_NOT_ELIGIBLE")

    if purpose is OrderPurpose.ENTRY:
        if drawdown is None:
            reasons.append("DRAWDOWN_STATE_UNAVAILABLE")
        elif drawdown.entry_halted:
            reasons.append("DRAWDOWN_ENTRY_HALT")
        if risk_approved is not True:
            reasons.append("RISK_NOT_APPROVED")
    elif purpose is OrderPurpose.DRAWDOWN_LIQUIDATION and (
        drawdown is None or not drawdown.liquidation_required
    ):
        reasons.append("LIQUIDATION_NOT_REQUIRED")

    return OrderGateDecision(
        purpose=purpose,
        allowed=not reasons,
        reasons=tuple(reasons),
        broker_label=safety.broker_label,
        environment_label=safety.environment_label,
    )
