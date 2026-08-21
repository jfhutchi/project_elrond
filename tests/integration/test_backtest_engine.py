from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.backtest import (
    BacktestEngine,
    BacktestOrderPurpose,
    BenchmarkVariant,
    ComponentSwitches,
    ExperimentManifest,
    ExperimentPeriod,
    canonical_result_json,
    write_experiment_manifest,
)
from quantbot.backtest.engine import BacktestInputError
from quantbot.domain import Bar
from quantbot.strategy import StrategyConfig, load_strategy_config

START = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)


def _bar(day: int, *, open_price: str, close: str) -> Bar:
    open_value = Decimal(open_price)
    close_value = Decimal(close)
    return Bar(
        symbol="SPY",
        timestamp=START + timedelta(days=day),
        open=open_value,
        high=max(open_value, close_value) + Decimal("1"),
        low=min(open_value, close_value) - Decimal("1"),
        close=close_value,
        volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )


def _config(repo_root: Path) -> StrategyConfig:
    return load_strategy_config(repo_root / "config" / "strategy-v1.yaml").model_copy(
        update={"slippage_bps": 0, "commission_per_order": Decimal("0")}
    )


def test_buy_and_hold_enters_first_eligible_open_and_replays_byte_identically() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    engine = BacktestEngine(_config(repo_root), initial_cash=Decimal("1000"))
    histories = {
        "SPY": (
            _bar(0, open_price="99", close="100"),
            _bar(1, open_price="100", close="110"),
            _bar(2, open_price="111", close="105"),
        )
    }

    first = engine.run(histories, BenchmarkVariant.SPY_BUY_AND_HOLD)
    second = engine.run(histories, BenchmarkVariant.SPY_BUY_AND_HOLD)

    assert len(first.fills) == 1
    assert first.fills[0].occurred_at == histories["SPY"][0].timestamp
    assert first.fills[0].quantity == Decimal("10")
    assert first.initial_cash == Decimal("1000")
    assert first.final_equity == Decimal("1060")
    assert canonical_result_json(first) == canonical_result_json(second)


def test_all_required_benchmarks_and_explicit_ablation_switches_exist() -> None:
    assert tuple(variant.value for variant in BenchmarkVariant) == (
        "SPY_BUY_AND_HOLD",
        "SPY_SMA200",
        "SPY_DONCHIAN",
        "PURE_MOMENTUM_12_1",
        "MOMENTUM_TREND",
        "FULL_STRATEGY",
        # Not a comparator. Its targets come from the caller, so it has nothing of its own to
        # compare against the others -- it exists so a generated signal (#8) reaches this engine
        # rather than a second scoring path.
        "EXTERNAL_SIGNAL",
    )
    assert ComponentSwitches.full().model_dump() == {
        "momentum": True,
        "asset_trend": True,
        "market_regime": True,
        "donchian_entry": True,
        "donchian_exit": True,
        "atr_risk": True,
        "trailing_stop": True,
        "roster_exit": True,
    }


def test_sma_round_trip_accounts_for_cash_realized_pnl_and_commissions() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root).model_copy(
        update={"trend_period": 2, "commission_per_order": Decimal("1")}
    )
    histories = {
        "SPY": (
            _bar(0, open_price="10", close="10"),
            _bar(1, open_price="10", close="12"),
            _bar(2, open_price="13", close="13"),
            _bar(3, open_price="12", close="8"),
            _bar(4, open_price="7", close="7"),
        )
    }

    result = BacktestEngine(config, initial_cash=Decimal("100")).run(
        histories,
        BenchmarkVariant.SPY_SMA200,
    )

    assert result.positions == ()
    assert result.final_cash == Decimal("56")
    assert result.final_equity == Decimal("56")
    assert len(result.trades) == 1
    assert result.trades[0].gross_pnl == Decimal("-42")
    assert result.trades[0].net_pnl == Decimal("-44")
    assert result.metrics.total_costs == Decimal("2")
    assert result.cost_comparison.gross_final_equity == Decimal("58")
    assert result.cost_comparison.net_final_equity == Decimal("56")
    assert result.cost_comparison.gross_total_return == Decimal("-0.42")
    assert result.cost_comparison.net_total_return == Decimal("-0.44")


