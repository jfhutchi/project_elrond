"""Component switches must change live decisions, and only then change identity.

The backtest engine could always disable a gate; the live strategy could not, so research
was able to recommend configurations the account had no way to trade. These tests pin both
halves of the fix: switches genuinely alter the decision, and a strategy already deployed
keeps its identity when the capability is added.
"""

from __future__ import annotations

from decimal import Decimal

from quantbot.strategy import (
    SignalAction,
    StrategyConfig,
    configuration_hash,
    evaluate_symbol,
    load_strategy_config,
)
from tests.unit.strategy.test_adaptive_momentum import (
    CONFIG_PATH,
    EVALUATION,
    NEXT_SESSION,
    XNYS_SESSIONS,
    identity_for,
    make_history,
    replace_last,
    roster_for,
)


def _with_components(**switches: bool) -> StrategyConfig:
    base = load_strategy_config(CONFIG_PATH)
    payload = base.model_dump(mode="python")
    payload["components"] = {**payload["components"], **switches}
    return StrategyConfig.model_validate(payload)


def _decide(config: StrategyConfig, asset: list, spy: list) -> object:
    return evaluate_symbol(
        asset,
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        session_sequence=XNYS_SESSIONS,
    )


def test_disabling_the_entry_gate_admits_a_trade_the_gate_blocked() -> None:
    """The gate cycles 1-5 identified as the main source of cash drag."""
    spy = make_history("SPY")
    asset = make_history("QQQ")
    # Price below its own breakout channel: the shipped strategy refuses to enter.
    below_channel = replace_last(asset, asset[-1].close - Decimal("5"))

    gated = _decide(load_strategy_config(CONFIG_PATH), below_channel, spy)
    ungated = _decide(_with_components(donchian_entry=False), below_channel, spy)

    assert "ENTRY_CHANNEL_NOT_BROKEN" in gated.reasons
    assert gated.action is SignalAction.HOLD
    assert "ENTRY_CHANNEL_NOT_BROKEN" not in ungated.reasons
    assert ungated.action is SignalAction.ENTER


def test_disabling_the_trend_filter_removes_only_that_reason() -> None:
    spy = make_history("SPY")
    asset = make_history("QQQ")
    below_trend = replace_last(asset, asset[0].close)

    filtered = _decide(load_strategy_config(CONFIG_PATH), below_trend, spy)
    unfiltered = _decide(_with_components(asset_trend=False), below_trend, spy)

    assert "TREND_FILTER_FAILED" in filtered.reasons
    assert "TREND_FILTER_FAILED" not in unfiltered.reasons


def test_defaults_preserve_the_identity_of_an_already_deployed_strategy() -> None:
    """Adding the capability must not silently re-version a running strategy."""
    shipped = load_strategy_config(CONFIG_PATH)

    assert shipped.components.donchian_entry is True
    assert shipped.components.trailing_stop is True
    # The golden hash in test_adaptive_momentum pins this exact value.
    assert configuration_hash(shipped) == configuration_hash(_with_components())


def test_changing_a_component_changes_the_strategy_identity() -> None:
    shipped = load_strategy_config(CONFIG_PATH)
    altered = _with_components(donchian_entry=False)

    assert configuration_hash(altered) != configuration_hash(shipped)
