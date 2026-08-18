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

    results = {variant: engine.run({"SPY": bars}, variant) for variant in BenchmarkVariant}

    assert set(results) == set(BenchmarkVariant)
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


def _manifest(**overrides: object) -> ExperimentManifest:
    fields: dict[str, object] = {
        "experiment_id": "experiment-1",
        "git_commit": "abc123",
        "data_hash": "data-hash",
        "configuration_hash": "config-hash",
        "costs": {"slippage_bps": "5", "commission_per_order": "0"},
        "periods": (ExperimentPeriod(name="train", start="2020-01-01", end="2022-12-31"),),
        "walk_forward_splits": (
            ExperimentPeriod(name="walk-1", start="2020-01-01", end="2020-12-31"),
        ),
        "holdout": ExperimentPeriod(name="holdout", start="2023-01-01", end="2024-12-31"),
        "neighborhood_grid": {"trend_period": (190, 200, 210)},
        "ablations": ("without_market_regime",),
        "results": {"FULL_STRATEGY": {"total_return": "0.12"}},
    }
    fields.update(overrides)
    return ExperimentManifest(**fields)


def test_an_experiment_is_exploratory_until_it_names_a_registration() -> None:
    """The default is the weaker claim, so a forgotten field cannot pass as evidence (#5)."""
    assert _manifest().mode == "EXPLORATORY"


def test_a_confirmatory_experiment_must_name_the_registration_it_was_frozen_against() -> None:
    with pytest.raises(ValueError, match="registration hash"):
        _manifest(mode="CONFIRMATORY")

    with pytest.raises(ValueError, match="SHA-256"):
        _manifest(
            mode="CONFIRMATORY",
            hypothesis_id="H-2026-001",
            hypothesis_version=1,
            registration_hash="not-a-digest",
        )

    confirmatory = _manifest(
        mode="CONFIRMATORY",
        hypothesis_id="H-2026-001",
        hypothesis_version=1,
        registration_hash="a" * 64,
    )
    assert confirmatory.registration_hash == "a" * 64


def test_an_exploratory_experiment_cannot_borrow_a_registration() -> None:
    with pytest.raises(ValueError, match="cannot claim a registration"):
        _manifest(hypothesis_id="H-2026-001", hypothesis_version=1, registration_hash="a" * 64)
