"""Can the engine express SPY_SMA200 at all? Instrumented, not reasoned about.

`STATUS.md` §6h is the project's central architectural finding: the engine cannot express the one
rule measured to beat the market. `SPY_SMA200` -- hold SPY while it is above its 200-day average
-- returns 10.34% CAGR at Sharpe 0.91 on 77.37% exposure, and configs written to reproduce it
reached 30% and 43%.

`docs/per-session-trend-spec.md` diagnosed that five times. Three diagnoses were refuted by
measurement, two by reading. The fifth -- the absorbing drawdown halt -- was accepted, and it was
measured on `strategy-trend-v4`.

It does not transfer. Measured 2026-08-19, the deployed `strategy-v1-2.yaml` halts entry on **0**
sessions with a 9.09% max drawdown against a 15% trigger. The halt is config-dependent, so the
gap for any other config is undiagnosed.

Worse, every prior attempt ran through a harness that ignored the config's components entirely
(`4644c3a`), so the numbers those diagnoses were reasoning about described a fixed benchmark
variant rather than the config under test.

This runs the clean comparison the spec itself names as the target -- `universe: [SPY]`, trend
gate only -- through the corrected harness, and **instruments every session** rather than
inferring. The spec has been wrong five times by reasoning ahead of measurement; this records
where sessions actually go.

Classification `EXPLORATORY`: consumed window, no registration, no probes. It diagnoses machinery,
it does not test a hypothesis, and it spends no statistical budget because it searches nothing.

Usage:
    QUANTBOT_MARKET_DATA_FEED=sip uv run python scripts/spy_only_exposure_diagnosis.py \
        ../../../research/bars.db 100
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BacktestEngine,
    BenchmarkVariant,
    ComponentSwitches,
    component_switches_for,
)
from quantbot.market_data import MarketDataCache
from quantbot.risk import calculate_drawdown
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyComponents, load_strategy_config, sma


def _pct(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * Decimal('100'):.2f}%"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research") / "bars.db"
    cash = Decimal(sys.argv[2]) if len(sys.argv) > 2 else Decimal("100")

    deployed = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    key = MarketDataCache.provider_key(feed.provider, feed.feed, feed.adjustment, feed.timeframe)

    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        spy = repository.list_bars(symbol="SPY", provider=key)
    database.close()
    if not spy:
        print("no SPY bars in that database")
        return 1

    histories = {"SPY": spy}

    # The spec's own target configuration: one name, trend gate only, nothing else.
    config = deployed.model_copy(
        update={
            "universe": ("SPY",),
            "roster_size": 1,
            "components": StrategyComponents(
                momentum=False,
                asset_trend=True,
                market_regime=False,
                donchian_entry=False,
                donchian_exit=False,
                trailing_stop=False,
                roster_exit=False,
            ),
        }
    )
    # atr_risk has no config field, so it is set here explicitly rather than inherited: the
    # benchmark applies no ATR cap, and leaving it on would compare two different rules.
    switches = ComponentSwitches(
        momentum=False,
        asset_trend=True,
        market_regime=False,
        donchian_entry=False,
        donchian_exit=False,
        atr_risk=False,
        trailing_stop=False,
        roster_exit=False,
    )

    engine = BacktestEngine(config, initial_cash=cash)
    configured = engine.run(
        histories, BenchmarkVariant.FULL_STRATEGY, component_switches=switches
    )
    benchmark = BacktestEngine(deployed, initial_cash=cash).run(
        histories,
        BenchmarkVariant.SPY_SMA200,
        component_switches=component_switches_for(BenchmarkVariant.SPY_SMA200),
    )

    print(f"sessions          {len(spy)}  {spy[0].timestamp.date()} .. {spy[-1].timestamp.date()}")
    print(f"trend period      {config.trend_period}")
    print()
    header = f"{'run':28}{'CAGR':>9}{'Sharpe':>8}{'exposure':>10}{'trades':>8}{'maxDD':>9}"
    print(header)
    print("-" * len(header))
    for label, result in (
        ("SPY_SMA200 benchmark", benchmark),
        ("engine, SPY + trend only", configured),
    ):
        metrics = result.metrics
        print(
            f"{label:28}{_pct(metrics.cagr_equivalent):>9}{metrics.sharpe or 0:>8.2f}"
            f"{_pct(metrics.exposure_fraction):>10}"
            f"{metrics.winners + metrics.losers:>8}{_pct(metrics.maximum_drawdown):>9}"
        )

    # How many sessions SHOULD be invested: SPY above its own 200-day average.
    above = 0
    eligible = 0
    for index in range(len(spy)):
        average = sma(spy[: index + 1], config.trend_period)
        if average is None:
            continue
        eligible += 1
        if spy[index].close > average:
            above += 1

    print()
    print(f"sessions past warmup            {eligible}")
    print(f"SPY above its 200-day average   {above} ({above / eligible * 100:.2f}%)")
    print(f"benchmark exposure              {_pct(benchmark.metrics.exposure_fraction)}")
    print(f"engine exposure                 {_pct(configured.metrics.exposure_fraction)}")

    # `exposure_fraction` is CAPITAL exposure -- mean of gross_exposure/equity -- not time in
    # market. Those are different quantities and conflating them is what this measures.
    def held_sessions(result: object) -> int:
        return sum(1 for point in result.equity_curve if point.gross_exposure > 0)  # type: ignore[attr-defined]

    def average_weight_when_held(result: object) -> Decimal:
        held = [
            point.gross_exposure / point.equity
            for point in result.equity_curve  # type: ignore[attr-defined]
            if point.gross_exposure > 0 and point.equity > 0
        ]
        return sum(held, Decimal(0)) / Decimal(len(held)) if held else Decimal(0)

    print()
    print(f"{'':28}{'time in market':>16}{'avg weight held':>18}{'capital exposure':>18}")
    print("-" * 80)
    for label, result in (
        ("SPY_SMA200 benchmark", benchmark),
        ("engine, SPY + trend only", configured),
    ):
        held = held_sessions(result)
        print(
            f"{label:28}{held / len(result.equity_curve) * 100:>15.2f}%"
            f"{float(average_weight_when_held(result)) * 100:>17.2f}%"
            f"{_pct(result.metrics.exposure_fraction):>18}"
        )

    print()
    print(f"max_position_value_bps = {config.max_position_value_bps} "
          f"({config.max_position_value_bps / 100:.0f}% cap per position)")

    high = None
    halted = 0
    for point in configured.equity_curve:
        state = calculate_drawdown(point.equity, high, config)
        high = state.high_water_equity
        if state.entry_halted:
            halted += 1
    print(f"engine sessions with entry halted   {halted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
