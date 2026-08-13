from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError

from quantbot.domain import Bar
from quantbot.domain.models import StrategyIdentity
from quantbot.strategy.adaptive_momentum import (
    MonthlyRoster,
    PositionContext,
    SignalAction,
    build_monthly_roster,
    evaluate_symbol,
)
from quantbot.strategy.config import DEFAULT_UNIVERSE, StrategyConfig, load_strategy_config
from quantbot.strategy.identity import (
    bar_set_hash,
    build_strategy_identity,
    canonical_configuration,
    configuration_hash,
)
from quantbot.strategy.indicators import donchian_entry_level, wilder_atr

ROOT = Path(__file__).parents[3]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"
EVALUATION = datetime(2026, 8, 31, 20, tzinfo=UTC)
NEXT_SESSION = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
GOLDEN_CONFIGURATION_HASH = "5224ee716e702594ae59ca091708de1ba6524bd780cc4934e4985a694e929b44"


def strategy_config(**updates: Any) -> StrategyConfig:
    payload = load_strategy_config(CONFIG_PATH).model_dump(mode="python")
    payload.update(updates)
    return StrategyConfig.model_validate(payload)


def make_history(
    symbol: str,
    *,
    count: int = 253,
    start: Decimal = Decimal("100"),
    step: Decimal = Decimal("1"),
    end: datetime = EVALUATION,
) -> list[Bar]:
    result: list[Bar] = []
    for index in range(count):
        close = start + step * index
        result.append(
            Bar(
                symbol=symbol,
                timestamp=end - timedelta(days=count - index - 1),
                open=close,
                high=close + Decimal("0.25"),
                low=close - Decimal("0.25"),
                close=close,
                volume=Decimal(1000 + index),
                adjustment=1,
            )
        )
    return result


def replace_last(history: list[Bar], close: Decimal) -> list[Bar]:
    previous = history[-1]
    replacement = Bar(
        symbol=previous.symbol,
        timestamp=previous.timestamp,
        open=close,
        high=close + Decimal("0.25"),
        low=close - Decimal("0.25"),
        close=close,
        volume=previous.volume,
        adjustment=previous.adjustment,
    )
    return [*history[:-1], replacement]


def roster_for(symbols: tuple[str, ...], *, effective_at: datetime = EVALUATION) -> MonthlyRoster:
    return MonthlyRoster(
        evaluated_at=effective_at - timedelta(days=1),
        effective_at=effective_at,
        symbols=symbols,
        rankings=(),
    )


def identity_for(config: StrategyConfig) -> StrategyIdentity:
    return build_strategy_identity(
        config,
        git_commit="52128ce116af5c0e8376955535033238afa7925c",
        deployment_timestamp=datetime(2026, 8, 13, 15, tzinfo=UTC),
    )


def constant_segments(
    symbol: str,
    old_close: Decimal,
    recent_close: Decimal,
    current_close: Decimal,
) -> list[Bar]:
    closes = [old_close] * 232 + [recent_close] * 20 + [current_close]
    bars: list[Bar] = []
    for index, close in enumerate(closes):
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=EVALUATION - timedelta(days=252 - index),
                open=close,
                high=close + Decimal("0.5"),
                low=close - Decimal("1"),
                close=close,
                volume=1000 + index,
                adjustment=1,
            )
        )
    return bars


def test_versioned_yaml_loads_all_proposed_defaults_and_is_frozen() -> None:
    config = load_strategy_config(CONFIG_PATH)

    assert config.strategy_name == "adaptive-momentum"
    assert config.version == "1.0.0"
    assert config.universe == DEFAULT_UNIVERSE
    assert config.benchmark_symbol == config.regime_symbol == "SPY"
    assert config.roster_size == 10
    assert (config.momentum_long, config.momentum_skip) == (252, 21)
    assert (config.trend_period, config.entry_period, config.exit_period) == (200, 55, 20)
    assert config.atr_period == 20
    assert config.initial_stop_atr == Decimal("2")
    assert config.trailing_stop_atr == Decimal("3")
    assert config.rotation_frequency == "monthly"
    assert config.positive_momentum_required is True
    assert config.market_regime_blocks_entries_only is True

    with pytest.raises(ValidationError):
        config.roster_size = 9  # type: ignore[misc]


