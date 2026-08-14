from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from quantbot.config import Settings
from quantbot.domain import BrokerEnvironment, Position, TradingMode
from quantbot.risk import (
    DrawdownState,
    OrderPurpose,
    build_liquidation_plan,
    calculate_drawdown,
    evaluate_order_gate,
)
from quantbot.strategy import load_strategy_config

ROOT = Path(__file__).parents[3]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"
NEXT_OPEN = datetime(2026, 8, 14, 13, 30, tzinfo=UTC)


def safe_paper_settings(**updates: Any) -> Settings:
    payload: dict[str, object] = {
        "TRADING_MODE": TradingMode.PAPER,
        "BROKER_ENVIRONMENT": BrokerEnvironment.PAPER,
        "EXPECTED_ACCOUNT_ID": "PA-123",
        "ALPACA_PAPER_API_KEY": SecretStr("paper-key"),
        "ALPACA_PAPER_API_SECRET": SecretStr("paper-secret"),
        "LIVE_TRADING_ACKNOWLEDGED": False,
        "KILL_SWITCH": False,
        "MARKET_DATA_HEALTHY": True,
        "BROKER_HEALTHY": True,
        "RISK_ENGINE_HEALTHY": True,
        "RECONCILIATION_SUCCESSFUL": True,
    }
    payload.update(updates)
    return Settings(**payload)  # type: ignore[arg-type]


def drawdown(fraction: str) -> DrawdownState:
    return calculate_drawdown(
        Decimal("100000") * (Decimal("1") - Decimal(fraction)),
        Decimal("100000"),
        load_strategy_config(CONFIG_PATH),
    )


def test_safe_paper_entry_is_allowed() -> None:
    decision = evaluate_order_gate(
        safe_paper_settings(),
        reported_account_id="PA-123",
        purpose=OrderPurpose.ENTRY,
        durable_kill_switch_engaged=False,
        market_eligible=True,
        drawdown=drawdown("0"),
        risk_approved=True,
    )

    assert decision.allowed is True
    assert decision.reasons == ()
    assert decision.purpose is OrderPurpose.ENTRY
    assert decision.broker_label == "alpaca"
    assert decision.environment_label == "PAPER"


def test_drawdown_halt_blocks_entries_but_not_strategy_exits() -> None:
    halted = drawdown("0.15")
    entry = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.ENTRY,
        False,
        True,
        drawdown=halted,
        risk_approved=True,
    )
    strategy_exit = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.STRATEGY_EXIT,
        False,
        True,
        drawdown=halted,
        risk_approved=False,
    )

    assert entry.allowed is False
    assert entry.reasons == ("DRAWDOWN_ENTRY_HALT",)
    assert strategy_exit.allowed is True
    assert strategy_exit.reasons == ()


def test_twenty_percent_liquidation_bypasses_entry_caps() -> None:
    breached = drawdown("0.20")
    decision = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.DRAWDOWN_LIQUIDATION,
        False,
        True,
        drawdown=breached,
        risk_approved=False,
    )

    assert breached.liquidation_required is True
    assert decision.allowed is True
    assert decision.reasons == ()


def test_entry_requires_explicit_risk_approval() -> None:
    decision = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.ENTRY,
        False,
        True,
        drawdown=drawdown("0"),
        risk_approved=False,
    )

    assert decision.allowed is False
    assert decision.reasons == ("RISK_NOT_APPROVED",)


def test_durable_kill_switch_and_market_gate_compose_in_stable_order() -> None:
    decision = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.STRATEGY_EXIT,
        durable_kill_switch_engaged=True,
        market_eligible=False,
        drawdown=drawdown("0"),
        risk_approved=True,
    )

    assert decision.allowed is False
    assert decision.reasons == ("KILL_SWITCH_ENGAGED", "MARKET_NOT_ELIGIBLE")


@pytest.mark.parametrize(
    ("updates", "reported_account", "reason"),
    [
        ({"BROKER_HEALTHY": False}, "PA-123", "BROKER_UNHEALTHY"),
        ({"MARKET_DATA_HEALTHY": False}, "PA-123", "MARKET_DATA_UNHEALTHY"),
        ({"RISK_ENGINE_HEALTHY": False}, "PA-123", "RISK_ENGINE_UNHEALTHY"),
        (
            {"RECONCILIATION_SUCCESSFUL": False},
            "PA-123",
            "RECONCILIATION_REQUIRED",
        ),
        (
            {"BROKER_ENVIRONMENT": BrokerEnvironment.LIVE},
            "PA-123",
            "BROKER_ENVIRONMENT_MISMATCH",
        ),
        ({"EXPECTED_ACCOUNT_ID": None}, None, "EXPECTED_ACCOUNT_ID_MISSING"),
        ({}, None, "ACCOUNT_VERIFICATION_UNAVAILABLE"),
        ({}, "WRONG", "ACCOUNT_MISMATCH"),
        ({"ALPACA_PAPER_API_SECRET": None}, "PA-123", "CREDENTIALS_INCOMPLETE"),
    ],
)
def test_operational_safety_failure_blocks_automatic_orders(
    updates: dict[str, object],
    reported_account: str | None,
    reason: str,
) -> None:
    decision = evaluate_order_gate(
        safe_paper_settings(**updates),
        reported_account,
        OrderPurpose.STRATEGY_EXIT,
        False,
        True,
        drawdown=drawdown("0"),
        risk_approved=True,
    )

    assert decision.allowed is False
    assert reason in decision.reasons


def test_live_mode_requires_live_credentials_and_acknowledgement() -> None:
    settings = safe_paper_settings(
        TRADING_MODE=TradingMode.LIVE,
        BROKER_ENVIRONMENT=BrokerEnvironment.LIVE,
        ALPACA_PAPER_API_KEY=None,
        ALPACA_PAPER_API_SECRET=None,
        ALPACA_LIVE_API_KEY=SecretStr("live-key"),
        ALPACA_LIVE_API_SECRET=SecretStr("live-secret"),
        LIVE_TRADING_ACKNOWLEDGED=True,
    )
    allowed = evaluate_order_gate(
        settings,
        "PA-123",
        OrderPurpose.ENTRY,
        False,
        True,
        drawdown=drawdown("0"),
        risk_approved=True,
    )
    blocked = evaluate_order_gate(
        settings.model_copy(update={"LIVE_TRADING_ACKNOWLEDGED": False}),
        "PA-123",
        OrderPurpose.ENTRY,
        False,
        True,
        drawdown=drawdown("0"),
        risk_approved=True,
    )

    assert allowed.allowed is True
    assert blocked.allowed is False
    assert blocked.reasons == ("LIVE_TRADING_NOT_ACKNOWLEDGED",)


def test_blocked_forced_liquidation_remains_required_and_is_not_falsely_executed() -> None:
    breached = drawdown("0.20")
    plan = build_liquidation_plan(
        breached,
        (
            Position(
                symbol="AAPL",
                quantity=Decimal("2"),
                market_price=Decimal("100"),
                market_value=Decimal("200"),
                average_entry_price=Decimal("90"),
            ),
        ),
        NEXT_OPEN,
    )
    gate = evaluate_order_gate(
        safe_paper_settings(),
        "PA-123",
        OrderPurpose.DRAWDOWN_LIQUIDATION,
        durable_kill_switch_engaged=True,
        market_eligible=True,
        drawdown=breached,
        risk_approved=False,
    )

    assert plan.liquidation_required is True
    assert len(plan.instructions) == 1
    assert gate.allowed is False
    assert gate.reasons == ("KILL_SWITCH_ENGAGED",)
