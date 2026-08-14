from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from quantbot.domain import Bar
from quantbot.strategy.adaptive_momentum import (
    MonthlyRoster,
    PositionContext,
    XNYSSession,
    XNYSSessionSequence,
    build_monthly_roster,
    evaluate_symbol,
)
from quantbot.strategy.config import StrategyConfig, load_strategy_config
from quantbot.strategy.identity import bar_set_hash, build_strategy_identity
from quantbot.strategy.indicators import donchian_entry_level, donchian_exit_level

ROOT = Path(__file__).parents[2]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"
CUTOFF = datetime(2026, 8, 31, 20, tzinfo=UTC)
NEXT_SESSION = datetime(2026, 9, 1, 13, 30, tzinfo=UTC)
ROSTER_EXPIRES = datetime(2026, 10, 1, 13, 30, tzinfo=UTC)
VALID_PRICE = st.decimals(
    min_value=Decimal("1"),
    max_value=Decimal("10000"),
    places=2,
    allow_nan=False,
    allow_infinity=False,
)


def xny_session(session_date: date) -> XNYSSession:
    midnight = datetime.combine(session_date, datetime.min.time(), tzinfo=UTC)
    return XNYSSession(
        session_date=session_date,
        open_at=midnight + timedelta(hours=13, minutes=30),
        close_at=midnight + timedelta(hours=20),
    )


XNYS_SESSIONS = XNYSSessionSequence(
    calendar="XNYS",
    sessions=tuple(
        xny_session(session_date)
        for session_date in (
            date(2026, 8, 31),
            date(2026, 9, 1),
            date(2026, 9, 30),
            date(2026, 10, 1),
        )
    ),
)


def history(symbol: str) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=CUTOFF - timedelta(days=252 - index),
            open=Decimal(100 + index),
            high=Decimal(100 + index) + Decimal("0.25"),
            low=Decimal(100 + index) - Decimal("0.25"),
            close=Decimal(100 + index),
            volume=1000 + index,
            adjustment=1,
        )
        for index in range(253)
    ]


def future_bars(symbol: str, closes: list[Decimal]) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            timestamp=CUTOFF + timedelta(days=index + 1),
            open=close,
            high=close + Decimal("0.5"),
            low=max(Decimal("0.01"), close - Decimal("0.5")),
            close=close,
            volume=2000 + index,
            adjustment=1,
        )
        for index, close in enumerate(closes)
    ]


def complete_histories(
    config: StrategyConfig,
    overrides: dict[str, list[Bar]] | None = None,
) -> dict[str, list[Bar]]:
    histories = {symbol: history(symbol) for symbol in config.universe}
    histories.update(overrides or {})
    return histories


def canonical_dump(value: BaseModel) -> str:
    return json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


@given(
    asset_future=st.lists(VALID_PRICE, min_size=1, max_size=5),
    spy_future=st.lists(VALID_PRICE, min_size=1, max_size=5),
)
@settings(max_examples=10, deadline=None)
def test_roster_and_decision_are_byte_identical_when_future_bars_change(
    asset_future: list[Decimal],
    spy_future: list[Decimal],
) -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = build_strategy_identity(
        config,
        git_commit="52128ce",
        deployment_timestamp=datetime(2026, 8, 13, tzinfo=UTC),
    )
    asset = history("QQQ")
    spy = history("SPY")
    roster = build_monthly_roster(
        complete_histories(config, {"QQQ": asset}),
        CUTOFF,
        NEXT_SESSION,
        config,
        session_sequence=XNYS_SESSIONS,
    )
    baseline = evaluate_symbol(
        asset,
        spy,
        roster,
        CUTOFF,
        NEXT_SESSION,
        config,
        identity,
        session_sequence=XNYS_SESSIONS,
    )

    augmented_asset = [*asset, *future_bars("QQQ", asset_future)]
    augmented_spy = [*spy, *future_bars("SPY", spy_future)]
    augmented_histories = {
        symbol: [*bars, *future_bars(symbol, asset_future)]
        for symbol, bars in complete_histories(config).items()
    }
    augmented_histories["QQQ"] = augmented_asset
    augmented_histories["SPY"] = augmented_spy
    augmented_roster = build_monthly_roster(
        augmented_histories,
        CUTOFF,
        NEXT_SESSION,
        config,
        session_sequence=XNYS_SESSIONS,
    )
    augmented = evaluate_symbol(
        augmented_asset,
        augmented_spy,
        augmented_roster,
        CUTOFF,
        NEXT_SESSION,
        config,
        identity,
        session_sequence=XNYS_SESSIONS,
    )

    assert canonical_dump(augmented_roster) == canonical_dump(roster)
    assert canonical_dump(augmented) == canonical_dump(baseline)


