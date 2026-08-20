"""Cycle 14: what should the money actually be in, under the real risk rules?

Thirteen cycles established that nothing built here beats SPY. That was stated abstractly. This
puts a number on it for the configuration ACTUALLY DEPLOYED, and then asks the question that
matters: under the configured halts — entries stop at 15% drawdown, liquidation at 20% — what
allocation produces the most wealth?

That constraint changes the answer, which is why the abstract version was not actionable. SPY
unlevered draws down 33.8% and breaches the liquidation threshold, so "just hold SPY" is not
available either. The honest comparison is between what each option delivers *after* being
sized to fit the rules.

Usage:
    uv run python scripts/cycle14_what_to_hold.py
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import cycle13_position_voltarget as c13
from cycle11_costs import SPY_SPREAD_BPS, vol_targeted_with_costs
from cycle11_mechanisms import _returns, load, stats

ENTRY_HALT = 0.15
LIQUIDATION = 0.20


def terminal(daily: list[float], start: float = 100.0) -> float:
    equity = start
    for r in daily:
        equity *= 1 + r
    return equity


def largest_scale_within(series: list[float], cap: float) -> tuple[float, float, float]:
    """Biggest constant scaling whose max drawdown still respects `cap`."""
    best = (0.0, 0.0, 0.0)
    for step in range(1, 81):
        scale = step * 0.125
        scaled = [scale * r for r in series]
        _, _, mdd = stats(scaled)
        if mdd <= cap:
            best = (scale, terminal(scaled), mdd)
    return best


def bootstrap_gap(
    a: list[float], b: list[float], seed: int = 20260818
) -> tuple[float, float, float]:
    rng = random.Random(seed)
    n = min(len(a), len(b))
    gaps = []
    for _ in range(2000):
        idx: list[int] = []
        while len(idx) < n:
            start = rng.randrange(n)
            for off in range(21):
                idx.append((start + off) % n)
                if len(idx) >= n:
                    break
        gaps.append(terminal([a[i] for i in idx]) - terminal([b[i] for i in idx]))
    gaps.sort()
    return gaps[50], gaps[1949], sum(1 for g in gaps if g <= 0) / len(gaps)


def main() -> int:
    dates, data = load()
    closes = data["close"]
    universe = sorted(closes)

    spy = _returns(dates, closes, "SPY")[max(20, 200):]
    rotation = c13.rotation(dates, closes, universe, vol_target=False)
    c13.TARGET_VOL = 0.01
    rotation_vt = c13.rotation(dates, closes, universe, vol_target=True)
    spy_vt, _, _ = vol_targeted_with_costs(
        dates, closes, "SPY", target_vol=0.15, cap=2.0, cost_bps=SPY_SPREAD_BPS
    )

    options = [
        ("SPY buy-and-hold", spy),
        ("DEPLOYED: 10-name rotation", rotation),
        ("rotation + vol target", rotation_vt),
        ("SPY + vol target", spy_vt),
    ]

    print("=" * 94)
    print("UNCONSTRAINED — ignoring the halts entirely")
    print("=" * 94)
    print(f"  {'option':30} {'CAGR':>8} {'Sharpe':>7} {'maxDD':>7} {'$100 becomes':>14}")
    for label, series in options:
        cagr, sharpe, mdd = stats(series)
        breach = "  BREACHES 20% LIQUIDATION" if mdd > LIQUIDATION else ""
        print(f"  {label:30} {cagr * 100:7.2f}% {sharpe:7.2f} {mdd * 100:6.1f}% "
              f"{terminal(series):13.2f}{breach}")

    deployed = terminal(rotation)
    benchmark = terminal(spy)
    print(f"\n  The deployed rotation returns ${deployed:.2f} against SPY's ${benchmark:.2f}"
          f" — {(deployed / benchmark - 1) * 100:+.1f}%.")
    print("  But BOTH breach the liquidation threshold, so neither is actually available")
    print("  unlevered. That is why the unconstrained comparison is not the decision.")

    print("\n" + "=" * 94)
    print(f"CONSTRAINED — sized so max drawdown respects the {ENTRY_HALT:.0%} entry halt")
    print("=" * 94)
    print(f"  {'option':30} {'max scale':>10} {'maxDD there':>12} {'$100 becomes':>14}")
    results = []
    for label, series in options:
        scale, wealth, mdd = largest_scale_within(series, ENTRY_HALT)
        results.append((label, wealth, series, scale))
        print(f"  {label:30} {scale:9.3f}x {mdd * 100:11.1f}% {wealth:13.2f}")

    results.sort(key=lambda row: row[1], reverse=True)
    winner, best_wealth, best_series, best_scale = results[0]
    deployed_row = next(r for r in results if r[0].startswith("DEPLOYED"))
    print(f"\n  Best under the rule: {winner} at {best_scale:.3f}x -> ${best_wealth:.2f}")
    print(f"  Currently deployed:  ${deployed_row[1]:.2f}"
          f"  ({(best_wealth / deployed_row[1] - 1) * 100:+.1f}% available)")

    print("\n" + "=" * 94)
    print("IS THAT GAP REAL, OR NOISE?")
    print("=" * 94)
    scaled_best = [best_scale * r for r in best_series]
    scaled_deployed = [deployed_row[3] * r for r in deployed_row[2]]
    lo, hi, lose = bootstrap_gap(scaled_best, scaled_deployed)
    print(f"  bootstrap 95% CI on the gap: [${lo:.2f}, ${hi:.2f}]")
    print(f"  the alternative loses in {lose:.1%} of resamples")
    if lo <= 0 <= hi:
        print("  SPANS ZERO — the gap is not established, and switching is not justified")
        print("  by this evidence alone.")
    else:
        print("  EXCLUDES ZERO — the gap survives resampling.")

    print("\n" + "=" * 94)
    print("WHAT THIS MEANS FOR THE MONEY")
    print("=" * 94)
    print("  Every option here is a variation on holding the equity market. None of them")
    print("  produces a return that changes what $100 becomes in a human timeframe:")
    print()
    for label, wealth, _, _ in results:
        annual = (wealth / 100) ** (1 / 10.6) - 1
        # $100 -> $100,000 is a 1000x, so log(1000) / log(1 + annual) years.
        years = math.log(1000) / math.log(1 + annual) if annual > 0 else float("inf")
        print(f"    {label:30} {annual * 100:5.2f}%/yr -> $100 reaches $100,000 in "
              f"{years:.0f} years")
    print()
    print("  That is the honest frame. The choice between these options is worth a few")
    print("  percent a year. The choice of how much capital enters is worth multiples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
