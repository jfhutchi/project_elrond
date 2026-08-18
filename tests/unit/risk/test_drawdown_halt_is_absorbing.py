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


def test_the_high_water_mark_never_decays_so_the_halt_never_times_out() -> None:
    """No lookback window: an old peak governs forever.

    This is what makes the halt absorbing rather than merely severe. A peak set years ago is
    still the reference, so time alone never releases the account.
    """
    ancient_peak = Decimal("100")

    state = calculate_drawdown(Decimal("84"), ancient_peak, CONFIG)

    assert state.entry_halted is True
    # There is no parameter that would age the peak out. Its absence is the defect.
    assert not hasattr(CONFIG, "drawdown_lookback_sessions")


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