@given(extreme_high=st.decimals(min_value="1000", max_value="100000", places=2))
def test_current_bar_extremes_cannot_change_prior_donchian_levels(
    extreme_high: Decimal,
) -> None:
    bars = history("QQQ")[-60:]
    baseline_entry = donchian_entry_level(bars, 55)
    baseline_exit = donchian_exit_level(bars, 20)
    current = bars[-1]
    mutated = Bar(
        symbol=current.symbol,
        timestamp=current.timestamp,
        open=current.open,
        high=extreme_high,
        low=Decimal("0.01"),
        close=current.close,
        volume=current.volume,
        adjustment=current.adjustment,
    )

    assert donchian_entry_level([*bars[:-1], mutated], 55) == baseline_entry
    assert donchian_exit_level([*bars[:-1], mutated], 20) == baseline_exit


@given(future_close=VALID_PRICE)
def test_future_only_symbols_do_not_change_bar_set_hash_through_cutoff(
    future_close: Decimal,
) -> None:
    asset = history("QQQ")
    baseline = bar_set_hash({"QQQ": asset}, CUTOFF)
    augmented = {
        "QQQ": asset,
        "AAPL": future_bars("AAPL", [future_close]),
    }

    assert bar_set_hash(augmented, CUTOFF) == baseline


@given(
    asset_future=st.lists(VALID_PRICE, min_size=1, max_size=5),
    spy_future=st.lists(VALID_PRICE, min_size=1, max_size=5),
)
@settings(max_examples=10, deadline=None)
def test_positioned_decision_is_identical_when_future_bars_change(
    asset_future: list[Decimal],
    spy_future: list[Decimal],
) -> None:
    config = load_strategy_config(CONFIG_PATH)
    identity = build_strategy_identity(
        config,
        git_commit="52128ce",
        deployment_timestamp=datetime(2026, 8, 13, tzinfo=UTC),
    )
    asset = history("QQQ")
    spy = history("SPY")
    roster = build_monthly_roster(
        complete_histories(config, {"QQQ": asset}),
        CUTOFF,
        NEXT_SESSION,
        config,
        session_sequence=XNYS_SESSIONS,
    )
    position = PositionContext(
        symbol="QQQ",
        entered_at=CUTOFF - timedelta(days=10),
        initial_stop=Decimal("1"),
        active_stop=Decimal("1"),
    )
    baseline = evaluate_symbol(
        asset,
        spy,
        roster,
        CUTOFF,
        NEXT_SESSION,
        config,
        identity,
        position,
        session_sequence=XNYS_SESSIONS,
    )
    augmented = evaluate_symbol(
        [*asset, *future_bars("QQQ", asset_future)],
        [*spy, *future_bars("SPY", spy_future)],
        roster,
        CUTOFF,
        NEXT_SESSION,
        config,
        identity,
        position,
        session_sequence=XNYS_SESSIONS,
    )

    assert canonical_dump(augmented) == canonical_dump(baseline)


def test_bar_set_hash_binds_cutoff_even_when_selected_bars_are_unchanged() -> None:
    asset = history("QQQ")
    assert bar_set_hash({"QQQ": asset}, CUTOFF) != bar_set_hash(
        {"QQQ": asset},
        CUTOFF + timedelta(hours=1),
    )


def test_bar_set_hash_normalizes_equivalent_cutoffs_to_utc() -> None:
    asset = history("QQQ")
    eastern = CUTOFF.astimezone(timezone(timedelta(hours=-4)))

    assert bar_set_hash({"QQQ": asset}, eastern) == bar_set_hash(
        {"QQQ": asset},
        CUTOFF,
    )


def test_property_roster_fixture_is_active_on_next_session() -> None:
    roster = MonthlyRoster(
        calendar="XNYS",
        evaluated_at=CUTOFF,
        effective_at=NEXT_SESSION,
        expires_at=ROSTER_EXPIRES,
        symbols=("QQQ",),
        rankings=(),
    )
    assert roster.effective_at > roster.evaluated_at
