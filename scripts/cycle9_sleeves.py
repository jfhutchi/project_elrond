"""Cycle 9: are energy, commodity or FX markets more trend-predictable than equities?

Pre-registered before running.

HYPOTHESIS (user's, made falsifiable). Energy prices respond to persistent geopolitical
drivers, so trends in energy should be more persistent than in equities, and the SAME trend
rule should earn a higher Sharpe there.

The rule is held identical across every sleeve so the comparison isolates the market, not the
strategy: hold each instrument equal-weight while its close is above its own N-day average,
else hold cash. One parameter, N=100, taken unchanged from cycle 5's BTC test so it is not
re-fitted here.

MEASURED PER SLEEVE
  * Sharpe, total return, max drawdown, net of 5bps per side
  * trend persistence: mean consecutive sessions the signal stays on. This is the direct
    test of "more predictable" - a market whose trends persist longer is more trend-tradeable
  * correlation to the existing equity sleeve, which is what decides diversification value

VERDICT RULE, fixed in advance. A sleeve is interesting only if EITHER its Sharpe exceeds the
equity sleeve's by more than 1.96 standard errors, OR adding it improves the blended Sharpe by
more than 1.96 standard errors. Raw comparisons are not verdicts - that error has been made
twice already in this project and corrected both times.

Usage:
    uv run python scripts/cycle9_sleeves.py
"""

from __future__ import annotations

import math
import os
import statistics

import httpx

PERIODS_PER_YEAR = 252
TREND_WINDOW = 100
COST_PER_SIDE = 0.0005
START = "2016-01-01T00:00:00Z"
END = "2026-08-15T00:00:00Z"

SLEEVES: dict[str, tuple[str, ...]] = {
    "equity": ("SPY", "QQQ", "IWM", "MDY", "EFA", "EEM", "XLB", "XLF", "XLI",
               "XLK", "XLP", "XLU", "XLV", "XLY"),
    "energy": ("XLE", "XOP", "OIH", "USO", "UNG", "DBO"),
    "commodity": ("DBC", "GLD", "SLV", "DBA", "CPER"),
    "fx": ("UUP", "UDN", "FXE", "FXY", "FXB", "FXF", "CEW"),
}


def _fetch(symbols: tuple[str, ...]) -> dict[str, dict[str, float]]:
    headers = {
        "APCA-API-KEY-ID": os.environ["ALPACA_PAPER_API_KEY"],
        "APCA-API-SECRET-KEY": os.environ["ALPACA_PAPER_API_SECRET"],
    }
    out: dict[str, dict[str, float]] = {s: {} for s in symbols}
    token = None
    while True:
        params = {
            "symbols": ",".join(symbols), "timeframe": "1Day", "start": START, "end": END,
            "feed": "sip", "limit": "10000", "sort": "asc",
        }
        if token:
            params["page_token"] = token
        payload = httpx.get(
            "https://data.alpaca.markets/v2/stocks/bars",
            params=params, headers=headers, timeout=180,
        ).json()
        for symbol, rows in (payload.get("bars") or {}).items():
            for row in rows:
                out[symbol][row["t"][:10]] = float(row["c"])
        token = payload.get("next_page_token")
        if not token:
            return out


def _sleeve_returns(
    closes: dict[str, dict[str, float]], members: tuple[str, ...], dates: list[str]
) -> tuple[list[float], float]:
    """Equal-weight trend sleeve, plus mean consecutive sessions the signal stays on."""
    held = dict.fromkeys(members, 0.0)
    returns: list[float] = []
    runs: list[int] = []
    current = dict.fromkeys(members, 0)
    for index in range(TREND_WINDOW, len(dates) - 1):
        day, nxt = dates[index], dates[index + 1]
        gross, cost, active = 0.0, 0.0, []
        for symbol in members:
            series = closes[symbol]
            window = [series[d] for d in dates[index - TREND_WINDOW : index] if d in series]
            if len(window) < TREND_WINDOW // 2 or day not in series or nxt not in series:
                continue
            signal = 1.0 if series[day] > statistics.fmean(window) else 0.0
            if signal > 0:
                active.append(symbol)
                current[symbol] += 1
            elif current[symbol]:
                runs.append(current[symbol])
                current[symbol] = 0
            cost += abs(signal - held[symbol]) * COST_PER_SIDE
            held[symbol] = signal
        if active:
            weight = 1.0 / len(active)
            for symbol in active:
                series = closes[symbol]
                gross += weight * (series[nxt] / series[day] - 1)
        returns.append(gross - cost / max(len(members), 1))
    runs.extend(v for v in current.values() if v)
    return returns, statistics.fmean(runs) if runs else 0.0


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    dev = statistics.pstdev(returns)
    return 0.0 if dev == 0 else statistics.fmean(returns) / dev * math.sqrt(PERIODS_PER_YEAR)


