"""Cycle 11 batch 2: H11.2b drawdown control, H11.6 the halt rule, H11.7 overnight, H11.8 TOM.

Each is stated so that a null result is reportable. See the pre-registration.

Usage:
    uv run python scripts/cycle11_batch2.py
"""

from __future__ import annotations

import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_costs import SPY_SPREAD_BPS, vol_targeted_with_costs
from cycle11_mechanisms import TRADING_DAYS, _returns, load, stats

CUMULATIVE_TRIALS = 54


def binomial_tail(k: int, n: int) -> float:
    """P(X >= k) for a fair coin — how surprised to be that k of n assets agreed."""
    return sum(math.comb(n, i) for i in range(k, n + 1)) / 2**n


def t_stat(sample: list[float]) -> tuple[float, float]:
    """One-sample t against zero, and the annualised mean."""
    if len(sample) < 2:
        return 0.0, 0.0
    mean = statistics.fmean(sample)
    se = statistics.stdev(sample) / math.sqrt(len(sample))
    return (mean / se if se > 0 else 0.0), mean * TRADING_DAYS


def halt_simulation(daily: list[float], threshold: float, *, resume_after: int) -> list[float]:
    """Apply a drawdown halt: flat once peak-to-trough exceeds threshold, resume later.

    Models the project's own rule. `resume_after` is how many sessions of being flat before
    re-entering, since the real rule needs a human and that is not instant.
    """
    out: list[float] = []
    equity = 1.0
    peak = 1.0
    halted_for = 0
    for value in daily:
        if halted_for > 0:
            out.append(0.0)
            halted_for -= 1
            if halted_for == 0:
                peak = equity  # Reset the reference on resumption, as a restart would.
            continue
        out.append(value)
        equity *= 1 + value
        peak = max(peak, equity)
        if (peak - equity) / peak > threshold:
            halted_for = resume_after
    return out