def test_config_rejects_missing_unknown_and_invalid_values() -> None:
    config = load_strategy_config(CONFIG_PATH)
    payload = config.model_dump(mode="python")

    missing = dict(payload)
    missing.pop("trend_period")
    with pytest.raises(ValidationError, match="trend_period"):
        StrategyConfig.model_validate(missing)

    with pytest.raises(ValidationError, match="extra_forbidden"):
        StrategyConfig.model_validate({**payload, "unknown": True})

    invalid_payloads = (
        {**payload, "universe": (*DEFAULT_UNIVERSE, "SPY")},
        {
            **payload,
            "universe": tuple(
                "spy" if item == "SPY" else item for item in DEFAULT_UNIVERSE
            ),
        },
        {**payload, "universe": tuple(item for item in DEFAULT_UNIVERSE if item != "SPY")},
        {**payload, "roster_size": len(DEFAULT_UNIVERSE) + 1},
        {**payload, "momentum_skip": payload["momentum_long"]},
        {**payload, "entry_period": payload["exit_period"]},
        {**payload, "trailing_stop_atr": Decimal("1")},
    )
    for invalid in invalid_payloads:
        with pytest.raises(ValidationError):
            StrategyConfig.model_validate(invalid)


def test_yaml_loader_uses_safe_loading_and_rejects_object_tags(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("!!python/object/apply:os.system ['echo unsafe']\n", encoding="ascii")

    with pytest.raises(ConstructorError):
        load_strategy_config(unsafe)

    assert yaml.safe_load("value: 1") == {"value": 1}


def test_identity_uses_golden_canonical_configuration_and_changes_with_config() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)

    assert canonical_configuration(config).startswith('{"atr_period":20,')
    assert " " not in canonical_configuration(config)
    assert configuration_hash(config) == GOLDEN_CONFIGURATION_HASH
    assert identity.configuration_hash == GOLDEN_CONFIGURATION_HASH
    assert identity.strategy_id == "adaptive-momentum-v1-5224ee716e702594"
    assert identity.version == "1.0.0"

    semantically_equal = StrategyConfig.model_validate(
        {**config.model_dump(mode="python"), "initial_stop_atr": Decimal("2.000")}
    )
    changed = strategy_config(roster_size=9)
    assert configuration_hash(semantically_equal) == GOLDEN_CONFIGURATION_HASH
    assert configuration_hash(changed) != GOLDEN_CONFIGURATION_HASH
    assert identity_for(changed).strategy_id != identity.strategy_id


def test_identity_rejects_naive_deployment_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_strategy_identity(
            load_strategy_config(CONFIG_PATH),
            git_commit="abc123",
            deployment_timestamp=datetime(2026, 8, 13),
        )


def test_monthly_roster_ranks_descending_with_symbol_tiebreak_and_limits_to_ten() -> None:
    config = load_strategy_config(CONFIG_PATH)
    candidates = config.universe[1:13]
    histories = {
        symbol: make_history(symbol, step=Decimal(index) / Decimal("10"))
        for index, symbol in enumerate(candidates, start=1)
    }
    histories["QQQ"] = make_history("QQQ", step=Decimal("2"))
    histories["IWM"] = make_history("IWM", step=Decimal("2"))

    roster = build_monthly_roster(
        histories,
        evaluation_at=EVALUATION,
        effective_at=NEXT_SESSION,
        config=config,
    )

    assert roster.evaluated_at == EVALUATION
    assert roster.effective_at == NEXT_SESSION
    assert len(roster.symbols) == len(roster.rankings) == 10
    assert roster.symbols[:2] == ("IWM", "QQQ")
    assert roster.symbols == tuple(item.symbol for item in roster.rankings)
    assert all(item.momentum > 0 and item.close > item.sma200 for item in roster.rankings)
    assert list(roster.rankings) == sorted(
        roster.rankings,
        key=lambda item: (-item.momentum, item.symbol),
    )


