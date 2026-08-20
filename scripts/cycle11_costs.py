"""Cycle 11 decisive test: does volatility targeting survive its own turnover?

H11.2 improved Sharpe 0.92 -> 1.09 and cut drawdown 41.5% -> 20.1% before costs. It pays for
that by changing exposure every day, so the honest question is whether the turnover eats it.

Real NBBO spreads measured on 2026-08-17 make this answerable rather than assumed:
SPY 0.26bps, QQQ 0.41, IWM 0.66, most of the universe under 2bps. Corwin-Schultz estimated
34bps for SPY from daily bars and was wrong by two orders of magnitude, because a daily
high-low range is mostly volatility, not spread.

Tested across a cost ladder up to 20bps so the conclusion does not rest on one number.

Usage:
    uv run python scripts/cycle11_costs.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_mechanisms import TRADING_DAYS, _above_trend, _returns, load, stats

# Measured NBBO on 2026-08-17 14:30-15:00Z. Half-spread is what a marketable order pays
# against the midpoint, but the full spread is used as the conservative case.
SPY_SPREAD_BPS = 0.26


def vol_targeted_with_costs(
    dates: list[str],
    closes: dict[str, dict[str, float]],
    symbol: str,
    *,
    target_vol: float,
    cap: float,
    cost_bps: float,
    window: int = 20,
    trend: bool = False,
) -> tuple[list[float], float, float]:
    """Returns the net series, mean exposure, and annualised turnover.

    Cost is charged on the change in exposure, which is what actually trades. Holding a
    constant exposure costs nothing; only the adjustment does.
    """
    series = [d for d in dates if d in closes[symbol]]
    prices = [closes[symbol][d] for d in series]
    rets = _returns(dates, closes, symbol)
    out: list[float] = []
    exposures: list[float] = []
    turnover = 0.0
    previous = 0.0
    for index in range(max(window, 200), len(rets)):
        realised = statistics.pstdev(rets[index - window:index]) * math.sqrt(TRADING_DAYS)
        exposure = min(cap, target_vol / realised) if realised > 0 else cap
        if trend and not _above_trend(prices, index):
            exposure = 0.0
        traded = abs(exposure - previous)
        turnover += traded
        out.append(exposure * rets[index] - traded * cost_bps / 10_000)
        exposures.append(exposure)
        previous = exposure
    years = len(out) / TRADING_DAYS
    return out, statistics.fmean(exposures), turnover / years if years else 0.0


def buy_and_hold(dates: list[str], closes: dict[str, dict[str, float]], symbol: str) -> list[float]:
    """The benchmark that has beaten everything this project has built."""
    rets = _returns(dates, closes, symbol)
    return rets[max(20, 200):]


def main() -> int:
    dates, data = load()
    closes = data["close"]

    print("Benchmark to beat — SPY buy-and-hold, same window, zero turnover")
    base = buy_and_hold(dates, closes, "SPY")
    b_cagr, b_sharpe, b_mdd = stats(base)
    print(f"  CAGR {b_cagr * 100:6.2f}%   Sharpe {b_sharpe:5.2f}   maxDD {b_mdd * 100:5.1f}%\n")

    print("=" * 78)
    print("H11.2 UNDER REAL COST — vol-targeted SPY, cost charged on every exposure change")
    print("=" * 78)
    print(f"{'cost':>8}  {'CAGR':>8}  {'Sharpe':>7}  {'maxDD':>7}  {'turnover':>9}  "
          f"{'vs SPY Sharpe':>14}")
    ladder = [0.0, SPY_SPREAD_BPS, 1.0, 2.0, 5.0, 10.0, 20.0]
    for cost in ladder:
        net, mean_exposure, turnover = vol_targeted_with_costs(
            dates, closes, "SPY", target_vol=0.15, cap=2.0, cost_bps=cost
        )
        cagr, sharpe, mdd = stats(net)
        marker = " <- real" if cost == SPY_SPREAD_BPS else ""
        print(f"{cost:6.2f}bp  {cagr * 100:7.2f}%  {sharpe:7.2f}  {mdd * 100:6.1f}%  "
              f"{turnover:8.1f}x  {sharpe - b_sharpe:+13.2f}{marker}")

    print(f"\n  mean exposure {mean_exposure:.2f}x")
    print("  Turnover is in units of exposure changed per year: 10x means the position was")
    print("  adjusted by a cumulative 1000% of equity over a year.")

    print("\n" + "=" * 78)
    print("BREAK-EVEN — how expensive would trading have to be to kill this?")
    print("=" * 78)
    low, high = 0.0, 2000.0
    for _ in range(60):
        mid = (low + high) / 2
        net, _, _ = vol_targeted_with_costs(
            dates, closes, "SPY", target_vol=0.15, cap=2.0, cost_bps=mid
        )
        if stats(net)[1] > b_sharpe:
            low = mid
        else:
            high = mid
    print(f"  Sharpe falls to SPY's at a round-trip cost of {low:.0f} bps")
    print(f"  Real measured SPY spread: {SPY_SPREAD_BPS} bps"
          f" — a margin of {low / SPY_SPREAD_BPS:.0f}x")

    print("\n" + "=" * 78)
    print("ROBUSTNESS — is this an artefact of the 20-day vol window or the 15% target?")
    print("=" * 78)
    print(f"{'window':>7} {'target':>7}  {'CAGR':>8}  {'Sharpe':>7}  {'maxDD':>7}")
    for window in (10, 20, 40, 60):
        for target in (0.10, 0.15, 0.20):
            net, _, _ = vol_targeted_with_costs(
                dates, closes, "SPY", target_vol=target, cap=2.0,
                cost_bps=SPY_SPREAD_BPS, window=window,
            )
            cagr, sharpe, mdd = stats(net)
            flag = "" if sharpe > b_sharpe else "   <- fails"
            print(f"{window:7} {target:7.0%}  {cagr * 100:7.2f}%  {sharpe:7.2f}  "
                  f"{mdd * 100:6.1f}%{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
