"""Volatility-targeted sizing: the cap, and the identity guarantee that lets it ship.

Cycle 12's justification for this is narrow and worth stating precisely, because a wider claim
would be false. It is NOT alpha — that was refuted at z=1.01, p=0.31. It is constraint relief:
under this project's 20% drawdown halt, vol targeting carries 0.75x exposure where raw SPY
affords only 0.50x, ending at $403.67 against $281.02 per $100.

The most important test here is the last one. Adding this feature must not re-version a
strategy that is already trading real (paper) money.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantbot.domain import Account
from quantbot.risk import (
    EntrySizingRequest,
    RiskApproval,
    RiskRejection,
    SizingCapName,
    build_portfolio_risk_state,
    calculate_drawdown,
    size_entry,
)
from quantbot.strategy import load_strategy_config, strategy_id_for

CONFIG = "config/strategy-v1-2.yaml"


def _request(config, *, vol: Decimal | None, equity: str = "10000") -> EntrySizingRequest:
    account = Account(
        account_id="acct-test",
        cash=Decimal(equity),
        buying_power=Decimal(equity),
        equity=Decimal(equity),
        currency="USD",
    )
    portfolio = build_portfolio_risk_state("run-test", account, "SPY", [], [])
    return EntrySizingRequest(
        symbol="SPY",
        reference_price=Decimal("100"),
        stop_distance=Decimal("5"),
        portfolio=portfolio,
        drawdown=calculate_drawdown(Decimal(equity), None, config),
        realized_volatility=vol,
    )


def _with_target(target_bps: int):
    base = load_strategy_config(CONFIG)
    return base.model_copy(update={"volatility_target_bps": target_bps})


def test_disabled_by_default_so_existing_behaviour_is_untouched() -> None:
    config = load_strategy_config(CONFIG)
    assert config.volatility_target_bps == 0

    result = size_entry(_request(config, vol=None), config)

    assert isinstance(result, RiskApproval)
    assert result.caps.volatility_target is None
    assert SizingCapName.VOLATILITY_TARGET not in result.limiting_caps


def test_the_cap_scales_inversely_with_volatility() -> None:
    """The mechanism itself: the allowance moves as target/realised."""
    config = _with_target(1500)  # 15% annualised target

    at_target = size_entry(_request(config, vol=Decimal("0.15")), config)
    quadruple = size_entry(_request(config, vol=Decimal("0.60")), config)

    assert isinstance(at_target, RiskApproval)
    assert isinstance(quadruple, RiskApproval)
    assert at_target.caps.volatility_target is not None
    assert quadruple.caps.volatility_target == at_target.caps.volatility_target / 4


def test_turbulence_actually_reduces_the_order_once_the_cap_binds() -> None:
    """Scaling the cap is useless unless it becomes the smallest one and shrinks the order.

    At moderate volatility the 10% position-value cap is tighter, so the order is unchanged —
    which is correct, and is why this test drives volatility high enough to bind.
    """
    config = _with_target(1500)

    calm = size_entry(_request(config, vol=Decimal("0.15")), config)
    storm = size_entry(_request(config, vol=Decimal("3.0")), config)

    assert isinstance(calm, RiskApproval)
    assert isinstance(storm, RiskApproval)
    assert storm.quantity < calm.quantity
    assert SizingCapName.VOLATILITY_TARGET in storm.limiting_caps


def test_a_crisis_makes_the_volatility_cap_the_binding_one() -> None:
    """In a crisis this must be what limits size, or it is not doing its job."""
    config = _with_target(1500)

    result = size_entry(_request(config, vol=Decimal("6.0")), config)

    if isinstance(result, RiskApproval):
        assert result.limiting_caps == (SizingCapName.VOLATILITY_TARGET,)
    else:
        assert "VOLATILITY_TARGET_REACHED" in result.reasons


def test_missing_volatility_fails_closed_rather_than_sizing_as_if_disabled() -> None:
    """Silently skipping the cap would size a turbulent name as though it were calm."""
    config = _with_target(1500)

    result = size_entry(_request(config, vol=None), config)

    assert isinstance(result, RiskRejection)
    assert "REALIZED_VOLATILITY_UNAVAILABLE" in result.reasons


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-0.2")])
def test_nonsensical_volatility_is_rejected(bad: Decimal) -> None:
    config = _with_target(1500)

    result = size_entry(_request(config, vol=bad), config)

    assert isinstance(result, RiskRejection)
    assert "REALIZED_VOLATILITY_UNAVAILABLE" in result.reasons


def test_adding_this_feature_did_not_reversion_any_deployed_strategy() -> None:
    """The one that matters: v1.2.0 is trading now, and its identity must not move.

    These hashes were recorded from the deployed configs before volatility targeting existed.
    A change here means a live strategy was silently re-versioned, which breaks every claim
    the qualification window is trying to establish.
    """
    expected = {
        "config/strategy-v1.yaml": "adaptive-momentum-v1-7d04bc9cc0cb20e6",
        "config/strategy-v1-1.yaml": "adaptive-momentum-v1-b73083b817f76b8f",
        "config/strategy-v1-2.yaml": "adaptive-momentum-v1-309894d8d8a5296e",
        "config/strategy-crypto-v2.yaml": "adaptive-momentum-v2-f4ede5cd70bab7ca",
    }
    for path, identity in expected.items():
        assert strategy_id_for(load_strategy_config(path)) == identity, path


def test_enabling_the_target_does_produce_a_new_identity() -> None:
    """The other half: a real behaviour change must not hide inside an existing version."""
    base = load_strategy_config(CONFIG)

    assert strategy_id_for(_with_target(1500)) != strategy_id_for(base)
