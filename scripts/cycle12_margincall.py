"""Cycle 12: does the levered optimum survive a margin call? And is its edge real?

cycle12_growth.py found SPY's growth optimum at 3.25x turning $100 into $954, and vol-targeted
SPY at 4.25x turning it into $2,475 — the first thing in this project to beat SPY on wealth.

Both numbers assume something false: that you can hold a position through a 79% drawdown. You
cannot. Reg T maintenance margin is 25%, and at 3.25x the account starts at 30.8% equity. A
drop of roughly 10% in assets triggers forced liquidation, which crystallises the loss at the
worst possible moment and removes the recovery the backtest quietly collects.

Two attacks:

1. Margin mechanics. Re-run every leverage with real maintenance rules and forced deleveraging.
2. Significance. The vol-targeted advantage rests on a Sharpe difference of 0.17 that cycle 11
   already refuted (z=1.01, p=0.31). A stationary bootstrap asks whether the terminal-wealth
   gap is distinguishable from luck once that is acknowledged.

Usage:
    uv run python scripts/cycle12_margincall.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_costs import SPY_SPREAD_BPS, vol_targeted_with_costs
from cycle11_mechanisms import _returns, load, stats
from cycle12_growth import DAILY_MARGIN, terminal, trend_only

MAINTENANCE = 0.25  # Reg T maintenance requirement
INITIAL_REQUIREMENT = 0.50  # Reg T initial: caps real leverage at 2x for equities
BOOTSTRAP_DRAWS = 2000
BLOCK = 21  # stationary bootstrap block, ~1 month, preserves volatility clustering


def run_levered(
    daily: list[float],
    leverage: float,
    *,
    enforce_calls: bool,
    rebalance_every: int = 21,
) -> tuple[float, int, float]:
    """Constant-leverage path. `enforce_calls` is the ONLY thing that varies.

    Two earlier versions of this comparison were wrong in ways worth recording. The first held
    debt fixed, so leverage decayed as the account grew and high leverage looked survivable.
    The second fixed that but compared a monthly-rebalanced margin path against a
    daily-rebalanced baseline, so it measured rebalancing frequency and appeared to show that
    margin calls CREATE wealth. Both had to be isolated to one variable to mean anything.

    Returns (terminal equity, margin calls, max drawdown on equity).
    """
    equity = 100.0
    assets = equity * leverage
    debt = assets - equity
    peak = equity
    mdd = 0.0
    calls = 0
    for day, r in enumerate(daily):
        assets *= 1 + r
        debt *= 1 + DAILY_MARGIN
        equity = assets - debt
        if equity <= 0:
            return 0.0, calls, 1.0
        if enforce_calls and equity / assets < MAINTENANCE:
            # Forced sale to restore the requirement — selling into weakness by definition.
            calls += 1
            assets = equity / MAINTENANCE
            debt = assets - equity
        elif rebalance_every and day % rebalance_every == 0:
            assets = equity * leverage
            debt = assets - equity
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)
    return equity, calls, mdd


def stationary_bootstrap(series: list[float], rng: random.Random) -> list[float]:
    """Resample in blocks so volatility clustering survives; iid resampling would destroy it."""
    n = len(series)
    out: list[float] = []
    while len(out) < n:
        start = rng.randrange(n)
        for offset in range(BLOCK):
            out.append(series[(start + offset) % n])
            if len(out) >= n:
                break
    return out


def main() -> int:
    dates, data = load()
    closes = data["close"]
    spy = _returns(dates, closes, "SPY")[max(20, 200):]
    trend = trend_only(dates, closes, "SPY")
    vt, _, _ = vol_targeted_with_costs(
        dates, closes, "SPY", target_vol=0.15, cap=2.0, cost_bps=SPY_SPREAD_BPS
    )

    print("=" * 100)
    print("ATTACK 1 — the same leverage sweep, with real margin mechanics")
    print("=" * 100)
    print("  constant leverage, monthly rebalance in BOTH columns;")
    print("  the only difference is whether maintenance calls are enforced")
    print(f"  {'leverage':>9} {'naive $100':>12} {'with calls':>12} {'destroyed':>10} "
          f"{'calls':>7} {'maxDD':>8}")
    for step in range(1, 21):
        leverage = step * 0.25
        naive, _, naive_mdd = run_levered(spy, leverage, enforce_calls=False)
        real, calls, mdd = run_levered(spy, leverage, enforce_calls=True)
        destroyed = (naive - real) / naive * 100 if naive > 0 else 0.0
        flag = "  <- Reg T initial limit" if abs(leverage - 2.0) < 1e-9 else ""
        print(f"  {leverage:8.2f}x {naive:11.2f} {real:11.2f} {destroyed:9.1f}% "
              f"{calls:7} {mdd * 100:7.1f}%{flag}")

    print("\n" + "=" * 100)
    print("ATTACK 1b — best achievable under each real-world constraint")
    print("=" * 100)
    for label, series in (("SPY buy-and-hold", spy), ("SPY 200d trend", trend),
                          ("SPY vol-targeted", vt)):
        grid = [step * 0.25 for step in range(1, 21)]
        best_naive = max((run_levered(series, lv, enforce_calls=False)[0], lv) for lv in grid)
        best_real = max((run_levered(series, lv, enforce_calls=True)[0], lv) for lv in grid)
        regt = run_levered(series, 2.0, enforce_calls=True)
        under20 = max(
            (run_levered(series, lv, enforce_calls=True)[0], lv)
            for lv in grid
            if run_levered(series, lv, enforce_calls=True)[2] <= 0.20
        )
        print(f"\n  {label}")
        print(f"    ignoring margin calls   ${best_naive[0]:9.2f} at {best_naive[1]:.2f}x")
        print(f"    with margin calls       ${best_real[0]:9.2f} at {best_real[1]:.2f}x")
        print(f"    capped at Reg T 2x      ${regt[0]:9.2f}  ({regt[1]} calls, "
              f"{regt[2] * 100:.1f}% maxDD)")
        print(f"    under a 20% drawdown    ${under20[0]:9.2f} at {under20[1]:.2f}x")

    print("\n" + "=" * 100)
    print("ATTACK 2 — is the vol-targeted wealth advantage distinguishable from luck?")
    print("=" * 100)
    rng = random.Random(20260818)
    n = min(len(spy), len(vt))
    observed = terminal(vt[:n]) - terminal(spy[:n])
    print(f"  observed unlevered wealth gap: ${observed:+.2f}")
    print(f"  bootstrapping {BOOTSTRAP_DRAWS} paired resamples, {BLOCK}-day blocks...")
    gaps: list[float] = []
    for _ in range(BOOTSTRAP_DRAWS):
        idx = []
        while len(idx) < n:
            start = rng.randrange(n)
            for offset in range(BLOCK):
                idx.append((start + offset) % n)
                if len(idx) >= n:
                    break
        gaps.append(terminal([vt[i] for i in idx]) - terminal([spy[i] for i in idx]))
    gaps.sort()
    lo, hi = gaps[int(0.025 * len(gaps))], gaps[int(0.975 * len(gaps))]
    share_negative = sum(1 for g in gaps if g <= 0) / len(gaps)
    print(f"  95% CI on the gap: [${lo:.2f}, ${hi:.2f}]")
    print(f"  fraction of resamples where vol targeting LOSES: {share_negative:.1%}")
    spans_zero = lo <= 0 <= hi
    print(f"  CI {'SPANS ZERO — not distinguishable from luck' if spans_zero else 'excludes zero'}")

    print("\n" + "=" * 100)
    print("ATTACK 2b — the same question at the leverage that produced the headline")
    print("=" * 100)
    lev_gap = (run_levered(vt, 4.25, enforce_calls=True)[0]
               - run_levered(spy, 3.25, enforce_calls=True)[0])
    print("  naive claim (no margin calls):  vol-targeted 4.25x $2475.60 v SPY 3.25x $954.45")
    print(f"  with margin calls:              gap becomes ${lev_gap:+.2f}")
    _, sharpe_vt, _ = stats(vt)
    _, sharpe_spy, _ = stats(spy)
    print(f"  and it rests on a Sharpe difference of {sharpe_vt - sharpe_spy:.2f}, which cycle 11")
    print("  already refuted at z=1.01, p=0.31. Leverage amplifies an edge that is not there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
