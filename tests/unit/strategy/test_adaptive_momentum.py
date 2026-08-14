from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
    XNYSSession,
    XNYSSessionSequence,
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
ROSTER_EXPIRES = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
GOLDEN_CONFIGURATION_HASH = "7d04bc9cc0cb20e6d879831a5ba48f152e8ab23b93f7a4ce94f3c0d7114981f1"

XNYS_DATES = (
    date(2026, 8, 28),
    date(2026, 8, 31),
    date(2026, 9, 1),
    date(2026, 9, 2),
    date(2026, 9, 3),
    date(2026, 9, 4),
    date(2026, 9, 8),
    date(2026, 9, 9),
    date(2026, 9, 10),
    date(2026, 9, 11),
    date(2026, 9, 14),
    date(2026, 9, 15),
    date(2026, 9, 16),
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 21),
    date(2026, 9, 22),
    date(2026, 9, 23),
    date(2026, 9, 24),
    date(2026, 9, 25),
    date(2026, 9, 28),
    date(2026, 9, 29),
    date(2026, 9, 30),
    date(2026, 10, 1),
)


def session_for(session_date: date) -> XNYSSession:
    return XNYSSession(
        session_date=session_date,
        open_at=datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=13, minutes=30),
        close_at=datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
        + timedelta(hours=20),
    )


XNYS_SESSIONS = XNYSSessionSequence(
    calendar="XNYS",
    sessions=tuple(session_for(session_date) for session_date in XNYS_DATES),
)


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


def roster_for(
    symbols: tuple[str, ...],
    *,
    effective_at: datetime = NEXT_SESSION,
    expires_at: datetime = ROSTER_EXPIRES,
) -> MonthlyRoster:
    return MonthlyRoster(
        calendar="XNYS",
        evaluated_at=EVALUATION,
        effective_at=effective_at,
        expires_at=expires_at,
        symbols=symbols,
        rankings=(),
    )


def complete_histories(
    config: StrategyConfig,
    overrides: dict[str, list[Bar]] | None = None,
) -> dict[str, list[Bar]]:
    histories = {symbol: make_history(symbol, step=Decimal("0")) for symbol in config.universe}
    histories.update(overrides or {})
    return histories


def identity_for(config: StrategyConfig) -> StrategyIdentity:
    return build_strategy_identity(
        config,
        git_commit="52128ce116af5c0e8376955535033238afa7925c",
        deployment_timestamp=datetime(2026, 8, 13, 15, tzinfo=UTC),
    )


def history_from_closes(symbol: str, closes: list[Decimal]) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=EVALUATION - timedelta(days=len(closes) - index - 1),
            open=close,
            high=close + Decimal("0.5"),
            low=close - Decimal("1"),
            close=close,
            volume=1000 + index,
            adjustment=1,
        )
        for index, close in enumerate(closes)
    ]


def constant_segments(
    symbol: str,
    old_close: Decimal,
    recent_close: Decimal,
    current_close: Decimal,
) -> list[Bar]:
    closes = [old_close] * 232 + [recent_close] * 20 + [current_close]
    return history_from_closes(symbol, closes)


