"""Cycle 11: try to kill H11.2 rather than confirm it.

Vol targeting beat SPY on every parameter combination tested, which is exactly when a result
deserves the most suspicion. Four attacks, each capable of refuting it:

1. COVID dependence. Feb-Mar 2020 is one enormous volatility spike that vol targeting is built
   to exploit. If the whole result is that single event, it is one observation, not an edge.
2. Sub-period stability. An effect present in every era is a mechanism; one confined to a
   single era is a story fitted after the fact.
3. Cross-asset generality. A real mechanism should not be a property of SPY alone.
4. Statistical significance, correcting for the fact that the two return series are the same
   asset and therefore highly correlated. A naive two-sample test would badly overstate it.

Usage:
    uv run python scripts/cycle11_attack.py
"""

from __future__ import annotations

import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from cycle11_costs import SPY_SPREAD_BPS, vol_targeted_with_costs
from cycle11_mechanisms import TRADING_DAYS, _returns, load, stats

CUMULATIVE_TRIALS = 45 + 5  # prior cycles plus this one's pre-registered hypotheses


def sharpe_difference_test(a: list[float], b: list[float]) -> tuple[float, float, float]:
    """Jobson-Korkie test with Memmel's correction, for two correlated return series.

    Treating these as independent samples would overstate significance badly: both series are
    the same asset at different exposures, so they share nearly all their variance.
    """
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    mu_a, mu_b = statistics.fmean(a), statistics.fmean(b)
    sd_a, sd_b = statistics.pstdev(a), statistics.pstdev(b)
    if sd_a == 0 or sd_b == 0:
        return 0.0, 0.0, 1.0
    sharpe_a, sharpe_b = mu_a / sd_a, mu_b / sd_b
    cov = statistics.fmean([(x - mu_a) * (y - mu_b) for x, y in zip(a, b, strict=True)])
    rho = cov / (sd_a * sd_b)
    # Memmel (2003) corrected variance of the Sharpe difference.
    theta = (
        2 - 2 * rho
        + 0.5 * (sharpe_a**2 + sharpe_b**2 - 2 * sharpe_a * sharpe_b * rho**2)
    ) / n
    if theta <= 0:
        return (sharpe_a - sharpe_b) * math.sqrt(TRADING_DAYS), 0.0, 1.0
    z = (sharpe_a - sharpe_b) / math.sqrt(theta)
    p = math.erfc(abs(z) / math.sqrt(2))
    annual_diff = (sharpe_a - sharpe_b) * math.sqrt(TRADING_DAYS)
    return annual_diff, z, p


def slice_window(dates: list[str], closes: dict, symbol: str,
                 start: str, end: str) -> dict[str, dict[str, float]]:
    kept = {s: {d: v for d, v in closes[s].items() if start <= d <= end} for s in closes}
    return kept


def run(dates: list[str], closes: dict, symbol: str, **kw) -> tuple[list[float], list[float]]:
    """Vol-targeted net series and the matching buy-and-hold baseline over the same window."""
    net, _, _ = vol_targeted_with_costs(
        dates, closes, symbol, target_vol=0.15, cap=2.0, cost_bps=SPY_SPREAD_BPS, **kw
    )
    rets = _returns(dates, closes, symbol)
    base = rets[max(20, 200):]
    return net, base


def report(label: str, net: list[float], base: list[float]) -> bool:
    n_cagr, n_sharpe, n_mdd = stats(net)
    b_cagr, b_sharpe, b_mdd = stats(base)
    wins = n_sharpe > b_sharpe
    print(f"  {label:26} vol-target {n_sharpe:5.2f}  buy-hold {b_sharpe:5.2f}  "
          f"diff {n_sharpe - b_sharpe:+5.2f}  "
          f"CAGR {n_cagr * 100:6.2f}% v {b_cagr * 100:6.2f}%  "
          f"maxDD {n_mdd * 100:4.1f}% v {b_mdd * 100:4.1f}%  {'WIN' if wins else 'LOSS'}")
    return wins


def main() -> int:
    dates, data = load()
    closes = data["close"]

    print("=" * 100)
    print("ATTACK 1 — is the whole result the COVID volatility spike?")
    print("=" * 100)
    ex_covid_dates = [d for d in dates if not ("2020-02-01" <= d <= "2020-06-30")]
    ex = slice_window(dates, closes, "SPY", "1900-01-01", "2100-01-01")
    net_full, base_full = run(dates, closes, "SPY")
    report("full history", net_full, base_full)
    net_ex, base_ex = run(ex_covid_dates, ex, "SPY")
    survived_covid = report("COVID Feb-Jun 2020 removed", net_ex, base_ex)

    print("\n" + "=" * 100)
    print("ATTACK 2 — is it stable across eras, or confined to one?")
    print("=" * 100)
    eras = [
        ("2016-01-01", "2018-12-31", "2016-2018"),
        ("2019-01-01", "2021-12-31", "2019-2021"),
        ("2022-01-01", "2024-12-31", "2022-2024"),
        ("2025-01-01", "2026-12-31", "2025-2026"),
    ]
    era_wins = 0
    for start, end, label in eras:
        window = [d for d in dates if start <= d <= end]
        # Each era needs 200 days of history before it to warm the trend and vol windows.
        warm = [d for d in dates if d < start][-260:]
        sub = slice_window(dates, closes, "SPY", warm[0] if warm else start, end)
        net, base = run(warm + window, sub, "SPY")
        if len(net) > 60 and report(label, net, base):
            era_wins += 1

    print("\n" + "=" * 100)
    print("ATTACK 3 — does it generalise beyond SPY, or is it a property of one series?")
    print("=" * 100)
    asset_wins = 0
    assets = ["SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLK", "XLF", "XLE", "XLV", "TLT", "GLD"]
    for symbol in assets:
        net, base = run(dates, closes, symbol)
        if report(symbol, net, base):
            asset_wins += 1

    print("\n" + "=" * 100)
    print("ATTACK 4 — significance, correcting for the two series being the same asset")
    print("=" * 100)
    diff, z, p = sharpe_difference_test(net_full, base_full)
    print(f"  annualised Sharpe difference  {diff:+.3f}")
    print(f"  Jobson-Korkie/Memmel z        {z:.2f}")
    print(f"  p-value                       {p:.4f}")
    threshold = math.sqrt(2 * math.log(CUMULATIVE_TRIALS))
    print(f"  luck threshold for {CUMULATIVE_TRIALS} cumulative trials: z > {threshold:.2f}")
    beats_luck = abs(z) > threshold
    print(f"  {'CLEARS' if beats_luck else 'FAILS'} the multiple-testing bar")

    print("\n" + "=" * 100)
    print("VERDICT")
    print("=" * 100)
    print(f"  survives COVID removal      {survived_covid}")
    print(f"  eras won                    {era_wins} of {len(eras)}")
    print(f"  assets won                  {asset_wins} of {len(assets)}")
    print(f"  clears multiple-testing bar {beats_luck}")
    verdict = survived_covid and era_wins >= 3 and asset_wins >= 8 and beats_luck
    print(f"\n  H11.2 is {'POSSIBLE — survived every attack' if verdict else 'NOT ESTABLISHED'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