def test_every_benchmark_variant_runs_on_the_same_adjusted_window() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    bars = tuple(
        _bar(index, open_price=str(100 + index * 2), close=str(101 + index * 2))
        for index in range(300)
    )
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))

    # EXTERNAL_SIGNAL is excluded because it has no targets of its own: it is the seam through
    # which a caller supplies them. Excluded explicitly rather than by the loop quietly
    # succeeding on an empty run -- and the exclusion is not a hole, because the test below
    # asserts it refuses to run this way at all.
    comparators = [
        variant for variant in BenchmarkVariant if variant is not BenchmarkVariant.EXTERNAL_SIGNAL
    ]
    results = {variant: engine.run({"SPY": bars}, variant) for variant in comparators}

    assert set(results) == set(comparators)
    assert all(result.equity_curve for result in results.values())
    expected_input_hash = results[BenchmarkVariant.SPY_BUY_AND_HOLD].input_hash
    assert all(result.input_hash == expected_input_hash for result in results.values())
    assert all(result.fills for result in results.values())
    assert all(point.cash >= 0 for result in results.values() for point in result.equity_curve)
    assert results[BenchmarkVariant.SPY_BUY_AND_HOLD].component_switches == ComponentSwitches(
        momentum=False,
        asset_trend=False,
        market_regime=False,
        donchian_entry=False,
        donchian_exit=False,
        atr_risk=False,
        trailing_stop=False,
        roster_exit=False,
    )
    full = results[BenchmarkVariant.FULL_STRATEGY]
    first_entry = next(fill for fill in full.fills if fill.purpose is BacktestOrderPurpose.ENTRY)
    prior_equity = tuple(
        point for point in full.equity_curve if point.timestamp < first_entry.occurred_at
    )[-1].equity
    assert first_entry.gross_value <= prior_equity * Decimal("0.10")


def test_experiment_manifest_binds_research_inputs_without_mutating_config(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    before = (repo_root / "config" / "strategy-v1.yaml").read_bytes()
    manifest = ExperimentManifest(
        experiment_id="experiment-1",
        git_commit="abc123",
        data_hash="data-hash",
        configuration_hash="config-hash",
        costs={"slippage_bps": "5", "commission_per_order": "0"},
        periods=(ExperimentPeriod(name="train", start="2020-01-01", end="2022-12-31"),),
        walk_forward_splits=(
            ExperimentPeriod(name="walk-1", start="2020-01-01", end="2020-12-31"),
        ),
        holdout=ExperimentPeriod(name="holdout", start="2023-01-01", end="2024-12-31"),
        neighborhood_grid={"trend_period": (190, 200, 210)},
        ablations=("without_market_regime",),
        results={"FULL_STRATEGY": {"total_return": "0.12"}},
    )

    encoded_once = manifest.canonical_json()
    encoded_twice = manifest.canonical_json()
    destination = tmp_path / "experiment.json"

    assert encoded_once == encoded_twice
    assert write_experiment_manifest(destination, manifest) is True
    assert write_experiment_manifest(destination, manifest) is False
    conflicting = manifest.model_copy(
        update={"results": {"FULL_STRATEGY": {"total_return": "0.13"}}}
    )
    with pytest.raises(FileExistsError, match="different data"):
        write_experiment_manifest(destination, conflicting)
    assert destination.read_text(encoding="utf-8") == encoded_once + "\n"
    assert config.slippage_bps == 0
    assert (repo_root / "config" / "strategy-v1.yaml").read_bytes() == before


def test_external_weights_are_refused_by_every_variant_that_computes_its_own(
    tmp_path: Path,
) -> None:
    """The gate that makes this change safe for the live paper daemon (#8).

    Every call site that existed before `external_weights` passes none, so nothing changes for
    them. But the dangerous version of this feature is the one where a comparator *accepts*
    weights and silently uses them instead of its own targets -- a SPY_SMA200 result that is
    not SPY_SMA200, with nothing in the output saying so. Refused for every variant, asserted
    for every variant, so adding a comparator later cannot quietly open a hole.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    bars = tuple(
        _bar(index, open_price=str(100 + index * 2), close=str(101 + index * 2))
        for index in range(300)
    )
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))
    weights = {bars[10].timestamp.date(): {"SPY": Decimal("1")}}

    for variant in BenchmarkVariant:
        if variant is BenchmarkVariant.EXTERNAL_SIGNAL:
            continue
        with pytest.raises(BacktestInputError, match="only accepted by EXTERNAL_SIGNAL"):
            engine.run({"SPY": bars}, variant, external_weights=weights)


def test_the_external_variant_refuses_to_run_with_nothing_to_run(tmp_path: Path) -> None:
    """Simulating an empty strategy would produce a flat curve that looks like a result.

    A caller whose signal generation failed would get back a clean-looking zero-return backtest
    rather than an error, and "the strategy made nothing" and "the strategy never ran" are not
    the same finding.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    bars = tuple(_bar(index, open_price="100", close="101") for index in range(300))
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))

    with pytest.raises(BacktestInputError, match="requires external_weights"):
        engine.run({"SPY": bars}, BenchmarkVariant.EXTERNAL_SIGNAL)