def test_monthly_roster_excludes_nonpositive_momentum_and_trend_equality() -> None:
    config = load_strategy_config(CONFIG_PATH)
    roster = build_monthly_roster(
        {
            "QQQ": make_history("QQQ", step=Decimal("1")),
            "IWM": make_history("IWM", step=Decimal("0")),
            "MDY": make_history("MDY", start=Decimal("400"), step=Decimal("-1")),
        },
        EVALUATION,
        NEXT_SESSION,
        config,
    )

    assert roster.symbols == ("QQQ",)


def test_monthly_roster_validates_cutoffs_and_unknown_symbols() -> None:
    config = load_strategy_config(CONFIG_PATH)

    with pytest.raises(ValueError, match="effective_at"):
        build_monthly_roster({}, EVALUATION, EVALUATION, config)
    with pytest.raises(ValueError, match="timezone-aware"):
        build_monthly_roster({}, datetime(2026, 8, 31), NEXT_SESSION, config)
    with pytest.raises(ValueError, match="not in strategy universe"):
        build_monthly_roster(
            {"AAPL": make_history("AAPL")},
            EVALUATION,
            NEXT_SESSION,
            config,
        )


def test_monthly_roster_rejects_nonmonotonic_history_before_cutoff() -> None:
    config = load_strategy_config(CONFIG_PATH)
    history = make_history("QQQ")

    with pytest.raises(ValueError, match="strictly increasing"):
        build_monthly_roster(
            {"QQQ": [history[1], history[0], *history[2:]]},
            EVALUATION,
            NEXT_SESSION,
            config,
        )


def test_entry_decision_populates_all_audit_fields_and_exact_stop_distance() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)
    asset = make_history("QQQ")
    spy = make_history("SPY")

    decision = evaluate_symbol(
        asset,
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
    )

    assert decision.symbol == "QQQ"
    assert decision.action is SignalAction.ENTER
    assert decision.reasons == ("ENTRY_BREAKOUT",)
    assert decision.evaluated_at == decision.input_cutoff == EVALUATION
    assert decision.eligible_from == NEXT_SESSION
    assert decision.regime_on is True
    assert decision.active_roster is True
    assert decision.strategy_id == identity.strategy_id
    assert decision.configuration_hash == identity.configuration_hash
    assert decision.bar_set_hash == bar_set_hash({"QQQ": asset, "SPY": spy}, EVALUATION)
    assert decision.momentum is not None and decision.momentum > 0
    assert decision.sma200 is not None and asset[-1].close > decision.sma200
    assert decision.entry_channel == donchian_entry_level(asset, config.entry_period)
    assert decision.exit_channel is not None
    assert decision.atr == wilder_atr(asset, config.atr_period)
    assert decision.atr is not None
    assert decision.initial_stop_distance == config.initial_stop_atr * decision.atr
    assert decision.trailing_stop is None


def test_entry_requires_strict_breakout_and_reports_every_failed_gate() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)
    spy = make_history("SPY")
    asset = make_history("QQQ")
    channel = donchian_entry_level(asset, config.entry_period)
    assert channel is not None

    equality = evaluate_symbol(
        replace_last(asset, channel),
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
    )
    assert equality.action is SignalAction.HOLD
    assert equality.reasons == ("ENTRY_CHANNEL_NOT_BROKEN",)

    all_off = evaluate_symbol(
        make_history("QQQ", step=Decimal("0")),
        make_history("SPY", step=Decimal("0")),
        roster_for(()),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
    )
    assert all_off.action is SignalAction.HOLD
    assert all_off.reasons == (
        "NOT_IN_ACTIVE_ROSTER",
        "REGIME_OFF",
        "TREND_FILTER_FAILED",
        "NON_POSITIVE_MOMENTUM",
        "ENTRY_CHANNEL_NOT_BROKEN",
    )


def test_missing_current_bar_and_indicator_warmup_are_ineligible() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)
    stale = make_history("QQQ", end=EVALUATION - timedelta(days=1))

    stale_decision = evaluate_symbol(
        stale,
        make_history("SPY"),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
    )
    warmup_decision = evaluate_symbol(
        make_history("QQQ", count=252),
        make_history("SPY"),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
    )

    assert stale_decision.action is SignalAction.INELIGIBLE
    assert stale_decision.reasons == ("STALE_OR_MISSING_BAR",)
    assert warmup_decision.action is SignalAction.INELIGIBLE
    assert warmup_decision.reasons == ("WARMUP_INCOMPLETE",)


