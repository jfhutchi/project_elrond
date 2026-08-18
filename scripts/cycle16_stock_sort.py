"""Cycle 16: does the momentum ranking sort INDIVIDUAL STOCKS, even though it fails on ETFs?

Cycle 15 found the ranking carries no information across 23 sector and asset-class ETFs: the
top decile does not beat the bottom (t=0.77). There is an obvious confound. Those ETFs are
highly correlated slices of one market, so there may simply be nothing to sort. The momentum
literature (Jegadeesh & Titman) documents the effect on individual stocks, where dispersion is
an order of magnitude wider.

Cycle 10 tested a long-only strategy on 511 survivorship-free names and found it lost to SPY on
Sharpe. It never asked whether the RANKING sorts, which is the prior question: a signal can
carry real information and still lose long-only after costs and beta drag.

So this is not a re-run. It is the cycle 15 test applied where the literature says the effect
should live, on a survivorship-free universe including delisted names.

Data is persisted this time. Cycle 10 fetched live and discarded it, which is why this needs a
fresh download at all.

Usage:
    uv run python scripts/cycle16_stock_sort.py [UNIVERSE_SIZE]
"""

from __future__ import annotations

import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle10_breadth import _bars, _universe

TRADING_DAYS = 252
LOOKBACK = 252
SKIP = 21
DECILE = 10
MIN_PRICE = 5.0
CUMULATIVE_TRIALS = 65
LUCK_BAR = math.sqrt(2 * math.log(CUMULATIVE_TRIALS))
CACHE = Path(__file__).parent.parent / "research" / "stocks.db"


def load_or_fetch(budget: int) -> dict[str, dict[str, float]]:
    """Persist the download so a later cycle never has to re-pay for it."""
    CACHE.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(CACHE)
    con.execute("create table if not exists closes (symbol text, day text, close real,"
                " primary key (symbol, day))")
    existing = con.execute("select count(distinct symbol) from closes").fetchone()[0]
    if existing >= budget * 0.5:
        print(f"using cached universe of {existing} symbols from {CACHE.name}")
        closes: dict[str, dict[str, float]] = defaultdict(dict)
        for symbol, day, close in con.execute("select symbol, day, close from closes"):
            closes[symbol][day] = close
        con.close()
        return closes

    print(f"fetching {budget} symbols (half survivors, half delisted)...")
    symbols = _universe(budget)
    fetched = _bars(symbols)
    con.executemany(
        "insert or replace into closes values (?, ?, ?)",
        [(s, d, c) for s, days in fetched.items() for d, c in days.items()],
    )
    con.commit()
    con.close()
    print(f"cached {len(fetched)} symbols to {CACHE.name}")
    return fetched


def decile_returns(
    closes: dict[str, dict[str, float]], dates: list[str]
) -> tuple[list[float], list[float], list[float]]:
    """Monthly-rebalanced top-decile, bottom-decile and equal-weight-all return series.

    Delisted names simply stop having bars, which is how survivorship bias is avoided: a
    position in a name that stops trading contributes nothing further rather than being
    quietly dropped from history.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in dates:
        by_month[day[:7]].append(day)
    rebalance = {days[-1] for days in by_month.values()}

    top: list[str] = []
    bottom: list[str] = []
    everything: list[str] = []
    out_top: list[float] = []
    out_bottom: list[float] = []
    out_all: list[float] = []

    def earned(holdings: list[str], day: str, prev: str) -> float:
        moves = [
            closes[s][day] / closes[s][prev] - 1
            for s in holdings
            if day in closes[s] and prev in closes[s] and closes[s][prev] > 0
        ]
        return statistics.fmean(moves) if moves else 0.0

    for index in range(1, len(dates)):
        day, prev = dates[index], dates[index - 1]
        out_top.append(earned(top, day, prev))
        out_bottom.append(earned(bottom, day, prev))
        out_all.append(earned(everything, day, prev))

        if day in rebalance and index >= LOOKBACK:
            start, end = dates[index - LOOKBACK], dates[index - SKIP]
            scored = [
                (closes[s][end] / closes[s][start] - 1, s)
                for s in closes
                if start in closes[s] and end in closes[s]
                and closes[s][start] > 0 and closes[s][end] >= MIN_PRICE
            ]
            if len(scored) < DECILE * 2:
                continue
            scored.sort(reverse=True)
            size = max(1, len(scored) // DECILE)
            top = [s for _, s in scored[:size]]
            bottom = [s for _, s in scored[-size:]]
            everything = [s for _, s in scored]
    return out_top, out_bottom, out_all


def annualised(series: list[float]) -> tuple[float, float]:
    if len(series) < 2:
        return 0.0, 0.0
    equity = 1.0
    for r in series:
        equity *= 1 + r
    years = len(series) / TRADING_DAYS
    cagr = equity ** (1 / years) - 1 if years > 0 and equity > 0 else -1.0
    sd = statistics.pstdev(series)
    return cagr, (statistics.fmean(series) / sd * math.sqrt(TRADING_DAYS)) if sd else 0.0


def paired_t(a: list[float], b: list[float]) -> tuple[float, float]:
    n = min(len(a), len(b))
    diffs = [a[i] - b[i] for i in range(n)]
    mean = statistics.fmean(diffs)
    se = statistics.stdev(diffs) / math.sqrt(n) if n > 1 else 0.0
    return mean * TRADING_DAYS, (mean / se if se > 0 else 0.0)


def main() -> int:
    budget = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    closes = load_or_fetch(budget)
    dates = sorted({d for days in closes.values() for d in days})
    delisted = sum(1 for s, days in closes.items() if max(days) < dates[-30])
    print(f"{len(closes)} symbols, {len(dates)} sessions, {dates[0]} to {dates[-1]}")
    print(f"{delisted} stopped trading before the end — survivorship-free\n")

    top, bottom, everything = decile_returns(closes, dates)

    print("=" * 88)
    print("THE SORT TEST — does the ranking separate winners from losers?")
    print("=" * 88)
    print(f"  {'arm':26} {'CAGR':>9} {'Sharpe':>8}")
    for label, series in (("top decile", top), ("all names, equal weight", everything),
                          ("bottom decile", bottom)):
        cagr, sharpe = annualised(series)
        print(f"  {label:26} {cagr * 100:8.2f}% {sharpe:8.2f}")

    spread, t_spread = paired_t(top, bottom)
    premium, t_premium = paired_t(top, everything)
    print(f"\n  top minus bottom   {spread * 100:+8.2f}%/yr   t = {t_spread:5.2f}")
    print(f"  top minus average  {premium * 100:+8.2f}%/yr   t = {t_premium:5.2f}")
    print(f"  luck bar for {CUMULATIVE_TRIALS} cumulative trials: |t| > {LUCK_BAR:.2f}")

    sorts = abs(t_spread) > LUCK_BAR
    if sorts:
        print("  Note this is the RANKING, not a tradeable strategy. Cycle 10 already showed")
        print("  the long-only version loses to SPY on Sharpe, so any value would have to be")
        print("  harvested by construction, and every such attempt so far died on cost.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
