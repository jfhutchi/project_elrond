"""Operational and risk-policy gates for automatic order purposes.

**The portfolio vetoes compose here rather than inside sizing, and that is the point (#58).**
`size_entry` returns a quantity; a veto has no opinion about quantity, and letting a
concentration rule reduce a size would defeat what it is for. So the vetoes join the same
allow/deny the kill switch and the drawdown halt already join, and their reasons travel in the
same list.

**A veto applies to `ENTRY` only.** A rule that blocked a strategy exit or a drawdown
liquidation would trap capital in exactly the state the rule exists to avoid -- the absorbing
halt `STATUS.md` records as a severity-1 defect, rebuilt one layer up.
"""

from quantbot.config import Settings, validate_trading_safety
from quantbot.risk.models import DrawdownState, OrderGateDecision, OrderPurpose
from quantbot.risk.vetoes import PortfolioVetoDecision


def evaluate_order_gate(
    settings: Settings,
    reported_account_id: str | None,
    purpose: OrderPurpose,
    durable_kill_switch_engaged: bool,
    market_eligible: bool,
    *,
    drawdown: DrawdownState | None,
    risk_approved: bool | None,
    vetoes: PortfolioVetoDecision | None = None,
) -> OrderGateDecision:
    """Compose operational safety with purpose-specific risk policy.

    `vetoes` is optional and its absence is **not** treated as approval: an entry evaluated with
    no veto decision reports `PORTFOLIO_VETOES_NOT_EVALUATED` and is refused. A control that
    passes when nobody ran it is the shape this project keeps finding, and the default exists so
    the parameter can be added without rewriting every call site -- not so it can be skipped.
    """
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
        if vetoes is None:
            reasons.append("PORTFOLIO_VETOES_NOT_EVALUATED")
        else:
            # Every refusing veto, not the first. #57 studies rejection *reasons*, and a reason
            # that never reached the decision cannot be evaluated for whether it was worth
            # having.
            reasons.extend(vetoes.reasons)
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
