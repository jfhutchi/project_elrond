"""Backtest the config you actually wrote, not a fixed benchmark variant.

`run_research_backtest.py` runs the six predefined comparators, and `BacktestEngine.run`
resolves switches as `component_switches or component_switches_for(variant)`
(`engine.py:212`). So a variant always overrides the config file, and every component setting
written in YAML is silently discarded.

That cost real time. Three separate config changes — target_weight_sizing,
trend_gate_per_session, and the component switches themselves — produced byte-identical output,
and they were read as three failed hypotheses before anyone checked that the harness could see
them at all. Identical results across unrelated changes is the signature of an instrument that
is not connected.

This passes `config.components` explicitly, so the run reflects the file.

Usage:
    QUANTBOT_CONFIG=config/strategy-trend-v4.yaml uv run python scripts/backtest_config.py
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import BacktestEngine, BenchmarkVariant
from quantbot.backtest.benchmarks import switches_for_config
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config, strategy_id_for


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    cash = Decimal(sys.argv[2]) if len(sys.argv) > 2 else Decimal("100")

    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)

    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        # SPY is always needed: the engine derives its session series from it.
        symbols = sorted(set(config.universe) | {"SPY", config.regime_symbol})
        histories = {s: repository.list_bars(symbol=s, provider=key) for s in symbols}
    database.close()

    missing = sorted(s for s, bars in histories.items() if not bars)
    if missing:
        print(f"no research bars for: {', '.join(missing)}")
        return 1

    print(f"config     {runtime_paths().config}")
    print(f"identity   {strategy_id_for(config)}  v{config.version}")
    print(f"universe   {len(config.universe)} names, roster {config.roster_size}")
    print(f"components {config.components.model_dump()}")
    print(f"vol target {config.volatility_target_bps}bps   "
          f"target-weight {config.target_weight_sizing}   "
          f"per-session trend {config.trend_gate_per_session}")
    print()

    # StrategyComponents (config) and ComponentSwitches (backtest) are separate types with
    # different fields -- ComponentSwitches carries atr_risk, which the config does not express.
    # That mismatch is precisely the seam where a config silently fails to reach the engine, so
    # the conversion lives in the library with a test that fails if any field stops being
    # carried, rather than as a filtered dict comprehension that would drop one in silence.
    switches = switches_for_config(config.components)

    engine = BacktestEngine(config=config, initial_cash=cash)
    # FULL_STRATEGY is the variant whose switches we are overriding; passing the config's own
    # components is the entire point of this script.
    result = engine.run(
        histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
    )

    m = result.metrics
    print(f"{'metric':22} {'value':>12}")
    print(f"{'total return':22} {m.total_return * 100:11.2f}%")
    print(f"{'CAGR':22} {m.cagr_equivalent * 100:11.2f}%")
    print(f"{'max drawdown':22} {m.maximum_drawdown * 100:11.2f}%")
    print(f"{'Sharpe':22} {m.sharpe if m.sharpe is not None else 0:12.2f}")
    print(f"{'trades':22} {len(result.trades):12}")
    print(f"{'average exposure':22} {m.exposure_fraction * 100:11.2f}%")
    print(f"{'total costs':22} {m.total_costs:12.2f}")
    print(f"{'final equity':22} {result.final_equity:12.2f}")
    print()
    print("Compare against SPY_SMA200 from run_research_backtest.py: 10.34% CAGR,")
    print("Sharpe 0.91, 19.50% maxDD, 77.37% exposure. That is the target to beat,")
    print("and it is the highest Sharpe measured anywhere in this project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
