"""Cycle 15: is the momentum signal worth anything, or is it an expensive beta wrapper?

Fourteen cycles compared the strategy against SPY and against variants of itself. None asked
the more basic question: does SELECTING ten names by momentum beat holding the same universe
without selecting at all? If it does not, the rotation pays spread and turnover for a decision
that adds nothing, and every parameter question asked so far was downstream of a dead premise.

Three decompositions, each capable of refuting the signal:

1. Alpha against SPY. Regress rotation returns on the market. A positive, significant intercept
   means selection adds value the market did not already provide.
2. The selection test proper. Momentum-selected 10 versus equal-weight all 23, same universe,
   same rebalance dates, same costs. This isolates the RANKING and nothing else.
3. The inverse. If ranking carries information, the bottom 10 should underperform the top 10.
   If top and bottom are indistinguishable, the ranking is noise.

Usage:
    uv run python scripts/cycle15_does_selection_work.py
"""

from __future__ import annotations

import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_mechanisms import TRADING_DAYS, _returns, load, stats

CUMULATIVE_TRIALS = 62
LUCK_BAR = math.sqrt(2 * math.log(CUMULATIVE_TRIALS))


def selected_rotation(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    universe: list[str],
    *,
    top_n: int | None,
    bottom: bool = False,
    weight: float = 0.10,
) -> list[float]:
    """Monthly rotation. `top_n=None` holds the whole universe, selecting nothing.

    Every arm shares rebalance dates, weighting and universe, so the only thing that varies is
    which names the ranking picks. That is what makes the comparison a test of selection.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in dates:
        by_month[day[:7]].append(day)
    rebalance = {days[-1] for days in by_month.values()}

    holdings: list[str] = []
    daily: list[float] = []
    for index in range(1, len(dates)):
        day, prev = dates[index], dates[index - 1]
        earned = [
            closes[s][day] / closes[s][prev] - 1
            for s in holdings
            if day in closes[s] and prev in closes[s]
        ]
        daily.append(statistics.fmean(earned) * (weight * len(holdings)) if earned else 0.0)
        if day in rebalance and index >= TRADING_DAYS:
            start, end = dates[index - TRADING_DAYS], dates[index - 21]
            ranked = sorted(
                (
                    (closes[s][end] / closes[s][start] - 1, s)
                    for s in universe
                    if start in closes[s] and end in closes[s]
                ),
                reverse=not bottom,
            )
            holdings = [s for _, s in ranked[:top_n]] if top_n else [s for _, s in ranked]
    return daily


def regress(y: list[float], x: list[float]) -> tuple[float, float, float]:
    """OLS of y on x. Returns (annualised alpha, beta, t-statistic of alpha)."""
    n = min(len(y), len(x))
    y, x = y[:n], x[:n]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    var = sum((v - mx) ** 2 for v in x)
    if var == 0:
        return 0.0, 0.0, 0.0
    beta = sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / var
    alpha = my - beta * mx
    residuals = [b - (alpha + beta * a) for a, b in zip(x, y, strict=True)]
    se_resid = statistics.pstdev(residuals)
    se_alpha = se_resid * math.sqrt(1 / n + mx**2 / var) if n else 0.0
    t = alpha / se_alpha if se_alpha > 0 else 0.0
    return alpha * TRADING_DAYS, beta, t


def paired_t(a: list[float], b: list[float]) -> tuple[float, float]:
    n = min(len(a), len(b))
    diffs = [a[i] - b[i] for i in range(n)]
    mean = statistics.fmean(diffs)
    se = statistics.stdev(diffs) / math.sqrt(n) if n > 1 else 0.0
    return mean * TRADING_DAYS, (mean / se if se > 0 else 0.0)


def main() -> int:
    dates, data = load()
    closes = data["close"]
    universe = sorted(closes)
    spy = _returns(dates, closes, "SPY")

    # Equal capital deployed in every arm: 10 names at 10% each, 23 at 10/23% each.
    top10 = selected_rotation(dates, closes, universe, top_n=10, weight=0.10)
    bottom10 = selected_rotation(dates, closes, universe, top_n=10, bottom=True, weight=0.10)
    all23 = selected_rotation(dates, closes, universe, top_n=None, weight=0.10 * 10 / 23)

    print("=" * 92)
    print("TEST 1 — does the rotation produce alpha, or just beta?")
    print("=" * 92)
    print(f"  {'arm':28} {'ann. alpha':>12} {'beta':>7} {'t(alpha)':>10}  vs luck bar "
          f"{LUCK_BAR:.2f}")
    for label, series in (("momentum top 10", top10), ("all 23, no selection", all23),
                          ("momentum bottom 10", bottom10)):
        alpha, beta, t = regress(series, spy)
        verdict = "CLEARS" if abs(t) > LUCK_BAR else ""
        print(f"  {label:28} {alpha * 100:11.2f}% {beta:7.2f} {t:10.2f}  {verdict}")

    print("\n" + "=" * 92)
    print("TEST 2 — the selection test: does picking beat not picking?")
    print("=" * 92)
    for label, series in (("momentum top 10", top10), ("all 23, no selection", all23)):
        cagr, sharpe, mdd = stats(series)
        print(f"  {label:28} CAGR {cagr * 100:6.2f}%  Sharpe {sharpe:5.2f}  "
              f"maxDD {mdd * 100:5.1f}%")
    diff, t = paired_t(top10, all23)
    print(f"\n  selection premium {diff * 100:+.2f}%/yr, t = {t:.2f}, luck bar {LUCK_BAR:.2f}")
    if abs(t) > LUCK_BAR:
        print("  ESTABLISHED")
    else:
        print("  NOT ESTABLISHED — selection adds nothing measurable")

    print("\n" + "=" * 92)
    print("TEST 3 — the inverse: should the bottom of the ranking underperform the top?")
    print("=" * 92)
    for label, series in (("momentum top 10", top10), ("momentum bottom 10", bottom10)):
        cagr, sharpe, _ = stats(series)
        print(f"  {label:28} CAGR {cagr * 100:6.2f}%  Sharpe {sharpe:5.2f}")
    spread, t_spread = paired_t(top10, bottom10)
    print(f"\n  top-minus-bottom {spread * 100:+.2f}%/yr, t = {t_spread:.2f}")
    print("  If the ranking carried information the top should beat the bottom decisively.")
    if abs(t_spread) > LUCK_BAR:
        print("  It does.")
    else:
        print("  It does not — the ranking is not separating winners from losers.")

    print("\n" + "=" * 92)
    print("WHAT THIS SAYS ABOUT THE DEPLOYED STRATEGY")
    print("=" * 92)
    print("  The deployed configuration sizes 10 momentum-selected names at 10% each. If")
    print("  selection is not measurable, that machinery — ranking, monthly rotation, roster")
    print("  exits, and the turnover they generate — is paying costs to reproduce something")
    print("  a static equal-weight basket would have delivered for free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