def test_position_exits_for_roster_trend_donchian_and_trailing_rules() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)
    spy = make_history("SPY")
    trending = make_history("QQQ")
    normal_position = PositionContext(
        symbol="QQQ",
        entered_at=EVALUATION,
        initial_stop=Decimal("1"),
        active_stop=Decimal("1"),
    )

    roster_exit = evaluate_symbol(
        trending,
        spy,
        roster_for(()),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
        normal_position,
    )
    trend_exit = evaluate_symbol(
        make_history("QQQ", step=Decimal("0")),
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
        normal_position,
    )
    donchian_exit = evaluate_symbol(
        constant_segments("QQQ", Decimal("80"), Decimal("100"), Decimal("98.5")),
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
        normal_position,
    )
    trailing_position = PositionContext(
        symbol="QQQ",
        entered_at=EVALUATION,
        initial_stop=Decimal("1"),
        active_stop=trending[-1].close,
    )
    trailing_exit = evaluate_symbol(
        trending,
        spy,
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
        trailing_position,
    )

    assert roster_exit.reasons == ("ROSTER_EXIT",)
    assert trend_exit.reasons == ("TREND_EXIT",)
    assert donchian_exit.reasons == ("DONCHIAN_EXIT",)
    assert trailing_exit.reasons == ("TRAILING_STOP_EXIT",)
    assert all(
        item.action is SignalAction.EXIT
        for item in (roster_exit, trend_exit, donchian_exit, trailing_exit)
    )
    assert trailing_exit.trailing_stop == trending[-1].close


def test_position_combines_exit_reasons_in_stable_order() -> None:
    config = load_strategy_config(CONFIG_PATH)
    asset = constant_segments("QQQ", Decimal("102"), Decimal("102"), Decimal("100"))
    position = PositionContext(
        symbol="QQQ",
        entered_at=EVALUATION - timedelta(days=20),
        initial_stop=Decimal("1"),
        active_stop=Decimal("100"),
    )

    decision = evaluate_symbol(
        asset,
        make_history("SPY"),
        roster_for(()),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        position,
    )

    assert decision.action is SignalAction.EXIT
    assert decision.reasons == (
        "ROSTER_EXIT",
        "TREND_EXIT",
        "DONCHIAN_EXIT",
        "TRAILING_STOP_EXIT",
    )
    assert decision.trailing_stop is not None
    assert decision.trailing_stop >= position.initial_stop
    assert decision.trailing_stop >= position.active_stop


def test_regime_off_alone_does_not_force_position_exit() -> None:
    config = load_strategy_config(CONFIG_PATH)
    decision = evaluate_symbol(
        make_history("QQQ"),
        make_history("SPY", step=Decimal("0")),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        PositionContext(
            symbol="QQQ",
            entered_at=EVALUATION,
            initial_stop=Decimal("1"),
            active_stop=Decimal("1"),
        ),
    )

    assert decision.action is SignalAction.HOLD
    assert decision.reasons == ("POSITION_HELD",)
    assert decision.regime_on is False


def test_evaluation_validates_position_symbol_and_next_session() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)
    asset = make_history("QQQ")
    spy = make_history("SPY")

    with pytest.raises(ValueError, match="position symbol"):
        evaluate_symbol(
            asset,
            spy,
            roster_for(("QQQ",)),
            EVALUATION,
            NEXT_SESSION,
            config,
            identity,
            PositionContext(
                symbol="IWM",
                entered_at=EVALUATION,
                initial_stop=1,
                active_stop=1,
            ),
        )
    with pytest.raises(ValueError, match="next_session_at"):
        evaluate_symbol(
            asset,
            spy,
            roster_for(("QQQ",)),
            EVALUATION,
            EVALUATION,
            config,
            identity,
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        PositionContext(
            symbol="QQQ",
            entered_at=datetime(2026, 8, 1),
            initial_stop=1,
            active_stop=1,
        )

    with pytest.raises(ValueError, match="strictly increasing"):
        evaluate_symbol(
            [asset[1], asset[0], *asset[2:]],
            spy,
            roster_for(("QQQ",)),
            EVALUATION,
            NEXT_SESSION,
            config,
            identity,
        )
