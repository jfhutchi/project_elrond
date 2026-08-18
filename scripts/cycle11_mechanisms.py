"""Cycle 11 Class A: mechanism-driven hypotheses that an exhausted holdout does not block.

H11.1 timing luck, H11.2 volatility targeting, H11.3 real execution cost. None of these
searches for a predictor; each measures the magnitude of an effect that follows from
construction. See reports/research/cycle11-preregistration.md.

Usage:
    uv run python scripts/cycle11_mechanisms.py
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

PROVIDER = "alpaca:sip:all:1day"
TRADING_DAYS = 252


def load() -> tuple[list[str], dict[str, dict[str, dict[str, float]]]]:
    con = sqlite3.connect(Path(__file__).parent.parent / "research" / "bars.db")
    rows = con.execute(
        "select symbol, timestamp, open, high, low, close from bars where provider = ? "
        "order by timestamp",
        (PROVIDER,),
    ).fetchall()
    con.close()
    closes: dict[str, dict[str, float]] = defaultdict(dict)
    opens: dict[str, dict[str, float]] = defaultdict(dict)
    highs: dict[str, dict[str, float]] = defaultdict(dict)
    lows: dict[str, dict[str, float]] = defaultdict(dict)
    dates: set[str] = set()
    for sym, ts, open_, high, low, close in rows:
        day = ts[:10]
        closes[sym][day] = float(close)
        opens[sym][day] = float(open_)
        highs[sym][day] = float(high)
        lows[sym][day] = float(low)
        dates.add(day)
    return sorted(dates), {"close": closes, "open": opens, "high": highs, "low": lows}


def stats(daily: list[float]) -> tuple[float, float, float]:
    """CAGR, annualised Sharpe, and max drawdown from a daily return series."""
    if not daily:
        return 0.0, 0.0, 0.0
    equity = 1.0
    peak = 1.0
    mdd = 0.0
    for value in daily:
        equity *= 1 + value
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)
    years = len(daily) / TRADING_DAYS
    cagr = equity ** (1 / years) - 1 if years > 0 and equity > 0 else -1.0
    sd = statistics.pstdev(daily)
    sharpe = (statistics.fmean(daily) / sd) * math.sqrt(TRADING_DAYS) if sd > 0 else 0.0
    return cagr, sharpe, mdd


def momentum_rotation(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    universe: list[str],
    offset: int,
    top_n: int = 10,
) -> list[float]:
    """12-1 momentum, monthly rotation, equal weight, rebalanced `offset` days into a month.

    The offset is the ONLY thing that varies across runs, so any dispersion in the results is
    timing luck by definition: no signal, parameter, or cost differs between them.
    """
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in dates:
        by_month[day[:7]].append(day)
    rebalance_days = {days[offset] for days in by_month.values() if len(days) > offset}

    holdings: list[str] = []
    daily: list[float] = []
    for index in range(1, len(dates)):
        day, prev = dates[index], dates[index - 1]
        if holdings:
            earned = [
                closes[s][day] / closes[s][prev] - 1
                for s in holdings
                if day in closes[s] and prev in closes[s]
            ]
            daily.append(statistics.fmean(earned) if earned else 0.0)
        else:
            daily.append(0.0)
        # Scored on the close and effective from the next bar, so no look-ahead.
        if day in rebalance_days and index >= TRADING_DAYS:
            start, end = dates[index - TRADING_DAYS], dates[index - 21]
            scored = sorted(
                (
                    (closes[s][end] / closes[s][start] - 1, s)
                    for s in universe
                    if start in closes[s] and end in closes[s]
                ),
                reverse=True,
            )
            holdings = [s for _, s in scored[:top_n]]
    return daily


def _returns(dates: list[str], closes: dict[str, dict[str, float]], symbol: str) -> list[float]:
    series = [d for d in dates if d in closes[symbol]]
    return [closes[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
            for i in range(1, len(series))]


def _above_trend(prices: list[float], index: int, window: int = 200) -> bool:
    recent = prices[index - window:index + 1]
    return recent[-1] >= statistics.fmean(recent)


def vol_targeted(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    symbol: str,
    *,
    target_vol: float,
    cap: float,
    window: int = 20,
    trend: bool = False,
) -> tuple[list[float], float]:
    """Exposure scaled by trailing realised vol; returns the series and mean exposure.

    The position held on day t uses only returns through t-1, so there is no look-ahead.
    """
    series = [d for d in dates if d in closes[symbol]]
    prices = [closes[symbol][d] for d in series]
    rets = _returns(dates, closes, symbol)
    out: list[float] = []
    exposures: list[float] = []
    for index in range(max(window, 200), len(rets)):
        realised = statistics.pstdev(rets[index - window:index]) * math.sqrt(TRADING_DAYS)
        exposure = min(cap, target_vol / realised) if realised > 0 else cap
        if trend and not _above_trend(prices, index):
            exposure = 0.0
        exposures.append(exposure)
        out.append(exposure * rets[index])
    return out, statistics.fmean(exposures) if exposures else 0.0


def fixed_exposure(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    symbol: str,
    *,
    leverage: float,
    trend: bool = False,
) -> list[float]:
    series = [d for d in dates if d in closes[symbol]]
    prices = [closes[symbol][d] for d in series]
    rets = _returns(dates, closes, symbol)
    out: list[float] = []
    for index in range(max(20, 200), len(rets)):
        exposure = 0.0 if (trend and not _above_trend(prices, index)) else leverage
        out.append(exposure * rets[index])
    return out


def corwin_schultz(
    highs: dict[str, dict[str, float]],
    lows: dict[str, dict[str, float]],
    dates: list[str],
    symbol: str,
) -> float:
    """Two-day high-low effective spread estimator (Corwin & Schultz 2012), in basis points.

    Estimates the spread from daily OHLC alone. Used because the market is closed and an
    after-hours quote would overstate what a strategy actually pays during the session.
    """
    days = [d for d in dates if d in highs[symbol] and d in lows[symbol]]
    estimates: list[float] = []
    for index in range(1, len(days)):
        high1, low1 = highs[symbol][days[index - 1]], lows[symbol][days[index - 1]]
        high2, low2 = highs[symbol][days[index]], lows[symbol][days[index]]
        if min(low1, low2) <= 0:
            continue
        beta = math.log(high1 / low1) ** 2 + math.log(high2 / low2) ** 2
        gamma = math.log(max(high1, high2) / min(low1, low2)) ** 2
        denominator = 3 - 2 * math.sqrt(2)
        alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / denominator - math.sqrt(
            gamma / denominator
        )
        spread = 2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha))
        if spread > 0:  # Negative estimates are noise; the standard treatment drops them.
            estimates.append(spread)
    return statistics.median(estimates) * 10_000 if estimates else float("nan")


def main() -> int:
    dates, data = load()
    closes, highs, lows = data["close"], data["high"], data["low"]
    universe = sorted(closes)
    print(f"{len(universe)} symbols, {len(dates)} sessions, {dates[0]} to {dates[-1]}\n")

    print("=" * 74)
    print("H11.1  TIMING LUCK — identical strategy, only the rebalance day differs")
    print("=" * 74)
    tranches: list[list[float]] = []
    cagrs: list[float] = []
    sharpes: list[float] = []
    for offset in range(21):
        daily = momentum_rotation(dates, closes, universe, offset)
        cagr, sharpe, mdd = stats(daily)
        tranches.append(daily)
        cagrs.append(cagr)
        sharpes.append(sharpe)
        print(f"  offset {offset:2}  CAGR {cagr * 100:6.2f}%   "
              f"Sharpe {sharpe:5.2f}   maxDD {mdd * 100:5.1f}%")

    width = min(len(t) for t in tranches)
    blended = [statistics.fmean([t[i] for t in tranches]) for i in range(width)]
    b_cagr, b_sharpe, b_mdd = stats(blended)
    spread = max(cagrs) - min(cagrs)
    print(f"\n  CAGR spread     {min(cagrs) * 100:.2f}% to {max(cagrs) * 100:.2f}%"
          f"  = {spread * 100:.2f} points of pure timing luck")
    print(f"  Sharpe spread   {min(sharpes):.2f} to {max(sharpes):.2f}")
    print(f"  median offset   Sharpe {statistics.median(sharpes):.2f}, "
          f"CAGR {statistics.median(cagrs) * 100:.2f}%")
    print(f"  21-tranche blend  CAGR {b_cagr * 100:.2f}%  Sharpe {b_sharpe:.2f}  "
          f"maxDD {b_mdd * 100:.1f}%")
    print(f"  VERDICT: {'SURVIVES' if spread > 0.01 else 'FALSIFIED'}"
          f"  (bar: CAGR spread > 1.00 point)")

    print("\n" + "=" * 74)
    print("H11.2  VOLATILITY TARGETING — compared at matched average exposure")
    print("=" * 74)
    for trend in (False, True):
        label = "SPY 200d trend" if trend else "SPY buy-and-hold"
        targeted, mean_exposure = vol_targeted(
            dates, closes, "SPY", target_vol=0.15, cap=2.0, trend=trend
        )
        flat = fixed_exposure(dates, closes, "SPY", leverage=mean_exposure, trend=trend)
        v_cagr, v_sharpe, v_mdd = stats(targeted)
        f_cagr, f_sharpe, f_mdd = stats(flat)
        print(f"\n  {label}   (matched average exposure {mean_exposure:.2f}x)")
        print(f"    fixed         CAGR {f_cagr * 100:6.2f}%   "
              f"Sharpe {f_sharpe:5.2f}   maxDD {f_mdd * 100:5.1f}%")
        print(f"    vol-targeted  CAGR {v_cagr * 100:6.2f}%   "
              f"Sharpe {v_sharpe:5.2f}   maxDD {v_mdd * 100:5.1f}%")
        print(f"    {'SURVIVES' if (v_sharpe > f_sharpe or v_mdd < f_mdd) else 'FALSIFIED'}: "
              f"Sharpe {v_sharpe - f_sharpe:+.2f}, "
              f"drawdown {(v_mdd - f_mdd) * 100:+.1f} points")

    print("\n" + "=" * 74)
    print("H11.3  REAL EXECUTION COST — Corwin-Schultz effective spread")
    print("=" * 74)
    measured = sorted(
        (bps, s) for s in universe if not math.isnan(bps := corwin_schultz(highs, lows, dates, s))
    )
    for bps, symbol in measured:
        print(f"  {symbol:5} {bps:7.1f} bps")
    median_bps = statistics.median([bps for bps, _ in measured])
    print(f"\n  median across universe: {median_bps:.1f} bps"
          f"   (assumption that killed H8/H9: 5.0 bps)")
    print(f"  VERDICT: {'FALSIFIED' if median_bps >= 5 else 'SURVIVES'}"
          f"  (bar: falsified if median >= 5bps)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
