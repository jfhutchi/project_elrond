# Benchmark Comparison

Generated at: 2026-08-18T10:43:37Z
Strategy: adaptive-momentum 1.3.0 (config hash `5e14d03a647f4e6b`)
Sessions: 2669 from 2016-01-04 to 2026-08-14 (273 of them consumed by warmup)
Initial cash: $100
Costs applied: 5bps per side, $0 per order

Data: Alpaca IEX daily bars, dividend and split adjusted. IEX is a partial-volume feed; daily OHLC on liquid ETFs tracks the consolidated tape closely but is not identical to SIP data.

| Variant | Total | CAGR | Max DD | Sharpe | Sortino | Profit factor | Win rate | Trades | Exposure | Costs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SPY_BUY_AND_HOLD | 354.70% | 15.37% | 33.79% | 0.90 | 1.27 | n/a | n/a | 0 | 100.00% | $0.05 |
| SPY_SMA200 | 183.50% | 10.34% | 19.50% | 0.91 | 1.23 | 4.20 | 44.00% | 25 | 77.37% | $3.97 |
| SPY_DONCHIAN | 52.35% | 4.05% | 24.55% | 0.49 | 0.65 | 2.16 | 65.38% | 26 | 55.75% | $3.28 |
| PURE_MOMENTUM_12_1 | 16.06% | 1.42% | 3.69% | 0.85 | 1.19 | n/a | 100.00% | 80 | 9.03% | $0.03 |
| MOMENTUM_TREND | 8.60% | 0.78% | 2.87% | 0.66 | 0.90 | 3.68 | 94.59% | 74 | 7.44% | $0.12 |
| FULL_STRATEGY | 4.72% | 0.44% | 3.64% | 0.31 | 0.41 | 1.48 | 46.67% | 45 | 8.79% | $1.26 |