def test_versioned_yaml_loads_all_proposed_defaults_and_is_frozen() -> None:
    config = load_strategy_config(CONFIG_PATH)

    assert config.strategy_name == "adaptive-momentum"
    assert config.version == "1.0.0"
    assert config.calendar == "XNYS"
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
    assert config.risk_per_trade_bps == 50
    assert config.max_open_risk_bps == 500
    assert config.max_position_value_bps == 1000
    assert config.max_gross_exposure_bps == 10000
    assert config.max_positions == 20
    assert config.drawdown_thresholds_bps == (500, 1000, 1500, 2000)
    assert config.drawdown_multipliers_bps == (10000, 7500, 5000, 0, 0)
    assert config.slippage_bps == 5
    assert config.commission_per_order == Decimal("0")
    assert config.idle_cash_rate_bps == 0
    assert config.allow_fractional_shares is False
    assert config.allow_pyramiding is False

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
            "universe": tuple("spy" if item == "SPY" else item for item in DEFAULT_UNIVERSE),
        },
        {**payload, "universe": tuple(item for item in DEFAULT_UNIVERSE if item != "SPY")},
        {**payload, "roster_size": len(DEFAULT_UNIVERSE) + 1},
        {**payload, "momentum_skip": payload["momentum_long"]},
        {**payload, "entry_period": payload["exit_period"]},
        {**payload, "trailing_stop_atr": Decimal("1")},
        {**payload, "risk_per_trade_bps": 0},
        {**payload, "max_open_risk_bps": 49},
        {**payload, "max_position_value_bps": 10001},
        {**payload, "drawdown_thresholds_bps": (500, 500, 1500, 2000)},
        {**payload, "drawdown_multipliers_bps": (10000, 7500, 8000, 2500, 0)},
        {**payload, "commission_per_order": Decimal("-0.01")},
    )
    for invalid in invalid_payloads:
        with pytest.raises(ValidationError):
            StrategyConfig.model_validate(invalid)


def test_numeric_parameters_remain_configurable_and_identity_bound() -> None:
    baseline = load_strategy_config(CONFIG_PATH)
    changed = strategy_config(
        roster_size=9,
        momentum_long=260,
        risk_per_trade_bps=60,
        slippage_bps=7,
    )

    assert changed.roster_size == 9
    assert changed.momentum_long == 260
    assert changed.risk_per_trade_bps == 60
    assert changed.slippage_bps == 7
    assert configuration_hash(changed) != configuration_hash(baseline)
    assert identity_for(changed).strategy_id != identity_for(baseline).strategy_id


