"""Cycle 11: attack H11.7, the overnight/intraday split.

Batch 2 found overnight returns beating intraday on 6 of 8 assets, with IWM (t=3.47),
GLD (3.82) and QQQ (3.10) clearing the 54-trial luck bar of 2.82. Striking splits: IWM earned
15.17%/yr overnight against -1.66% intraday.

That is exactly the shape of a result that dies on costs, because capturing it means two trades
every session. Attacks:

1. Cost. 504 trades/yr. Charged at measured spreads, and at a punitive multiple of them.
2. Era stability. An effect that has decayed is not tradeable now, however real it once was.
3. Breadth. Does it hold across assets or only where it was first spotted?
4. The honest benchmark. Overnight-only must beat BUY-AND-HOLD net, not merely beat intraday.

Usage:
    uv run python scripts/cycle11_overnight.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_mechanisms import TRADING_DAYS, load, stats

CUMULATIVE_TRIALS = 58
LUCK_BAR = math.sqrt(2 * math.log(CUMULATIVE_TRIALS))

# Measured NBBO, 2026-08-17 14:30-15:00Z. Assets not sampled use the universe median.
SPREAD_BPS = {
    "SPY": 0.26, "QQQ": 0.41, "IWM": 0.66, "XLK": 1.05, "XLE": 1.60,
    "DBC": 3.31, "EFA": 0.92, "TLT": 1.22, "VNQ": 1.02, "MDY": 1.96,
}
DEFAULT_SPREAD_BPS = 1.10


def overnight_series(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    opens: dict[str, dict[str, float]],
    symbol: str,
    *,
    cost_multiple: float = 1.0,
) -> tuple[list[float], list[float]]:
    """Overnight-only net of cost, and buy-and-hold over the identical window.

    Cost model: a marketable order pays roughly half the quoted spread against the midpoint,
    and the strategy trades twice a session (buy at close, sell at open). So one full spread
    per session. MOC/MOO auction orders would pay less, making this the conservative case.
    """
    spread = SPREAD_BPS.get(symbol, DEFAULT_SPREAD_BPS) * cost_multiple
    per_session_cost = spread / 10_000
    series = [d for d in dates if d in closes[symbol] and d in opens[symbol]]
    overnight: list[float] = []
    hold: list[float] = []
    for i in range(1, len(series)):
        gap = opens[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
        overnight.append(gap - per_session_cost)
        hold.append(closes[symbol][series[i]] / closes[symbol][series[i - 1]] - 1)
    return overnight, hold


def era_split(dates: list[str]) -> list[tuple[str, str, str]]:
    return [
        ("2016-01-01", "2018-12-31", "2016-2018"),
        ("2019-01-01", "2021-12-31", "2019-2021"),
        ("2022-01-01", "2024-12-31", "2022-2024"),
        ("2025-01-01", "2026-12-31", "2025-2026"),
    ]


def main() -> int:
    dates, data = load()
    closes, opens = data["close"], data["open"]
    assets = ["SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLK", "XLF", "XLE",
              "XLV", "XLY", "XLP", "XLU", "XLB", "XLI", "TLT", "GLD", "VNQ"]

    print("=" * 98)
    print("ATTACK 1 — net of measured spread, does overnight-only beat BUY-AND-HOLD?")
    print("=" * 98)
    print(f"  {'asset':6} {'spread':>7} {'ON CAGR':>9} {'BH CAGR':>9} {'ON Sharpe':>10} "
          f"{'BH Sharpe':>10} {'ON maxDD':>9} {'BH maxDD':>9}  verdict")
    beats_cagr = 0
    beats_sharpe = 0
    for symbol in assets:
        on, bh = overnight_series(dates, closes, opens, symbol)
        o_cagr, o_sharpe, o_mdd = stats(on)
        b_cagr, b_sharpe, b_mdd = stats(bh)
        if o_cagr > b_cagr:
            beats_cagr += 1
        if o_sharpe > b_sharpe:
            beats_sharpe += 1
        verdict = "WIN" if o_sharpe > b_sharpe else "loss"
        print(f"  {symbol:6} {SPREAD_BPS.get(symbol, DEFAULT_SPREAD_BPS):6.2f}b "
              f"{o_cagr * 100:8.2f}% {b_cagr * 100:8.2f}% {o_sharpe:10.2f} {b_sharpe:10.2f} "
              f"{o_mdd * 100:8.1f}% {b_mdd * 100:8.1f}%  {verdict}")
    n = len(assets)
    print(f"\n  beats buy-and-hold on CAGR:   {beats_cagr} of {n}")
    print(f"  beats buy-and-hold on Sharpe: {beats_sharpe} of {n}")
    tail = sum(math.comb(n, i) for i in range(beats_sharpe, n + 1)) / 2**n
    print(f"  P(>= {beats_sharpe} of {n} by chance) = {tail:.4f}")

    print("\n" + "=" * 98)
    print("ATTACK 2 — cost ladder. How wrong can the spread estimate be before it dies?")
    print("=" * 98)
    print(f"  {'multiple':>9} {'SPY':>8} {'QQQ':>8} {'IWM':>8} {'GLD':>8} {'XLK':>8}"
          f"   (Sharpe, overnight-only)")
    for multiple in (1, 2, 5, 10, 20, 50):
        row = []
        for symbol in ("SPY", "QQQ", "IWM", "GLD", "XLK"):
            on, _ = overnight_series(dates, closes, opens, symbol, cost_multiple=multiple)
            row.append(stats(on)[1])
        print(f"  {multiple:8}x " + " ".join(f"{v:8.2f}" for v in row))
    print("  Buy-and-hold Sharpe for reference: " + " ".join(
        f"{stats(overnight_series(dates, closes, opens, s)[1])[1]:8.2f}"
        for s in ("SPY", "QQQ", "IWM", "GLD", "XLK")))

    print("\n" + "=" * 98)
    print("ATTACK 3 — has the effect decayed? Overnight minus intraday, by era")
    print("=" * 98)
    print(f"  {'era':12} " + " ".join(f"{s:>8}" for s in
                                      ("SPY", "QQQ", "IWM", "GLD", "XLK", "XLF")))
    for start, end, label in era_split(dates):
        window = [d for d in dates if start <= d <= end]
        row = []
        for symbol in ("SPY", "QQQ", "IWM", "GLD", "XLK", "XLF"):
            series = [d for d in window if d in closes[symbol] and d in opens[symbol]]
            if len(series) < 30:
                row.append(float("nan"))
                continue
            gaps = [opens[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
                    for i in range(1, len(series))]
            intra = [closes[symbol][series[i]] / opens[symbol][series[i]] - 1
                     for i in range(1, len(series))]
            row.append((statistics.fmean(gaps) - statistics.fmean(intra)) * TRADING_DAYS * 100)
        print(f"  {label:12} " + " ".join(f"{v:7.2f}%" for v in row))

    print("\n" + "=" * 98)
    print("ATTACK 4 — significance of overnight-minus-intraday, paired by session")
    print("=" * 98)
    print(f"  {'asset':6} {'ann. diff':>10} {'t':>8}  vs luck bar {LUCK_BAR:.2f}")
    clears = 0
    for symbol in assets:
        series = [d for d in dates if d in closes[symbol] and d in opens[symbol]]
        # Paired differences remove the common market factor, which is the right test.
        diffs = [
            (opens[symbol][series[i]] / closes[symbol][series[i - 1]] - 1)
            - (closes[symbol][series[i]] / opens[symbol][series[i]] - 1)
            for i in range(1, len(series))
        ]
        mean = statistics.fmean(diffs)
        se = statistics.stdev(diffs) / math.sqrt(len(diffs))
        t = mean / se if se > 0 else 0.0
        if abs(t) > LUCK_BAR:
            clears += 1
        print(f"  {symbol:6} {mean * TRADING_DAYS * 100:9.2f}% {t:8.2f}  "
              f"{'CLEARS' if abs(t) > LUCK_BAR else ''}")
    print(f"\n  {clears} of {n} assets clear the {CUMULATIVE_TRIALS}-trial luck bar")

    print("\n" + "=" * 98)
    print("VERDICT")
    print("=" * 98)
    survives = beats_sharpe >= n * 0.6 and clears >= n * 0.4
    print(f"  beats buy-and-hold on Sharpe, net of cost: {beats_sharpe}/{n}")
    print(f"  clears the multiple-testing bar:           {clears}/{n}")
    print(f"\n  H11.7 is {'POSSIBLE' if survives else 'NOT ESTABLISHED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
