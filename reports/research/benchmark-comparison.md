# Benchmark Comparison

> SUPERSEDED. This is cycle 1, run over 5 years of IEX data (2020-07 → 2026-08). Cycle 2
> repeated it over 10.6 years of SIP data covering two bear markets — see
> [cycle-2-findings.md](cycle-2-findings.md). Numbers here are post-bugfix and correct for
> their window, but the window is half the size and almost entirely a bull market.


Generated at: 2026-08-15T14:01:41Z
Strategy: adaptive-momentum 1.1.0 (config hash `b73083b817f76b8f`)
Sessions: 1520 from 2020-07-27 to 2026-08-14 (273 of them consumed by warmup)
Initial cash: $100
Costs applied: 5bps per side, $0 per order

Data: Alpaca IEX daily bars, dividend and split adjusted. IEX is a partial-volume feed; daily OHLC on liquid ETFs tracks the consolidated tape closely but is not identical to SIP data.

| Variant | Total | CAGR | Max DD | Sharpe | Sortino | Profit factor | Win rate | Trades | Exposure | Costs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SPY_BUY_AND_HOLD | 161.98% | 17.31% | 24.51% | 1.05 | 1.52 | n/a | n/a | 0 | 100.00% | $0.05 |
| SPY_SMA200 | 76.54% | 9.88% | 19.49% | 0.94 | 1.33 | 4.51 | 46.67% | 15 | 68.82% | $1.72 |
| SPY_DONCHIAN | 21.00% | 3.21% | 24.57% | 0.40 | 0.54 | 1.66 | 62.50% | 16 | 55.13% | $1.76 |
| PURE_MOMENTUM_12_1 | 83.40% | 10.58% | 16.66% | 0.84 | 1.21 | 4.26 | 82.76% | 319 | 83.22% | $1.20 |
| MOMENTUM_TREND | 49.36% | 6.88% | 17.23% | 0.66 | 0.92 | 2.13 | 75.77% | 326 | 75.15% | $1.30 |
| FULL_STRATEGY | 1.84% | 0.30% | 5.33% | 0.11 | 0.14 | 1.05 | 37.82% | 193 | 18.66% | $1.97 |