def _drawdown(returns: list[float]) -> float:
    equity, peak, worst = 1.0, 1.0, 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        worst = max(worst, (peak - equity) / peak)
    return worst


def _correlation(a: list[float], b: list[float]) -> float:
    n = min(len(a), len(b))
    x, y = a[:n], b[:n]
    mx, my = statistics.fmean(x), statistics.fmean(y)
    num = sum((x[i] - mx) * (y[i] - my) for i in range(n))
    dx = math.sqrt(sum((v - mx) ** 2 for v in x))
    dy = math.sqrt(sum((v - my) ** 2 for v in y))
    return 0.0 if dx == 0 or dy == 0 else num / (dx * dy)


def main() -> int:
    every = tuple(sorted({s for members in SLEEVES.values() for s in members}))
    closes = _fetch(every)
    common = sorted(set.intersection(*(set(closes[s]) for s in every)))
    print(f"{len(every)} instruments, {len(common)} common sessions "
          f"{common[0]} .. {common[-1]}")
    print(f"identical rule everywhere: hold while close > {TREND_WINDOW}-day average, "
          f"{COST_PER_SIDE * 10000:.0f}bps/side\n")

    results: dict[str, tuple[list[float], float]] = {}
    for name, members in SLEEVES.items():
        results[name] = _sleeve_returns(closes, members, common)

    equity_returns = results["equity"][0]
    standard_error = math.sqrt(PERIODS_PER_YEAR / len(equity_returns))
    base = _sharpe(equity_returns)

    print(f"  {'sleeve':11}{'total':>9}{'CAGR':>8}{'Sharpe':>8}{'maxDD':>8}"
          f"{'persistence':>13}{'corr(eq)':>10}")
    print("  " + "-" * 67)
    for name, (returns, persistence) in results.items():
        total = math.prod(1 + r for r in returns) - 1
        years = len(returns) / PERIODS_PER_YEAR
        cagr = (1 + total) ** (1 / years) - 1 if total > -1 else -1.0
        corr = 1.0 if name == "equity" else _correlation(returns, equity_returns)
        print(f"  {name:11}{total * 100:>8.1f}%{cagr * 100:>7.2f}%{_sharpe(returns):>8.2f}"
              f"{_drawdown(returns) * 100:>7.1f}%{persistence:>12.1f}d{corr:>10.2f}")

    print(f"\n  standard error on Sharpe at n={len(equity_returns)}: {standard_error:.3f}")
    print(f"  equity sleeve Sharpe {base:.2f}\n")
    print("  Test 1 - does any sleeve beat equities standalone?")
    for name, (returns, _) in results.items():
        if name == "equity":
            continue
        delta = _sharpe(returns) - base
        sigmas = delta / standard_error
        flag = "BEATS" if sigmas >= 1.96 else "no"
        print(f"    {name:11}{delta:+.2f} Sharpe = {sigmas:+.2f} sigma  -> {flag}")

    print("\n  Test 2 - does adding it to equities improve the blend?")
    trials = 0
    for name, (returns, _) in results.items():
        if name == "equity":
            continue
        n = min(len(returns), len(equity_returns))
        best_weight, best_sharpe = 0.0, base
        for weight in (0.1, 0.2, 0.3, 0.4, 0.5):
            blend = [(1 - weight) * equity_returns[i] + weight * returns[i] for i in range(n)]
            value = _sharpe(blend)
            trials += 1
            if value > best_sharpe:
                best_weight, best_sharpe = weight, value
        sigmas = (best_sharpe - base) / standard_error
        flag = "IMPROVES" if sigmas >= 1.96 else "no"
        print(f"    {name:11} best {best_weight * 100:>3.0f}% -> Sharpe {best_sharpe:.2f}"
              f"  ({sigmas:+.2f} sigma)  -> {flag}")
    print(f"\n  weight trials run: {trials}. Best-of-N is a selected maximum; the 1.96 sigma")
    print("  bar is the minimum, and a stricter deflation would raise it further.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
