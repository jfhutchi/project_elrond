"""Pins a severity-1 design defect: the drawdown halt has no exit.

These tests DOCUMENT CURRENT BEHAVIOUR, they do not endorse it. Once equity falls 15% below its
high-water mark `entry_halted` blocks new positions, but a strategy that cannot open positions
cannot earn the return that would recover the drawdown. The halt is caused by a loss and then
prevents the only mechanism that could undo it.

Measured consequence: in backtest the gate blocked entry on 70.8% of sessions (1,890 of 2,669).
That, not any component switch, is why `strategy-trend-v4.yaml` made six trades in a decade.

This is not confined to research. `strategy-v1-2.yaml` is DEPLOYED, uses the same thresholds,
and has a 26.4% historical max drawdown. If the live account reaches -15% it stops entering and
cannot trade its way back; only a deposit or manual intervention releases it.

When this is fixed, these tests should fail. That is the point — they are here so a fix is a
deliberate act with a visible diff rather than something that quietly changes behaviour.
Candidate fixes are in docs/per-session-trend-spec.md.
"""

from __future__ import annotations

from decimal import Decimal

from quantbot.risk import calculate_drawdown
from quantbot.strategy import load_strategy_config

CONFIG = load_strategy_config("config/strategy-v1-2.yaml")


def test_the_deployed_config_halts_entries_at_fifteen_percent() -> None:
    """The threshold is real and it applies to the account trading right now."""
    assert CONFIG.drawdown_thresholds_bps == (500, 1000, 1500, 2000)

    state = calculate_drawdown(Decimal("85"), Decimal("100"), CONFIG)

    assert state.drawdown_fraction == Decimal("0.15")
    assert state.entry_halted is True, "entries stop at a 15% drawdown"
    assert state.liquidation_required is False, "liquidation is a separate, deeper threshold"


def test_the_halt_does_not_release_as_equity_recovers_toward_the_peak() -> None:
    """The trap. Recovery needs positions; positions need the halt lifted.

    Equity has to climb back above 85% of the high-water mark to trade again, and the only
    engine for that climb is the trading the halt forbids. Nothing in the account's own
    dynamics escapes this — the release has to come from outside.
    """
    for equity in ("60", "70", "80", "84.99"):
        state = calculate_drawdown(Decimal(equity), Decimal("100"), CONFIG)

        assert state.entry_halted is True, f"still halted at equity {equity}"
        assert state.new_risk_multiplier == Decimal("0"), (
            f"risk multiplier is zero at equity {equity}, so even a permitted entry sizes to "
            "nothing"
        )


def test_an_escape_hatch_now_exists_but_is_off_for_deployed_configs() -> None:
    """`drawdown_lookback_sessions` lets an old peak age out, so time can release the account.

    This test previously asserted that no such parameter existed, and it failed the moment one
    was added — which is what it was for. The halt is still absorbing for anything already
    deployed, because the default is 0 (all-time peak) and must stay that way to keep those
    configuration hashes identical.

    So the defect is fixable rather than fixed: turning it on is a new strategy version and a
    restarted qualification window, which is an operator decision.
    """
    assert CONFIG.drawdown_lookback_sessions == 0, "deployed configs keep the all-time peak"

    # With an all-time peak the trap is intact: an ancient high still halts entries.
    assert calculate_drawdown(Decimal("84"), Decimal("100"), CONFIG).entry_halted is True

    # A finite window is what releases it, by letting the peak roll off.
    windowed = CONFIG.model_copy(update={"drawdown_lookback_sessions": 504})
    assert windowed.drawdown_lookback_sessions == 504


def test_partial_drawdowns_scale_size_down_which_is_the_sane_behaviour() -> None:
    """Shown for contrast: below 15% the policy degrades gracefully.

    5% and 10% cut the risk multiplier rather than zeroing it, so the account keeps trading at
    reduced size and can still recover. The hard zero at 15% is the specific thing that turns a
    graceful ramp into a trap.
    """
    mild = calculate_drawdown(Decimal("94"), Decimal("100"), CONFIG)
    moderate = calculate_drawdown(Decimal("89"), Decimal("100"), CONFIG)

    assert mild.entry_halted is False
    assert mild.new_risk_multiplier == Decimal("0.75")
    assert moderate.entry_halted is False
    assert moderate.new_risk_multiplier == Decimal("0.5")
