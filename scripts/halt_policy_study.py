"""Measure the three candidate fixes for the absorbing drawdown halt.

The defect (severity-1, `STATUS.md`): once equity is 15% below its high-water mark,
`entry_halted` blocks new positions — but a strategy that cannot open positions cannot earn
back the drawdown. The halt is triggered by a loss and then forbids the only mechanism that
could undo it. It blocks entry on 70.8% of backtest sessions and the same thresholds are live
in `config/strategy-v1-2.yaml`.

`docs/per-session-trend-spec.md` proposes three fixes and says to measure before choosing.
This measures them.

  A. hysteresis        — halt at 15%, resume at a shallower drawdown
  B. reset high-water  — on halt, measure drawdown from the halt point, not an unreachable peak
  C. exposure floor    — scale toward a floor instead of hard zero
  D. rolling lookback  — `drawdown_lookback_sessions`, already built and off by default, which
                         measures the high-water mark over a finite window so an old peak ages
                         out. It escapes for a different reason than the others: it does not
                         need equity to move, which matters because a halted account's equity
                         cannot move.

The comparison must be path-dependent: the halt changes returns, which changes equity, which
changes the halt. Simulating on a fixed return series with the halt applied afterwards would
miss the feedback that makes the trap absorbing in the first place.

Usage:
    uv run python scripts/halt_policy_study.py
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path

PROVIDER = "alpaca:sip:all:1day"
TRADING_DAYS = 252
HALT_AT = 0.15  # thresholds[2] in the deployed config
LIQUIDATE_AT = 0.20


def load_closes() -> tuple[list[str], dict[str, dict[str, float]]]:
    con = sqlite3.connect(Path(__file__).parent.parent / "research" / "bars.db")
    rows = con.execute(
        "select symbol, timestamp, close from bars where provider = ? order by timestamp",
        (PROVIDER,),
    ).fetchall()
    con.close()
    closes: dict[str, dict[str, float]] = defaultdict(dict)
    dates: set[str] = set()
    for symbol, ts, close in rows:
        closes[symbol][ts[:10]] = float(close)
        dates.add(ts[:10])
    return sorted(dates), closes


def simulate(
    daily: list[float],
    *,
    policy: str,
    resume_at: float = 0.10,
    floor: float = 0.25,
    lookback: int = 504,
) -> tuple[float, float, float, int]:
    """Run a halt policy against a return stream, with the feedback loop intact.

    `daily` is the return the strategy would earn if fully invested. Exposure is decided each
    session by the halt policy, so equity, high-water and the halt state all co-evolve.

    Returns (terminal wealth, fraction of sessions halted, max drawdown, escapes).
    """
    equity = 100.0
    high_water = 100.0
    history: list[float] = [100.0]
    halted = False
    halted_sessions = 0
    escapes = 0
    peak = 100.0
    mdd = 0.0

    for r in daily:
        if policy == "lookback":
            # A finite window: the peak ages out on its own, so release does not require the
            # equity to recover. That is the only escape route available to a halted account,
            # whose equity is frozen precisely because it is halted.
            high_water = max(history[-lookback:])
        drawdown = (high_water - equity) / high_water if high_water > 0 else 0.0

        if policy == "current":
            # Absorbing: no exit condition exists at all.
            halted = drawdown >= HALT_AT
        elif policy == "hysteresis":
            # Halt on the deep threshold, release on a shallower one.
            if not halted and drawdown >= HALT_AT:
                halted = True
            elif halted and drawdown <= resume_at:
                halted = False
                escapes += 1
        elif policy == "reset_hwm":
            # On halt, re-anchor the high-water mark so the drawdown is measured from here.
            if drawdown >= HALT_AT:
                high_water = equity
                escapes += 1
            halted = False
        elif policy == "floor":
            # Never go to zero exposure; the hard zero is what makes the trap absorbing.
            halted = False
        elif policy == "lookback":
            was_halted = halted
            halted = drawdown >= HALT_AT
            if was_halted and not halted:
                escapes += 1
        else:
            raise ValueError(policy)

        if policy == "floor":
            exposure = floor if drawdown >= HALT_AT else 1.0
        else:
            exposure = 0.0 if halted else 1.0

        if halted:
            halted_sessions += 1
        equity *= 1 + exposure * r
        history.append(equity)
        if policy != "lookback":
            high_water = max(high_water, equity)
        peak = max(peak, equity)
        mdd = max(mdd, (peak - equity) / peak)

    return equity, halted_sessions / len(daily), mdd, escapes


def returns(dates: list[str], closes: dict[str, dict[str, float]], symbol: str) -> list[float]:
    series = [d for d in dates if d in closes[symbol]]
    return [closes[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
            for i in range(1, len(series))]


def trend_returns(dates: list[str], closes: dict[str, dict[str, float]]) -> list[float]:
    """SPY held only while above its 200-day average — the one thing measured to work."""
    series = [d for d in dates if d in closes["SPY"]]
    prices = [closes["SPY"][d] for d in series]
    out: list[float] = []
    for i in range(200, len(prices) - 1):
        window = prices[i - 200:i + 1]
        above = window[-1] >= statistics.fmean(window)
        out.append((prices[i + 1] / prices[i] - 1) if above else 0.0)
    return out


def report(label: str, daily: list[float]) -> None:
    print(f"\n  {label}")
    print(f"    {'policy':22} {'$100 becomes':>13} {'% halted':>9} {'maxDD':>7} {'escapes':>8}")
    baseline = None
    for policy, kwargs in (
        ("current", {}),
        ("hysteresis", {"resume_at": 0.10}),
        ("hysteresis", {"resume_at": 0.05}),
        ("reset_hwm", {}),
        ("floor", {"floor": 0.25}),
        ("floor", {"floor": 0.50}),
        ("lookback", {"lookback": 504}),
        ("lookback", {"lookback": 252}),
        ("lookback", {"lookback": 126}),
    ):
        wealth, share, mdd, escapes = simulate(daily, policy=policy, **kwargs)
        if baseline is None:
            baseline = wealth
        detail = "".join(f" {k}={v}" for k, v in kwargs.items())
        delta = f"{wealth - baseline:+9.2f}" if policy != "current" else "  baseline"
        print(f"    {policy + detail:22} {wealth:12.2f} {share * 100:8.1f}% "
              f"{mdd * 100:6.1f}% {escapes:8} {delta}")


def main() -> int:
    dates, closes = load_closes()
    spy = returns(dates, closes, "SPY")
    trend = trend_returns(dates, closes)

    print("=" * 82)
    print("THE ABSORBING DRAWDOWN HALT — measuring the three proposed fixes")
    print("=" * 82)
    print(f"  halt at {HALT_AT:.0%} below high-water, {len(spy)} sessions, "
          f"{dates[0]} to {dates[-1]}")
    print("  Exposure and equity co-evolve, so the halt's feedback on its own recovery is")
    print("  preserved — that feedback is what makes the current policy absorbing.")

    report("SPY buy-and-hold underneath", spy)
    report("SPY 200-day trend underneath (the measured-good strategy)", trend)

    print("\n" + "=" * 82)
    print("NO-HALT REFERENCE — what the halt costs against never halting at all")
    print("=" * 82)
    for label, daily in (("SPY buy-and-hold", spy), ("SPY 200d trend", trend)):
        equity = 100.0
        peak = 100.0
        mdd = 0.0
        for r in daily:
            equity *= 1 + r
            peak = max(peak, equity)
            mdd = max(mdd, (peak - equity) / peak)
        cagr = (equity / 100) ** (TRADING_DAYS / len(daily)) - 1
        print(f"  {label:22} ${equity:8.2f}  CAGR {cagr * 100:6.2f}%  maxDD {mdd * 100:5.1f}%")

    print("\n  A halt is a capital-preservation rule and is expected to cost return. The")
    print("  question is not whether it costs, but whether it can ever be escaped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