def test_yaml_loader_uses_safe_loading_and_rejects_object_tags(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe.yaml"
    unsafe.write_text("!!python/object/apply:os.system ['echo unsafe']\n", encoding="ascii")

    with pytest.raises(ConstructorError):
        load_strategy_config(unsafe)

    assert yaml.safe_load("value: 1") == {"value": 1}


def test_identity_uses_golden_canonical_configuration_and_changes_with_config() -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = identity_for(config)

    assert canonical_configuration(config).startswith('{"allow_fractional_shares":false,')
    assert " " not in canonical_configuration(config)
    assert configuration_hash(config) == GOLDEN_CONFIGURATION_HASH
    assert identity.configuration_hash == GOLDEN_CONFIGURATION_HASH
    assert identity.strategy_id == "adaptive-momentum-v1-7d04bc9cc0cb20e6"
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
    candidate_histories = {
        symbol: make_history(symbol, step=Decimal(index) / Decimal("10"))
        for index, symbol in enumerate(candidates, start=1)
    }
    candidate_histories["QQQ"] = make_history("QQQ", step=Decimal("2"))
    candidate_histories["IWM"] = make_history("IWM", step=Decimal("2"))
    histories = complete_histories(config, candidate_histories)

    roster = build_monthly_roster(
        histories,
        evaluation_at=EVALUATION,
        effective_at=NEXT_SESSION,
        config=config,
        session_sequence=XNYS_SESSIONS,
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
        complete_histories(
            config,
            {
                "QQQ": make_history("QQQ", step=Decimal("1")),
                "IWM": make_history("IWM", step=Decimal("0")),
                "MDY": make_history("MDY", start=Decimal("400"), step=Decimal("-1")),
            },
        ),
        EVALUATION,
        NEXT_SESSION,
        config,
        session_sequence=XNYS_SESSIONS,
    )

    assert roster.symbols == ("QQQ",)


def test_monthly_roster_validates_cutoffs_and_unknown_symbols() -> None:
    config = load_strategy_config(CONFIG_PATH)

    with pytest.raises(ValueError, match="effective_at"):
        build_monthly_roster(
            complete_histories(config),
            EVALUATION,
            EVALUATION,
            config,
            session_sequence=XNYS_SESSIONS,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        build_monthly_roster(
            complete_histories(config),
            datetime(2026, 8, 31),
            NEXT_SESSION,
            config,
            session_sequence=XNYS_SESSIONS,
        )
    with pytest.raises(ValueError, match="exactly match"):
        build_monthly_roster(
            {"AAPL": make_history("AAPL")},
            EVALUATION,
            NEXT_SESSION,
            config,
            session_sequence=XNYS_SESSIONS,
        )


def test_monthly_roster_rejects_nonmonotonic_history_before_cutoff() -> None:
    config = load_strategy_config(CONFIG_PATH)
    history = make_history("QQQ")

    with pytest.raises(ValueError, match="strictly increasing"):
        build_monthly_roster(
            complete_histories(
                config,
                {"QQQ": [history[1], history[0], *history[2:]]},
            ),
            EVALUATION,
            NEXT_SESSION,
            config,
            session_sequence=XNYS_SESSIONS,
        )


def test_monthly_roster_rejects_partial_stale_and_midmonth_inputs() -> None:
    config = load_strategy_config(CONFIG_PATH)
    partial = complete_histories(config)
    partial.pop("HYG")
    stale = complete_histories(
        config,
        {"HYG": make_history("HYG", end=EVALUATION - timedelta(days=1))},
    )

    with pytest.raises(ValueError, match="exactly match"):
        build_monthly_roster(
            partial,
            EVALUATION,
            NEXT_SESSION,
            config,
            session_sequence=XNYS_SESSIONS,
        )
    with pytest.raises(ValueError, match="fresh through evaluation_at"):
        build_monthly_roster(
            stale,
            EVALUATION,
            NEXT_SESSION,
            config,
            session_sequence=XNYS_SESSIONS,
        )

    midmonth_evaluation = datetime(2026, 9, 15, 20, tzinfo=UTC)
    midmonth_next = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="final XNYS session"):
        build_monthly_roster(
            {symbol: make_history(symbol, end=midmonth_evaluation) for symbol in config.universe},
            midmonth_evaluation,
            midmonth_next,
            config,
            session_sequence=XNYS_SESSIONS,
        )


def test_monthly_roster_rejects_non_xnys_session_context() -> None:
    with pytest.raises(ValidationError, match="XNYS"):
        XNYSSessionSequence.model_validate(
            {
                "calendar": "XNAS",
                "sessions": [session.model_dump() for session in XNYS_SESSIONS.sessions],
            }
        )


def test_xnys_session_sequence_rejects_unsorted_duplicate_and_bad_boundaries() -> None:
    august_session = session_for(date(2026, 8, 31))
    september_session = session_for(date(2026, 9, 1))

    for sessions in (
        (september_session, august_session),
        (august_session, august_session),
    ):
        with pytest.raises(ValidationError, match="strictly increasing"):
            XNYSSessionSequence(calendar="XNYS", sessions=sessions)

    with pytest.raises(ValidationError, match="session_date"):
        XNYSSession(
            session_date=date(2026, 8, 31),
            open_at=datetime(2026, 9, 1, 13, 30, tzinfo=UTC),
            close_at=datetime(2026, 9, 1, 20, tzinfo=UTC),
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
        session_sequence=XNYS_SESSIONS,
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
    assert decision.asset_close == asset[-1].close
    assert decision.spy_close == spy[-1].close
    assert decision.spy_sma200 is not None
    assert decision.high_water_since_entry is None
    assert decision.prior_active_stop is None


def test_month_end_roster_is_effective_for_next_session_entry_and_held_exit() -> None:
    config = load_strategy_config(CONFIG_PATH)
    asset = make_history("QQQ")
    histories = complete_histories(config, {"QQQ": asset})
    roster = build_monthly_roster(
        histories,
        EVALUATION,
        NEXT_SESSION,
        config,
        session_sequence=XNYS_SESSIONS,
    )

    entry = evaluate_symbol(
        asset,
        make_history("SPY"),
        roster,
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        session_sequence=XNYS_SESSIONS,
    )
    held_exit = evaluate_symbol(
        make_history("HYG"),
        make_history("SPY"),
        roster,
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        PositionContext(
            symbol="HYG",
            entered_at=EVALUATION,
            initial_stop=Decimal("1"),
            active_stop=Decimal("1"),
        ),
        session_sequence=XNYS_SESSIONS,
    )

    assert roster.effective_at == NEXT_SESSION
    assert roster.expires_at == ROSTER_EXPIRES
    assert entry.active_roster is True
    assert entry.action is SignalAction.ENTER
    assert held_exit.action is SignalAction.EXIT
    assert held_exit.reasons == ("ROSTER_EXIT",)


def test_evaluation_rejects_stale_future_and_arbitrary_roster_windows() -> None:
    config = load_strategy_config(CONFIG_PATH)
    asset = make_history("QQQ")
    spy = make_history("SPY")
    stale = MonthlyRoster(
        calendar="XNYS",
        evaluated_at=datetime(2026, 7, 31, 20, tzinfo=UTC),
        effective_at=datetime(2026, 8, 28, 13, 30, tzinfo=UTC),
        expires_at=NEXT_SESSION,
        symbols=("QQQ",),
        rankings=(),
    )
    future = roster_for(
        ("QQQ",),
        effective_at=datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
    )
    arbitrary = roster_for(
        ("QQQ",),
        expires_at=datetime(2026, 9, 30, 13, 30, tzinfo=UTC),
    )

    for roster in (stale, future, arbitrary):
        with pytest.raises(ValueError, match="roster.*next_session_at|roster schedule"):
            evaluate_symbol(
                asset,
                spy,
                roster,
                EVALUATION,
                NEXT_SESSION,
                config,
                identity_for(config),
                session_sequence=XNYS_SESSIONS,
            )


def test_evaluation_rejects_non_session_transition() -> None:
    config = load_strategy_config(CONFIG_PATH)
    with pytest.raises(ValueError, match="immediate next XNYS session"):
        evaluate_symbol(
            make_history("QQQ"),
            make_history("SPY"),
            roster_for(("QQQ",)),
            EVALUATION,
            datetime(2026, 9, 2, 13, 30, tzinfo=UTC),
            config,
            identity_for(config),
            session_sequence=XNYS_SESSIONS,
        )


@pytest.mark.parametrize(
    "identity_update",
    [
        {"configuration_hash": "0" * 64},
        {"version": "9.9.9"},
        {"strategy_id": "adaptive-momentum-v9-invalid"},
    ],
    ids=["configuration-hash", "version", "strategy-id"],
)
def test_evaluation_rejects_identity_not_bound_to_config(
    identity_update: dict[str, str],
) -> None:
    config = load_strategy_config(CONFIG_PATH)
    mismatched = identity_for(config).model_copy(update=identity_update)

    with pytest.raises(ValueError, match="strategy identity does not match configuration"):
        evaluate_symbol(
            make_history("QQQ"),
            make_history("SPY"),
            roster_for(("QQQ",)),
            EVALUATION,
            NEXT_SESSION,
            config,
            mismatched,
            session_sequence=XNYS_SESSIONS,
        )


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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
    )
    warmup_decision = evaluate_symbol(
        make_history("QQQ", count=252),
        make_history("SPY"),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity,
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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
        session_sequence=XNYS_SESSIONS,
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


@pytest.mark.parametrize("count", [200, 252])
def test_position_trend_exit_does_not_require_entry_momentum(count: int) -> None:
    config = load_strategy_config(CONFIG_PATH)
    asset = make_history("QQQ", count=count, step=Decimal("0"))
    decision = evaluate_symbol(
        asset,
        make_history("SPY", count=count),
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
        session_sequence=XNYS_SESSIONS,
    )

    assert decision.action is SignalAction.EXIT
    assert decision.reasons == ("TREND_EXIT",)
    assert decision.momentum is None


def test_position_donchian_and_trailing_exits_work_at_200_bars() -> None:
    config = load_strategy_config(CONFIG_PATH)
    donchian_asset = history_from_closes(
        "QQQ",
        [Decimal("80")] * 179 + [Decimal("100")] * 20 + [Decimal("98.5")],
    )
    trending = make_history("QQQ", count=200)
    common = (
        make_history("SPY", count=200),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
    )

    donchian = evaluate_symbol(
        donchian_asset,
        *common,
        PositionContext(
            symbol="QQQ",
            entered_at=EVALUATION,
            initial_stop=Decimal("1"),
            active_stop=Decimal("1"),
        ),
        session_sequence=XNYS_SESSIONS,
    )
    trailing = evaluate_symbol(
        trending,
        *common,
        PositionContext(
            symbol="QQQ",
            entered_at=EVALUATION,
            initial_stop=Decimal("1"),
            active_stop=trending[-1].close,
        ),
        session_sequence=XNYS_SESSIONS,
    )

    assert donchian.action is SignalAction.EXIT
    assert donchian.reasons == ("DONCHIAN_EXIT",)
    assert donchian.momentum is None
    assert trailing.action is SignalAction.EXIT
    assert trailing.reasons == ("TRAILING_STOP_EXIT",)
    assert trailing.momentum is None


def test_position_without_momentum_holds_when_no_exit_rule_fires() -> None:
    config = load_strategy_config(CONFIG_PATH)
    asset = make_history("QQQ", count=252)
    position = PositionContext(
        symbol="QQQ",
        entered_at=EVALUATION,
        initial_stop=Decimal("1"),
        active_stop=Decimal("1"),
    )
    decision = evaluate_symbol(
        asset,
        make_history("SPY", count=252),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        position,
        session_sequence=XNYS_SESSIONS,
    )

    assert decision.action is SignalAction.HOLD
    assert decision.reasons == ("POSITION_HELD",)
    assert decision.momentum is None
    assert decision.asset_close == asset[-1].close
    assert decision.spy_close is not None
    assert decision.spy_sma200 is not None
    assert decision.high_water_since_entry == asset[-1].high
    assert decision.prior_active_stop == position.active_stop


def test_position_fail_closed_does_not_suppress_available_exit_predicates() -> None:
    config = load_strategy_config(CONFIG_PATH)
    incomplete = make_history("QQQ", count=199)
    available_donchian = history_from_closes(
        "QQQ",
        [Decimal("100")] * 20 + [Decimal("98")],
    )
    position = PositionContext(
        symbol="QQQ",
        entered_at=EVALUATION,
        initial_stop=Decimal("1"),
        active_stop=Decimal("1"),
    )

    unavailable = evaluate_symbol(
        incomplete,
        make_history("SPY", count=200),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        position,
        session_sequence=XNYS_SESSIONS,
    )
    available = evaluate_symbol(
        available_donchian,
        make_history("SPY", count=200),
        roster_for(("QQQ",)),
        EVALUATION,
        NEXT_SESSION,
        config,
        identity_for(config),
        position,
        session_sequence=XNYS_SESSIONS,
    )

    assert unavailable.action is SignalAction.INELIGIBLE
    assert unavailable.reasons == ("EXIT_WARMUP_INCOMPLETE",)
    assert available.action is SignalAction.EXIT
    assert available.reasons == ("DONCHIAN_EXIT", "EXIT_WARMUP_INCOMPLETE")


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
        session_sequence=XNYS_SESSIONS,
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
            session_sequence=XNYS_SESSIONS,
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
            session_sequence=XNYS_SESSIONS,
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
            session_sequence=XNYS_SESSIONS,
        )
