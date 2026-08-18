"""Cycle 12: locate the growth-optimal leverage, with borrowing charged.

Compounding is the only wealth mechanism this project has confirmed. Growth is not linear in
leverage: it rises, peaks, then falls, because variance drag scales as the square of exposure.
Cycle 6 tested fixed 2x and 3x, found them worse, and stopped. It never located the peak and
never charged margin interest.

That omission matters more than it sounds. Borrowed money costs roughly 6% at retail brokers,
which is a large fraction of the ~15% the market returns, so it moves the optimum a long way.

Usage:
    uv run python scripts/cycle12_growth.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_costs import SPY_SPREAD_BPS, vol_targeted_with_costs
from cycle11_mechanisms import (
    TRADING_DAYS,
    _above_trend,
    _returns,
    load,
    momentum_rotation,
    stats,
)

# Alpaca's published margin rate for accounts under $25k. The rate a $100 account actually
# pays, not the institutional rate that makes leverage studies look good.
MARGIN_RATE = 0.0575
DAILY_MARGIN = MARGIN_RATE / TRADING_DAYS


def levered(daily: list[float], leverage: float, *, charge_margin: bool = True) -> list[float]:
    """Apply constant leverage, charging interest on the borrowed portion only.

    Borrowing is max(0, L-1) of equity. At L <= 1 nothing is borrowed and nothing is charged.
    """
    borrowed = max(0.0, leverage - 1.0)
    cost = borrowed * DAILY_MARGIN if charge_margin else 0.0
    return [leverage * r - cost for r in daily]


def terminal(daily: list[float], start: float = 100.0) -> float:
    """Terminal wealth, floored at zero: a levered path can be wiped out."""
    equity = start
    for r in daily:
        equity *= 1 + r
        if equity <= 0:
            return 0.0
    return equity


def growth_curve(
    daily: list[float], *, charge_margin: bool = True
) -> list[tuple[float, float, float, float]]:
    """(leverage, terminal wealth, CAGR, maxDD) across a leverage sweep."""
    out = []
    for step in range(1, 41):
        leverage = step * 0.125
        series = levered(daily, leverage, charge_margin=charge_margin)
        cagr, _, mdd = stats(series)
        out.append((leverage, terminal(series), cagr, mdd))
    return out


def peak(curve: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return max(curve, key=lambda row: row[1])


def theoretical_optimum(daily: list[float], rate: float) -> float:
    """L* = (mu - r) / sigma^2 on annualised arithmetic moments."""
    mu = statistics.fmean(daily) * TRADING_DAYS
    sigma = statistics.pstdev(daily) * math.sqrt(TRADING_DAYS)
    return (mu - rate) / sigma**2 if sigma > 0 else 0.0


def trend_only(dates: list[str], closes: dict, symbol: str) -> list[float]:
    series = [d for d in dates if d in closes[symbol]]
    prices = [closes[symbol][d] for d in series]
    rets = _returns(dates, closes, symbol)
    return [
        (rets[i] if _above_trend(prices, i) else 0.0)
        for i in range(max(20, 200), len(rets))
    ]


def main() -> int:
    dates, data = load()
    closes = data["close"]
    universe = sorted(closes)

    strategies: dict[str, list[float]] = {}
    strategies["SPY buy-and-hold"] = _returns(dates, closes, "SPY")[max(20, 200):]
    strategies["SPY 200d trend"] = trend_only(dates, closes, "SPY")
    vt, _, _ = vol_targeted_with_costs(
        dates, closes, "SPY", target_vol=0.15, cap=2.0, cost_bps=SPY_SPREAD_BPS
    )
    strategies["SPY vol-targeted"] = vt
    strategies["momentum rotation"] = momentum_rotation(dates, closes, universe, 0)

    print("=" * 104)
    print(f"H12.1 / H12.2 — growth-optimal leverage, margin charged at {MARGIN_RATE:.2%}")
    print("=" * 104)
    print(f"  {'strategy':20} {'Sharpe':>7} {'L* theory':>10} {'L* free':>8} {'L* charged':>11} "
          f"{'$100 at L*':>12} {'CAGR':>8} {'maxDD':>7}")
    results = {}
    for name, daily in strategies.items():
        _, sharpe, _ = stats(daily)
        free = peak(growth_curve(daily, charge_margin=False))
        charged = peak(growth_curve(daily, charge_margin=True))
        theory = theoretical_optimum(daily, MARGIN_RATE)
        results[name] = (sharpe, charged)
        print(f"  {name:20} {sharpe:7.2f} {theory:10.2f} {free[0]:8.2f} {charged[0]:11.2f} "
              f"{charged[1]:11.2f} {charged[2] * 100:7.2f}% {charged[3] * 100:6.1f}%")

    print("\n  L* free vs L* charged is H12.2: how far interest moves the optimum.")

    print("\n" + "=" * 104)
    print("H12.3 — at each strategy's OWN optimal leverage, does any beat SPY on wealth?")
    print("=" * 104)
    spy_sharpe, spy_best = results["SPY buy-and-hold"]
    print(f"  SPY at its optimum {spy_best[0]:.2f}x turns $100 into ${spy_best[1]:.2f}\n")
    winners = 0
    for name, (sharpe, best) in results.items():
        if name == "SPY buy-and-hold":
            continue
        delta = best[1] - spy_best[1]
        if delta > 0:
            winners += 1
        print(f"  {name:20} L*={best[0]:.2f}x  ${best[1]:8.2f}  "
              f"{delta:+9.2f} vs SPY   Sharpe {sharpe:.2f} v {spy_sharpe:.2f}  "
              f"{'BEATS SPY' if delta > 0 else ''}")
    print(f"\n  {winners} of {len(results) - 1} strategies beat SPY at matched-optimal leverage")

    print("\n" + "=" * 104)
    print("H12.4 — does the 20% drawdown halt bind before the growth optimum?")
    print("=" * 104)
    print(f"  {'strategy':20} {'L*':>6} {'maxDD at L*':>12} {'max L under 20% DD':>20} "
          f"{'$100 there':>12}")
    for name, daily in strategies.items():
        curve = growth_curve(daily)
        best = peak(curve)
        under = [row for row in curve if row[3] <= 0.20]
        if under:
            capped = max(under, key=lambda row: row[1])
            print(f"  {name:20} {best[0]:6.2f} {best[3] * 100:11.1f}% "
                  f"{capped[0]:19.2f}x {capped[1]:11.2f}")
        else:
            print(f"  {name:20} {best[0]:6.2f} {best[3] * 100:11.1f}% "
                  f"{'none — even 0.125x breaches':>19} ")

    print("\n" + "=" * 104)
    print("THE SHAPE OF THE CURVE — SPY buy-and-hold, margin charged")
    print("=" * 104)
    print(f"  {'leverage':>9} {'$100 becomes':>14} {'CAGR':>9} {'maxDD':>8}")
    for leverage, wealth, cagr, mdd in growth_curve(strategies["SPY buy-and-hold"]):
        if abs(leverage * 4 - round(leverage * 4)) < 1e-9:  # print every 0.25x
            bar = "#" * int(min(40, wealth / 40))
            print(f"  {leverage:8.2f}x {wealth:13.2f} {cagr * 100:8.2f}% {mdd * 100:7.1f}%  {bar}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
