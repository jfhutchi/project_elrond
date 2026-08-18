"""Cycle 13: does PER-POSITION vol targeting help the actual 10-name rotation?

An honesty gap left by cycle 12 and worth closing before anything ships. Cycle 12 measured
PORTFOLIO-level vol targeting on a single asset: one exposure knob on SPY. What has now been
built into the sizing path is PER-POSITION targeting across a 10-name roster, which is a
different mechanism — closer to inverse-volatility weighting than to exposure scaling.

Cycle 12's +44%-under-the-drawdown-cap result does NOT transfer to it. This measures the thing
actually built, on the universe actually traded.

Also tests the pre-registered cycle 11 survivor S1 in the same run: tranching the rebalance
date, which is the largest free effect found so far.

Usage:
    uv run python scripts/cycle13_position_voltarget.py
"""

from __future__ import annotations

import math
import random
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_mechanisms import TRADING_DAYS, load, stats

TARGET_VOL = 0.15
LOOKBACK = 20
MAX_WEIGHT = 0.10  # the deployed 10% per-position cap
BOOTSTRAP = 2000
BLOCK = 21


def realised_vol(closes: dict[str, float], series: list[str], index: int) -> float | None:
    if index < LOOKBACK + 1:
        return None
    window = series[index - LOOKBACK:index + 1]
    rets = []
    for a, b in zip(window, window[1:], strict=False):
        if closes[a] <= 0:
            return None
        rets.append(closes[b] / closes[a] - 1)
    return statistics.pstdev(rets) * math.sqrt(TRADING_DAYS) if rets else None


def rotation(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    universe: list[str],
    *,
    offset: int = 0,
    vol_target: bool,
    top_n: int = 10,
) -> list[float]:
    """Monthly momentum rotation. `vol_target` switches per-position sizing on.

    Weights are capped at MAX_WEIGHT either way, so the only difference is whether a turbulent
    name gets less than the cap. Unallocated weight stays in cash earning nothing, which is
    what the live system does.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in dates:
        by_month[day[:7]].append(day)
    rebalance = {days[offset] for days in by_month.values() if len(days) > offset}

    weights: dict[str, float] = {}
    daily: list[float] = []
    index_of = {d: i for i, d in enumerate(dates)}
    for index in range(1, len(dates)):
        day, prev = dates[index], dates[index - 1]
        earned = 0.0
        for symbol, weight in weights.items():
            if day in closes[symbol] and prev in closes[symbol]:
                earned += weight * (closes[symbol][day] / closes[symbol][prev] - 1)
        daily.append(earned)

        if day in rebalance and index >= TRADING_DAYS:
            start, end = dates[index - TRADING_DAYS], dates[index - 21]
            ranked = sorted(
                (
                    (closes[s][end] / closes[s][start] - 1, s)
                    for s in universe
                    if start in closes[s] and end in closes[s]
                ),
                reverse=True,
            )
            chosen = [s for _, s in ranked[:top_n]]
            weights = {}
            for symbol in chosen:
                weight = MAX_WEIGHT
                if vol_target:
                    series = [d for d in dates if d in closes[symbol] and index_of[d] <= index]
                    vol = realised_vol(closes[symbol], series, len(series) - 1)
                    if vol is None or vol <= 0:
                        continue  # fails closed, exactly as the sizing path does
                    weight = min(MAX_WEIGHT, TARGET_VOL / vol)
                weights[symbol] = weight
    return daily


def bootstrap_gap(a: list[float], b: list[float], rng: random.Random) -> tuple[float, float, float]:
    """95% CI on terminal-wealth difference, block-resampled to keep vol clustering."""
    n = min(len(a), len(b))
    gaps = []
    for _ in range(BOOTSTRAP):
        idx: list[int] = []
        while len(idx) < n:
            start = rng.randrange(n)
            for off in range(BLOCK):
                idx.append((start + off) % n)
                if len(idx) >= n:
                    break
        wa = wb = 100.0
        for i in idx:
            wa *= 1 + a[i]
            wb *= 1 + b[i]
        gaps.append(wa - wb)
    gaps.sort()
    return gaps[int(0.025 * BOOTSTRAP)], gaps[int(0.975 * BOOTSTRAP)], sum(
        1 for g in gaps if g <= 0
    ) / len(gaps)


def terminal(daily: list[float]) -> float:
    equity = 100.0
    for r in daily:
        equity *= 1 + r
    return equity


def main() -> int:
    dates, data = load()
    closes = data["close"]
    universe = sorted(closes)

    print("=" * 92)
    print("H13.1 — per-position vol targeting on the ACTUAL 10-name rotation")
    print("=" * 92)
    plain = rotation(dates, closes, universe, vol_target=False)
    vtgt = rotation(dates, closes, universe, vol_target=True)
    for label, series in (("10% flat weights (deployed)", plain),
                          ("per-position vol targeted", vtgt)):
        cagr, sharpe, mdd = stats(series)
        print(f"  {label:30} CAGR {cagr * 100:6.2f}%  Sharpe {sharpe:5.2f}  "
              f"maxDD {mdd * 100:5.1f}%  $100 -> {terminal(series):7.2f}")

    rng = random.Random(20260818)
    lo, hi, lose = bootstrap_gap(vtgt, plain, rng)
    print(f"\n  bootstrap 95% CI on the wealth gap: [${lo:.2f}, ${hi:.2f}]")
    print(f"  vol targeting loses in {lose:.1%} of resamples")
    spans = lo <= 0 <= hi
    print(f"  {'SPANS ZERO — not established' if spans else 'excludes zero'}")

    print("\n" + "=" * 92)
    print("H13.2 — does it buy exposure back under the 20% drawdown cap? (cycle 12's claim)")
    print("=" * 92)
    print(f"  {'variant':30} {'maxDD':>8} {'max scale under 20%':>21} {'$100 there':>12}")
    for label, series in (("10% flat weights (deployed)", plain),
                          ("per-position vol targeted", vtgt)):
        _, _, mdd = stats(series)
        best = None
        for step in range(1, 41):
            scale = step * 0.125
            scaled = [scale * r for r in series]
            if stats(scaled)[2] <= 0.20:
                best = (scale, terminal(scaled))
        print(f"  {label:30} {mdd * 100:7.1f}% {best[0]:20.3f}x {best[1]:11.2f}")

    print("\n" + "=" * 92)
    print("H13.3 — S1 tranching, on the same rotation")
    print("=" * 92)
    tranches = [rotation(dates, closes, universe, offset=o, vol_target=False) for o in range(21)]
    width = min(len(t) for t in tranches)
    blended = [statistics.fmean([t[i] for t in tranches]) for i in range(width)]
    finals = [terminal(t) for t in tranches]
    b_cagr, b_sharpe, b_mdd = stats(blended)
    print(f"  single-offset terminal wealth spans ${min(finals):.2f} to ${max(finals):.2f}")
    print(f"  that is a ${max(finals) - min(finals):.2f} spread on $100 decided by the calendar")
    print(f"  21-tranche blend: CAGR {b_cagr * 100:.2f}%  Sharpe {b_sharpe:.2f}  "
          f"maxDD {b_mdd * 100:.1f}%  $100 -> {terminal(blended):.2f}")
    print(f"  blend vs median single offset: ${terminal(blended) - statistics.median(finals):+.2f}")
    print("  Tranching does not raise the mean. It removes the dispersion — which is the")
    print("  point, because the deployed system currently holds one arbitrary draw from it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
