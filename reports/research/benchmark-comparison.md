# Benchmark Comparison

Generated at: 2026-08-19T13:57:23Z
Strategy: adaptive-momentum 1.2.0 (config hash `309894d8d8a5296e`)
Sessions: 2669 from 2016-01-04 to 2026-08-14 (273 of them consumed by warmup)
Initial cash: $100
Costs applied: 5bps per side, $0 per order

`CONFIGURED_STRATEGY` is the only row applying this config's component selection. Every other row is a fixed comparator and ignores it by design; a runner reporting only those rows is why three config changes once produced identical output.

Data: Alpaca IEX daily bars, dividend and split adjusted. IEX is a partial-volume feed; daily OHLC on liquid ETFs tracks the consolidated tape closely but is not identical to SIP data.

| Variant | Total | CAGR | Max DD | Sharpe | Sortino | Profit factor | Win rate | Trades | Exposure | Costs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| SPY_BUY_AND_HOLD | 354.70% | 15.37% | 33.79% | 0.90 | 1.27 | n/a | n/a | 0 | 100.00% | $0.05 |
| SPY_SMA200 | 183.50% | 10.34% | 19.50% | 0.91 | 1.23 | 4.20 | 44.00% | 25 | 77.37% | $3.97 |
| SPY_DONCHIAN | 52.35% | 4.05% | 24.55% | 0.49 | 0.65 | 2.16 | 65.38% | 26 | 55.75% | $3.28 |
| PURE_MOMENTUM_12_1 | 182.24% | 10.29% | 28.12% | 0.77 | 1.07 | 3.83 | 84.14% | 643 | 89.81% | $2.95 |
| MOMENTUM_TREND | 132.78% | 8.30% | 19.90% | 0.76 | 1.04 | 2.63 | 81.65% | 654 | 83.35% | $3.20 |
| FULL_STRATEGY | 5.41% | 0.50% | 7.84% | 0.15 | 0.20 | 1.10 | 40.88% | 411 | 24.06% | $4.30 |
| CONFIGURED_STRATEGY | 52.46% | 4.06% | 9.09% | 0.69 | 0.93 | 2.05 | 35.66% | 272 | 41.95% | $2.82 |

