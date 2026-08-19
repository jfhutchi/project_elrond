"""Compare the six required benchmark variants over the research history.

Reads the separate research database written by ``fetch_research_bars.py`` and runs
every variant through the same deterministic engine, with the strategy's configured
slippage and commissions applied.

Usage:
    uv run python scripts/run_research_backtest.py [DB_PATH] [INITIAL_CASH]
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BacktestEngine,
    BacktestResult,
    BenchmarkVariant,
    switches_for_config,
)
from quantbot.market_data import MarketDataCache
from quantbot.runtime import market_data_settings, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import configuration_hash, load_strategy_config

DEFAULT_DB = Path("research") / "bars.db"


def _percent(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value * Decimal('100'):.2f}%"


def _ratio(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _money(value: Decimal | None) -> str:
    return "n/a" if value is None else f"${value:.2f}"


def main() -> int:
    db_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DB
    initial_cash = Decimal(sys.argv[2]) if len(sys.argv) > 2 else Decimal("100")

    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    provider_key = MarketDataCache.provider_key(
        feed.provider, feed.feed, feed.adjustment, feed.timeframe
    )
    database = Database(db_path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        raw = {
            symbol: repository.list_bars(symbol=symbol, provider=provider_key)
            for symbol in config.universe
        }
    database.close()

    missing = sorted(symbol for symbol, bars in raw.items() if not bars)
    if missing:
        print(f"no research bars for: {', '.join(missing)}")
        return 1

    # Every symbol must share an identical session series: the engine requires a bar for
    # any held or ordered symbol, and a symbol-specific gap would silently skew ranking.
    common = set.intersection(*({bar.timestamp for bar in bars} for bars in raw.values()))
    histories = {
        symbol: [bar for bar in bars if bar.timestamp in common] for symbol, bars in raw.items()
    }
    sessions = sorted(common)
    dropped = sum(len(raw[s]) - len(histories[s]) for s in raw)
    print(f"strategy      {config.strategy_name} {config.version}")
    print(f"config hash   {configuration_hash(config)[:16]}")
    print(f"sessions      {len(sessions)}  {sessions[0].date()} .. {sessions[-1].date()}")
    print(f"warmup        {config.momentum_long + config.momentum_skip} sessions")
    print(f"initial cash  ${initial_cash}")
    print(f"dropped bars  {dropped} (outside the common session series)")
    print(f"costs         {config.slippage_bps}bps/side, ${config.commission_per_order}/order")
    print(f"components    {switches_for_config(config.components)}")
    print()

    engine = BacktestEngine(config, initial_cash=initial_cash)
    results: dict[str, BacktestResult] = {}
    for variant in BenchmarkVariant:
        # Fixed comparators. These deliberately ignore the config's component selection --
        # that is what makes them comparators.
        results[variant.value] = engine.run(histories, variant)

    # The config under test, which the loop above does NOT measure. Omitting this row is the
    # defect recorded in REFUTED.md: three config changes produced byte-identical output
    # because every row was a fixed variant and none reached the code being changed.
    results["CONFIGURED_STRATEGY"] = engine.run(
        histories,
        BenchmarkVariant.FULL_STRATEGY,
        component_switches=switches_for_config(config.components),
    )

    header = (
        f"{'variant':22}{'total':>9}{'CAGR':>8}{'maxDD':>8}{'Sharpe':>8}"
        f"{'Sortino':>9}{'PF':>7}{'win%':>7}{'trades':>8}{'expos':>8}{'costs':>10}"
    )
    print(header)
    print("-" * len(header))
    rows = []
    for name, result in results.items():
        m = result.metrics
        trades = m.winners + m.losers
        line = (
            f"{name:22}{_percent(m.total_return):>9}{_percent(m.cagr_equivalent):>8}"
            f"{_percent(m.maximum_drawdown):>8}{_ratio(m.sharpe):>8}{_ratio(m.sortino):>9}"
            f"{_ratio(m.profit_factor):>7}{_percent(m.win_rate):>7}{trades:>8}"
            f"{_percent(m.exposure_fraction):>8}{_money(m.total_costs):>10}"
        )
        print(line)
        rows.append((name, result))

    report = Path("reports") / "research" / "benchmark-comparison.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Benchmark Comparison",
        "",
        f"Generated at: {datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}",
        f"Strategy: {config.strategy_name} {config.version} "
        f"(config hash `{configuration_hash(config)[:16]}`)",
        f"Sessions: {len(sessions)} from {sessions[0].date()} to {sessions[-1].date()} "
        f"({config.momentum_long + config.momentum_skip} of them consumed by warmup)",
        f"Initial cash: ${initial_cash}",
        f"Costs applied: {config.slippage_bps}bps per side, "
        f"${config.commission_per_order} per order",
        "",
        "`CONFIGURED_STRATEGY` is the only row applying this config's component selection. "
        "Every other row is a fixed comparator and ignores it by design; a runner reporting "
        "only those rows is why three config changes once produced identical output.",
        "",
        "Data: Alpaca IEX daily bars, dividend and split adjusted. IEX is a partial-volume "
        "feed; daily OHLC on liquid ETFs tracks the consolidated tape closely but is not "
        "identical to SIP data.",
        "",
        "| Variant | Total | CAGR | Max DD | Sharpe | Sortino | Profit factor | Win rate |"
        " Trades | Exposure | Costs |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, result in rows:
        m = result.metrics
        lines.append(
            f"| {name} | {_percent(m.total_return)} | {_percent(m.cagr_equivalent)} "
            f"| {_percent(m.maximum_drawdown)} | {_ratio(m.sharpe)} | {_ratio(m.sortino)} "
            f"| {_ratio(m.profit_factor)} | {_percent(m.win_rate)} | {m.winners + m.losers} "
            f"| {_percent(m.exposure_fraction)} | {_money(m.total_costs)} |"
        )
    lines.append("")
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nwrote {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
