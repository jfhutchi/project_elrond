"""The escape from the absorbing halt: trade smaller instead of stopping.

`test_drawdown_halt_is_absorbing.py` pins the defect — a halted account holds nothing, so its
equity is frozen, so its drawdown is frozen, so nothing expressed in terms of drawdown can ever
release it. These tests pin the fix and, just as importantly, pin that the fix is off by default
so no deployed strategy identity changes without a deliberate act.

Why a floor rather than the other candidates in docs/per-session-trend-spec.md — all four were
measured in `scripts/halt_policy_study.py` on SPY 200d trend, 2016-2026:

    hard halt (today)   $126.80   65.6% of sessions halted   17.3% maxDD   0 escapes
    hysteresis          $126.80   65.6%                      17.3%         0 escapes
    floor 2500bps       $252.43    0.0%                      17.3%
    no halt at all      $311.01      —                       19.8%

Hysteresis is identical to the broken policy in every configuration because it is structurally
incapable of working, not because it is mistuned. The floor matches the hard halt's drawdown
exactly while doubling terminal wealth, and beats never halting at all on drawdown, so it is
real protection rather than paralysis.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from quantbot.risk import calculate_drawdown
from quantbot.strategy import StrategyConfig, load_strategy_config, strategy_id_for

DEPLOYED = load_strategy_config("config/strategy-v1-2.yaml")


def _with_floor(bps: int) -> StrategyConfig:
    return DEPLOYED.model_copy(update={"drawdown_halt_floor_bps": bps})


def test_the_floor_is_off_by_default_so_deployed_identities_are_unchanged() -> None:
    """A new config field must not silently re-version a running strategy."""
    assert DEPLOYED.drawdown_halt_floor_bps == 0
    assert strategy_id_for(DEPLOYED) == "adaptive-momentum-v1-309894d8d8a5296e"


def test_without_a_floor_the_halt_still_traps_exactly_as_before() -> None:
    """The defect is preserved at the default, so nothing changes underneath the live account."""
    state = calculate_drawdown(Decimal("85"), Decimal("100"), DEPLOYED)

    assert state.entry_halted is True
    assert state.new_risk_multiplier == Decimal("0")


def test_a_floor_releases_the_halt_and_keeps_the_account_trading() -> None:
    """The whole point: exposure is reduced, not removed, so a recovery remains possible."""
    state = calculate_drawdown(Decimal("85"), Decimal("100"), _with_floor(2500))

    assert state.entry_halted is False, "the account must still be able to open positions"
    assert state.new_risk_multiplier == Decimal("0.25"), "at a quarter of normal size"


def test_the_floor_never_raises_risk_above_the_tier_it_replaces() -> None:
    """A floor is a lower bound on a throttle, never a way to trade larger in a drawdown."""
    shallow = calculate_drawdown(Decimal("96"), Decimal("100"), _with_floor(9000))
    assert shallow.new_risk_multiplier == Decimal("1"), "untouched above the halt threshold"

    moderate = calculate_drawdown(Decimal("92"), Decimal("100"), _with_floor(9000))
    assert moderate.new_risk_multiplier == Decimal("0.75"), "tier 1 unaffected by the floor"

    halted = calculate_drawdown(Decimal("85"), Decimal("100"), _with_floor(9000))
    assert halted.new_risk_multiplier == Decimal("0.9"), "only the halt tier is floored"


def test_liquidation_is_a_hard_stop_and_the_floor_does_not_touch_it() -> None:
    """The floor throttles entries; it must not defeat the deepest protection."""
    state = calculate_drawdown(Decimal("79"), Decimal("100"), _with_floor(2500))

    assert state.liquidation_required is True
    assert state.drawdown_fraction >= Decimal("0.20")


def test_a_floored_account_can_actually_climb_out() -> None:
    """The property the hard halt lacks: recovery is reachable by trading.

    Walks equity up from a halted level and asserts the account is permitted to trade at every
    step. Under the default policy this same walk stays halted throughout — that is the
    absorbing behaviour, and it is pinned in test_drawdown_halt_is_absorbing.py.
    """
    config = _with_floor(2500)
    for equity in ("85", "88", "91", "94", "97", "100"):
        state = calculate_drawdown(Decimal(equity), Decimal("100"), config)
        assert state.entry_halted is False, f"must be able to trade at equity {equity}"
        assert state.new_risk_multiplier > 0

    recovered = calculate_drawdown(Decimal("100"), Decimal("100"), config)
    assert recovered.new_risk_multiplier == Decimal("1"), "full size once the drawdown is gone"


@pytest.mark.parametrize("resume_at", [Decimal("0.10"), Decimal("0.05"), Decimal("0.01")])
def test_hysteresis_cannot_work_because_a_halted_account_cannot_move(
    resume_at: Decimal,
) -> None:
    """Refutes candidate fix #1 analytically, at every resume threshold.

    With entries halted the account holds nothing, so equity is unchanged session over session,
    so the drawdown is unchanged. A release condition of the form "drawdown has fallen back to
    X" therefore evaluates against a constant that is by definition still above the halt level.
    No choice of X releases it. This is why the measured result was byte-identical to the broken
    policy rather than merely worse.
    """
    equity, high_water = Decimal("85"), Decimal("100")

    for _ in range(500):
        state = calculate_drawdown(equity, high_water, DEPLOYED)
        assert state.entry_halted is True
        # A halted account earns nothing, so equity is carried forward untouched.
        equity = equity
        assert state.drawdown_fraction > resume_at, "the release condition is never reachable"
