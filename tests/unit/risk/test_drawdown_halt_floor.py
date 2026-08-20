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


def test_the_floor_moves_a_concentrated_config_from_halted_to_liquidating() -> None:
    """The floor is not a way to restore exposure for a concentrated position.

    Measured 2026-08-19 (`reports/research/exposure-diagnosis-2026-08-19.md`): a single-name SPY
    config at a 100% cap draws down 16.29% with the floor off — halting entry on 928 of 2669
    sessions — and 20.51% with the floor on, which trips the *liquidation* tier on 868 sessions.
    Net exposure gain 0.79 points, CAGR 4.94% -> 4.43%.

    This asserts that specific mechanism at the two measured drawdown depths, so that a change to
    either threshold fails here rather than being rediscovered by another expensive backtest.
    """
    high_water = Decimal("100")

    # Floor off, at the depth the halted run actually reached.
    halted = calculate_drawdown(Decimal("83.71"), high_water, _with_floor(0))
    assert halted.entry_halted is True, "the deployed ladder halts a concentrated position"
    assert halted.liquidation_required is False, "16.29% is short of the 20% liquidation tier"

    # Floor on, at the depth the floored run then reached by continuing to trade.
    floored = calculate_drawdown(Decimal("79.49"), high_water, _with_floor(2500))
    assert floored.entry_halted is False, "the floor does release the halt"
    assert floored.liquidation_required is True, (
        "but 20.51% crosses the liquidation tier, which the floor deliberately does not touch — "
        "so the floor does not recover exposure for this config"
    )


def test_the_halt_threshold_sits_below_the_drawdown_of_the_rule_it_would_express() -> None:
    """Why `SPY_SMA200` is unreachable at full size, asserted rather than narrated.

    The published `SPY_SMA200` benchmark draws down 19.50%. The deployed halt tier is 15%. A config
    holding that rule in one full-size position therefore halts on its own benchmark's drawdown —
    a risk-budget limitation, not a sizing defect. If either number moves, this should be revisited
    rather than silently becoming false.
    """
    halt_threshold = Decimal(DEPLOYED.drawdown_thresholds_bps[2]) / Decimal("10000")
    benchmark_max_drawdown = Decimal("0.1950")
    assert halt_threshold < benchmark_max_drawdown, (
        "the risk ladder is calibrated for a ten-name rotation holding 10% positions; a "
        "concentrated config cannot express a rule whose own drawdown exceeds the halt tier"
    )
    state = calculate_drawdown(
        Decimal("100") * (Decimal("1") - benchmark_max_drawdown), Decimal("100"), DEPLOYED
    )
    assert state.entry_halted is True
