"""Fractional-share sizing: the same caps, without the whole-share floor.

Small accounts cannot hold a whole share of most ETFs, so the whole-share floor
silently rejects every entry. Enabling fractional shares must relax only the
rounding, never a risk cap.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantbot.risk import (
    MINIMUM_FRACTIONAL_NOTIONAL,
    EntrySizingRequest,
    PortfolioRiskState,
    RiskApproval,
    RiskRejection,
    calculate_drawdown,
    quantize_quantity,
    size_entry,
)
from quantbot.strategy import StrategyConfig, load_strategy_config

ROOT = Path(__file__).parents[3]
WHOLE_SHARE_CONFIG = ROOT / "config" / "strategy-v1.yaml"
FRACTIONAL_CONFIG = ROOT / "config" / "strategy-v1-1.yaml"


def _portfolio(equity: Decimal, **updates: Any) -> PortfolioRiskState:
    payload: dict[str, object] = {
        "account_snapshot_id": "snapshot-1",
        "target_symbol": "IWM",
        "equity": equity,
        "buying_power": equity,
        "committed_open_risk": Decimal("0"),
        "committed_gross_exposure": Decimal("0"),
        "committed_symbol_market_value": Decimal("0"),
        "committed_position_count": 0,
        "is_new_position": True,
        "open_risk_available": True,
        "reasons": (),
    }
    payload.update(updates)
    return PortfolioRiskState.model_validate(payload)


def _request(
    config: StrategyConfig,
    *,
    equity: Decimal,
    reference_price: Decimal,
    stop_distance: Decimal,
) -> EntrySizingRequest:
    return EntrySizingRequest(
        symbol="IWM",
        reference_price=reference_price,
        stop_distance=stop_distance,
        portfolio=_portfolio(equity),
        drawdown=calculate_drawdown(equity, equity, config),
    )


def test_hundred_dollar_account_cannot_trade_without_fractional_shares() -> None:
    """The blocker this feature exists to remove."""
    config = load_strategy_config(WHOLE_SHARE_CONFIG)

    result = size_entry(
        _request(
            config,
            equity=Decimal("100"),
            reference_price=Decimal("305.09"),
            stop_distance=Decimal("8.03"),
        ),
        config,
    )

    assert isinstance(result, RiskRejection)
    assert "QUANTITY_BELOW_ONE_SHARE" in result.reasons


def test_hundred_dollar_account_trades_with_fractional_shares() -> None:
    config = load_strategy_config(FRACTIONAL_CONFIG)
    equity = Decimal("100")

    result = size_entry(
        _request(
            config,
            equity=equity,
            reference_price=Decimal("305.09"),
            stop_distance=Decimal("8.03"),
        ),
        config,
    )

    assert isinstance(result, RiskApproval)
    assert result.fractional_shares is True
    assert Decimal("0") < result.quantity < Decimal("1")
    # The 10% position cap binds before the 0.5% risk cap for this volatility.
    assert result.projected_position_value <= equity * Decimal("0.10")
    assert result.projected_open_risk <= equity * Decimal("0.005")
    assert result.quantity * Decimal("305.09") >= MINIMUM_FRACTIONAL_NOTIONAL


def test_fractional_sizing_never_exceeds_a_cap_it_previously_respected() -> None:
    """Rounding down is the only behaviour change; every cap still binds."""
    config = load_strategy_config(FRACTIONAL_CONFIG)
    equity = Decimal("100000")

    result = size_entry(
        _request(
            config,
            equity=equity,
            reference_price=Decimal("50"),
            stop_distance=Decimal("4"),
        ),
        config,
    )

    assert isinstance(result, RiskApproval)
    assert result.projected_position_value <= equity * Decimal("0.10")
    assert result.projected_gross_exposure <= equity
    assert result.projected_open_risk <= equity * Decimal("0.05")
    assert result.quantity * result.stop_distance <= result.risk_budget


def test_order_below_the_broker_minimum_notional_is_rejected() -> None:
    config = load_strategy_config(FRACTIONAL_CONFIG)

    result = size_entry(
        _request(
            config,
            equity=Decimal("5"),
            reference_price=Decimal("305.09"),
            stop_distance=Decimal("8.03"),
        ),
        config,
    )

    assert isinstance(result, RiskRejection)
    assert "NOTIONAL_BELOW_MINIMUM" in result.reasons


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("3.9999999"), Decimal("3.999999")),
        (Decimal("0.0656123456"), Decimal("0.065612")),
        (Decimal("7"), Decimal("7")),
    ],
)
def test_fractional_quantization_always_rounds_down(value: Decimal, expected: Decimal) -> None:
    config = load_strategy_config(FRACTIONAL_CONFIG)

    assert quantize_quantity(value, config) == expected


def test_whole_share_quantization_is_unchanged() -> None:
    config = load_strategy_config(WHOLE_SHARE_CONFIG)

    assert quantize_quantity(Decimal("3.9999999"), config) == Decimal("3")
    assert quantize_quantity(Decimal("0.9"), config) == Decimal("0")


def test_fractional_quantity_is_rejected_when_the_strategy_forbids_it() -> None:
    """The model invariant still guards the whole-share strategy version."""
    with pytest.raises(ValueError, match="whole number of shares"):
        RiskApproval(
            symbol="IWM",
            quantity=Decimal("0.5"),
            stop_distance=Decimal("8"),
            drawdown_fraction=Decimal("0"),
            new_risk_multiplier=Decimal("1"),
            effective_risk_fraction=Decimal("0.005"),
            risk_budget=Decimal("1"),
            caps={
                "per_trade_risk": Decimal("1"),
                "portfolio_open_risk": Decimal("1"),
                "position_value": Decimal("1"),
                "gross_exposure": Decimal("1"),
                "buying_power": Decimal("1"),
            },
            limiting_caps=(),
            projected_position_value=Decimal("1"),
            projected_gross_exposure=Decimal("1"),
            projected_open_risk=Decimal("1"),
        )