def main() -> int:
    dates, data = load()
    closes, opens = data["close"], data["open"]

    print("=" * 96)
    print("H11.2b — vol targeting as DRAWDOWN CONTROL, not alpha")
    print("=" * 96)
    assets = ["SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV",
              "XLY", "XLP", "XLU", "XLB", "XLI", "TLT", "GLD", "VNQ"]
    reduced = 0
    cagr_cost: list[float] = []
    print(f"  {'asset':6} {'maxDD vt':>9} {'maxDD bh':>9} {'change':>8}  {'CAGR vt':>8} "
          f"{'CAGR bh':>8} {'give-up':>8}")
    for symbol in assets:
        net, _, _ = vol_targeted_with_costs(
            dates, closes, symbol, target_vol=0.15, cap=2.0, cost_bps=SPY_SPREAD_BPS
        )
        base = _returns(dates, closes, symbol)[max(20, 200):]
        n_cagr, _, n_mdd = stats(net)
        b_cagr, _, b_mdd = stats(base)
        if n_mdd < b_mdd:
            reduced += 1
        cagr_cost.append(n_cagr - b_cagr)
        print(f"  {symbol:6} {n_mdd * 100:8.1f}% {b_mdd * 100:8.1f}% "
              f"{(n_mdd - b_mdd) * 100:+7.1f}p  {n_cagr * 100:7.2f}% {b_cagr * 100:7.2f}% "
              f"{(n_cagr - b_cagr) * 100:+7.2f}p")
    p = binomial_tail(reduced, len(assets))
    print(f"\n  drawdown reduced on {reduced} of {len(assets)} assets, "
          f"P(>= that many by chance) = {p:.4f}")
    print(f"  median CAGR given up: {statistics.median(cagr_cost) * 100:+.2f} points/yr")
    print(f"  VERDICT: {'SURVIVES' if reduced >= len(assets) * 0.7 else 'FALSIFIED'}"
          f"  (bar: clear majority of assets)")

    print("\n" + "=" * 96)
    print("H11.6 — does the 20% drawdown halt help or hurt?")
    print("=" * 96)
    spy = _returns(dates, closes, "SPY")
    print(f"  {'variant':34} {'CAGR':>8} {'Sharpe':>7} {'maxDD':>7} {'terminal $100':>14}")
    no_halt_cagr, no_halt_sharpe, no_halt_mdd = stats(spy)
    terminal = 100.0
    for value in spy:
        terminal *= 1 + value
    print(f"  {'no halt (ride it through)':34} {no_halt_cagr * 100:7.2f}% "
          f"{no_halt_sharpe:7.2f} {no_halt_mdd * 100:6.1f}% {terminal:13.2f}")
    for resume in (5, 21, 63):
        halted = halt_simulation(spy, 0.20, resume_after=resume)
        h_cagr, h_sharpe, h_mdd = stats(halted)
        h_terminal = 100.0
        for value in halted:
            h_terminal *= 1 + value
        label = f"20% halt, resume after {resume}d"
        print(f"  {label:34} {h_cagr * 100:7.2f}% {h_sharpe:7.2f} {h_mdd * 100:6.1f}% "
              f"{h_terminal:13.2f}")
    print("\n  The halt is a capital-preservation rule, not a return rule. Reported so the")
    print("  cost of the project's own safety constraint is explicit rather than assumed.")

    print("\n" + "=" * 96)
    print("H11.7 — overnight (close->open) versus intraday (open->close)")
    print("=" * 96)
    print(f"  {'asset':6} {'overnight/yr':>13} {'intraday/yr':>12} {'t(overnight)':>13} "
          f"{'t(intraday)':>12}")
    overnight_wins = 0
    for symbol in ["SPY", "QQQ", "IWM", "EFA", "XLK", "XLF", "GLD", "TLT"]:
        series = [d for d in dates if d in closes[symbol] and d in opens[symbol]]
        overnight = [opens[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
                     for i in range(1, len(series))]
        intraday = [closes[symbol][series[i]] / opens[symbol][series[i]] - 1
                    for i in range(1, len(series))]
        t_on, ann_on = t_stat(overnight)
        t_in, ann_in = t_stat(intraday)
        if ann_on > ann_in:
            overnight_wins += 1
        print(f"  {symbol:6} {ann_on * 100:12.2f}% {ann_in * 100:11.2f}% {t_on:13.2f} "
              f"{t_in:12.2f}")
    print(f"\n  overnight beat intraday on {overnight_wins} of 8 assets")
    print(f"  luck bar for {CUMULATIVE_TRIALS} cumulative trials: |t| > "
          f"{math.sqrt(2 * math.log(CUMULATIVE_TRIALS)):.2f}")
    print("  Cost note: capturing overnight-only needs 2 trades/day = ~504/yr. At SPY's")
    print(f"  measured {SPY_SPREAD_BPS}bps that is "
          f"~{504 * SPY_SPREAD_BPS / 100:.2f}% a year in spread.")

    print("\n" + "=" * 96)
    print("H11.8 — turn-of-month effect")
    print("=" * 96)
    by_month: dict[str, list[str]] = defaultdict(list)
    for day in dates:
        by_month[day[:7]].append(day)
    tom_days: set[str] = set()
    for days in by_month.values():
        tom_days.update(days[:3])   # first three sessions
        tom_days.update(days[-1:])  # and the last
    print(f"  {'asset':6} {'TOM ann.':>10} {'rest ann.':>10} {'t(TOM-rest)':>12} {'TOM days':>9}")
    tom_wins = 0
    for symbol in ["SPY", "QQQ", "IWM", "EFA", "XLK", "GLD", "TLT"]:
        series = [d for d in dates if d in closes[symbol]]
        tom: list[float] = []
        rest: list[float] = []
        for i in range(1, len(series)):
            r = closes[symbol][series[i]] / closes[symbol][series[i - 1]] - 1
            (tom if series[i] in tom_days else rest).append(r)
        mean_diff = statistics.fmean(tom) - statistics.fmean(rest)
        pooled = math.sqrt(
            statistics.pvariance(tom) / len(tom) + statistics.pvariance(rest) / len(rest)
        )
        t = mean_diff / pooled if pooled > 0 else 0.0
        if t > 0:
            tom_wins += 1
        print(f"  {symbol:6} {statistics.fmean(tom) * TRADING_DAYS * 100:9.2f}% "
              f"{statistics.fmean(rest) * TRADING_DAYS * 100:9.2f}% {t:12.2f} {len(tom):9}")
    print(f"\n  TOM positive on {tom_wins} of 7 assets")
    print(f"  luck bar for {CUMULATIVE_TRIALS} cumulative trials: |t| > "
          f"{math.sqrt(2 * math.log(CUMULATIVE_TRIALS)):.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