def test_an_external_signal_is_executed_by_the_production_engine(tmp_path: Path) -> None:
    """#8's outstanding box: the generated signal reaches the engine that would actually trade.

    Not a reimplementation of the fill and cost model -- the same one, with the same look-ahead
    protection. The weights are applied through `_PendingTargets`, so they execute on the *next*
    session exactly as every other variant's do.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    bars = tuple(
        _bar(index, open_price=str(100 + index), close=str(101 + index)) for index in range(300)
    )
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))

    entry = bars[50].timestamp.date()
    exit_day = bars[200].timestamp.date()
    result = engine.run(
        {"SPY": bars},
        BenchmarkVariant.EXTERNAL_SIGNAL,
        external_weights={entry: {"SPY": Decimal("1")}, exit_day: {}},
    )

    assert result.variant is BenchmarkVariant.EXTERNAL_SIGNAL
    assert result.fills, "the signal should have traded"
    # It bought after the signal date, never on it: pending targets execute next session.
    first_fill = min(result.fills, key=lambda fill: fill.occurred_at)
    assert first_fill.occurred_at.date() > entry, "a signal must not fill on its own session"
    # Costs came from the production fill model, not from anything this test supplied.
    assert result.cost_comparison.net_final_equity == result.final_equity
    assert all(point.cash >= 0 for point in result.equity_curve)


def test_a_silent_day_is_not_an_instruction_to_go_flat(tmp_path: Path) -> None:
    """Absent means "no new instruction", not "liquidate".

    A signal that only names the days it changes would otherwise be sold out on every silent
    day, which is not what it said -- and the resulting turnover would be charged to it.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = _config(repo_root)
    bars = tuple(
        _bar(index, open_price=str(100 + index), close=str(101 + index)) for index in range(300)
    )
    engine = BacktestEngine(config, initial_cash=Decimal("100000"))

    result = engine.run(
        {"SPY": bars},
        BenchmarkVariant.EXTERNAL_SIGNAL,
        external_weights={bars[50].timestamp.date(): {"SPY": Decimal("1")}},
    )

    # One instruction, so one entry and no churn from the 249 sessions that said nothing.
    assert len(result.fills) == 1, [fill.occurred_at for fill in result.fills]
    assert result.positions, "the position should still be held at the end"
