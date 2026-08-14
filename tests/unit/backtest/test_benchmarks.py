from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from quantbot.backtest import (
    BenchmarkVariant,
    equal_weight_targets,
    rank_assets,
    spy_target,
)
from quantbot.domain import Bar
from quantbot.strategy import StrategyConfig, load_strategy_config

START = datetime(2025, 1, 2, 21, 0, tzinfo=UTC)


def _config() -> StrategyConfig:
    repo_root = Path(__file__).resolve().parents[3]
    return load_strategy_config(repo_root / "config" / "strategy-v1.yaml").model_copy(
        update={
            "universe": ("SPY", "QQQ"),
            "roster_size": 1,
            "momentum_long": 2,
            "momentum_skip": 0,
            "trend_period": 2,
            "entry_period": 2,
            "exit_period": 1,
            "atr_period": 2,
        }
    )


def _history(symbol: str, closes: tuple[str, ...]) -> tuple[Bar, ...]:
    bars: list[Bar] = []
    for index, close_text in enumerate(closes):
        close = Decimal(close_text)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=START + timedelta(days=index),
                open=close,
                high=close + Decimal("0.5"),
                low=close - Decimal("0.5"),
                close=close,
                volume=Decimal("1000"),
                adjustment=Decimal("1"),
            )
        )
    return tuple(bars)


def test_pure_momentum_and_trend_ranking_share_indicator_implementations() -> None:
    config = _config()
    histories = {
        "SPY": _history("SPY", ("100", "101", "102")),
        "QQQ": _history("QQQ", ("100", "105", "120")),
    }

    pure = rank_assets(
        histories,
        config,
        require_trend=False,
        require_positive=False,
        as_of=START + timedelta(days=2),
    )
    trend = rank_assets(
        histories,
        config,
        require_trend=True,
        require_positive=False,
        as_of=START + timedelta(days=2),
    )

    assert tuple(item.symbol for item in pure) == ("QQQ", "SPY")
    assert tuple(item.symbol for item in trend) == ("QQQ", "SPY")
    assert equal_weight_targets(pure, config) == {"QQQ": Decimal("0.10")}


def test_stale_asset_is_ineligible_instead_of_forward_filled() -> None:
    config = _config()
    histories = {
        "SPY": _history("SPY", ("100", "101", "102")),
        "QQQ": _history("QQQ", ("100", "105")),
    }

    rankings = rank_assets(
        histories,
        config,
        require_trend=False,
        require_positive=False,
        as_of=START + timedelta(days=2),
    )

    assert tuple(item.symbol for item in rankings) == ("SPY",)


def test_spy_sma_and_donchian_signals_use_strict_inequalities() -> None:
    config = _config()
    rising = _history("SPY", ("10", "11", "13"))
    equality = tuple(
        bar.model_copy(update={"close": Decimal("11.5"), "high": Decimal("12")})
        if index == 2
        else bar
        for index, bar in enumerate(_history("SPY", ("10", "11", "12")))
    )

    assert spy_target(
        BenchmarkVariant.SPY_SMA200,
        rising,
        config,
        currently_held=False,
    ) == {"SPY": Decimal("1")}
    assert spy_target(
        BenchmarkVariant.SPY_DONCHIAN,
        rising,
        config,
        currently_held=False,
    ) == {"SPY": Decimal("1")}
    assert (
        spy_target(
            BenchmarkVariant.SPY_DONCHIAN,
            equality,
            config,
            currently_held=False,
        )
        == {}
    )
