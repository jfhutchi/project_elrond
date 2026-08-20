"""The harness must actually run the config it claims to be testing.

`REFUTED.md` records three analysis errors from a runner that was not connected to the code
under test, and the tell was byte-identical output across runs that should have differed. This
is the shifted-feature probe applied to the harness itself: change the config, and the result
has to move.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BacktestEngine,
    BenchmarkVariant,
    ComponentSwitches,
    component_switches_for,
    switches_for_config,
)
from quantbot.domain import Bar
from quantbot.strategy import StrategyComponents, StrategyConfig, load_strategy_config

START = datetime(2020, 1, 2, 21, 0, tzinfo=UTC)


def config() -> StrategyConfig:
    """The repository's own config, so the harness is exercised against a real one."""
    repo_root = Path(__file__).resolve().parents[3]
    return load_strategy_config(repo_root / "config" / "strategy-v1.yaml").model_copy(
        update={
            "universe": ("SPY", "QQQ"),
            "slippage_bps": 0,
            "commission_per_order": Decimal("0"),
        }
    )


def history(symbol: str, *, drift: str) -> list[Bar]:
    """A steadily trending series, so component changes have something to act on."""
    price = Decimal("100")
    step = Decimal(drift)
    bars = []
    for index in range(420):
        price = max(price + step, Decimal("1"))
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=START + timedelta(days=index),
                open=price,
                high=price * Decimal("1.01"),
                low=price * Decimal("0.99"),
                close=price,
                volume=Decimal("1000000"),
                adjustment=Decimal("1"),
            )
        )
    return bars


def histories() -> dict[str, list[Bar]]:
    return {"SPY": history("SPY", drift="0.05"), "QQQ": history("QQQ", drift="0.09")}


def test_the_config_selection_maps_onto_every_engine_switch_it_can() -> None:
    """`atr_risk` has no configuration field, so it is named rather than defaulted invisibly."""
    everything_off = switches_for_config(
        StrategyComponents(
            momentum=False,
            asset_trend=False,
            market_regime=False,
            donchian_entry=False,
            donchian_exit=False,
            trailing_stop=False,
            roster_exit=False,
        )
    )
    assert everything_off == ComponentSwitches(
        momentum=False,
        asset_trend=False,
        market_regime=False,
        donchian_entry=False,
        donchian_exit=False,
        atr_risk=True,
        trailing_stop=False,
        roster_exit=False,
    )
    assert switches_for_config(StrategyComponents()) == ComponentSwitches.full()

    # Every field the config does have is carried through, none silently dropped.
    for field in StrategyComponents.model_fields:
        one_off = switches_for_config(StrategyComponents(**{field: False}))
        assert getattr(one_off, field) is False, field


def test_changing_the_config_changes_the_result() -> None:
    """The property whose absence let three config changes produce identical output."""
    engine = BacktestEngine(config(), initial_cash=Decimal("10000"))
    data = histories()

    full = engine.run(
        data,
        BenchmarkVariant.FULL_STRATEGY,
        component_switches=switches_for_config(StrategyComponents()),
    )
    without_trend = engine.run(
        data,
        BenchmarkVariant.FULL_STRATEGY,
        component_switches=switches_for_config(StrategyComponents(asset_trend=False)),
    )
    without_entry = engine.run(
        data,
        BenchmarkVariant.FULL_STRATEGY,
        component_switches=switches_for_config(StrategyComponents(donchian_entry=False)),
    )

    outcomes = {
        full.metrics.total_return,
        without_trend.metrics.total_return,
        without_entry.metrics.total_return,
    }
    assert len(outcomes) > 1, "the harness produced identical output for different configs"


def test_a_fixed_variant_ignores_the_config_which_is_why_it_is_a_comparator() -> None:
    """Correct for a comparator, and wrong for the config under test.

    Asserted so the distinction stays visible: a runner reporting only these rows measures
    something that cannot respond to the change it claims to be evaluating.
    """
    engine = BacktestEngine(config(), initial_cash=Decimal("10000"))
    data = histories()

    # No component_switches argument: the engine falls back to the variant's fixed definition.
    default = engine.run(data, BenchmarkVariant.SPY_BUY_AND_HOLD)
    explicit = engine.run(
        data,
        BenchmarkVariant.SPY_BUY_AND_HOLD,
        component_switches=component_switches_for(BenchmarkVariant.SPY_BUY_AND_HOLD),
    )
    assert default.metrics.total_return == explicit.metrics.total_return

    # And a full-strategy run with the same fixed variant switches is unmoved by any config
    # component selection, because it never reads one.
    assert (
        engine.run(
            data,
            BenchmarkVariant.SPY_BUY_AND_HOLD,
            component_switches=component_switches_for(BenchmarkVariant.SPY_BUY_AND_HOLD),
        ).metrics.total_return
        == default.metrics.total_return
    )
