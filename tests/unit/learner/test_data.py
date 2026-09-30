"""Point-in-time daily learner data and fixed-session targets (#242)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from math import isfinite

import pytest

from quantbot.domain import Bar
from quantbot.learner.data import (
    FEATURE_NAMES,
    DailyInput,
    Dataset,
    FeatureRow,
    LabelRow,
    build_dataset,
    build_features,
)
from quantbot.research.registry import DataRole, DataWindow
from quantbot.strategy import XNYSSession, XNYSSessionSequence


def trading_dates(count: int, *, start: date = date(2026, 1, 5)) -> tuple[date, ...]:
    values: list[date] = []
    cursor = start
    while len(values) < count:
        if cursor.weekday() < 5:
            values.append(cursor)
        cursor += timedelta(days=1)
    return tuple(values)


def session_sequence(count: int = 95) -> XNYSSessionSequence:
    sessions = tuple(
        XNYSSession(
            session_date=day,
            open_at=datetime(day.year, day.month, day.day, 14, 30, tzinfo=UTC),
            close_at=datetime(day.year, day.month, day.day, 21, 0, tzinfo=UTC),
        )
        for day in trading_dates(count)
    )
    return XNYSSessionSequence(calendar="XNYS", sessions=sessions)


def inputs_for(sessions: XNYSSessionSequence, *, through: int) -> tuple[DailyInput, ...]:
    values: list[DailyInput] = []
    for index, session in enumerate(sessions.sessions[: through + 1]):
        for symbol, offset in (("AAPL", 100), ("MSFT", 140), ("SPY", 300)):
            close = offset + index
            values.append(
                DailyInput(
                    bar=Bar(
                        symbol=symbol,
                        timestamp=session.close_at,
                        open=str(close - 1),
                        high=str(close + 1),
                        low=str(close - 2),
                        close=str(close),
                        volume=str(1_000_000 + index * 10_000 + offset),
                        adjustment="1",
                    ),
                    available_at=session.close_at,
                )
            )
    return tuple(values)


def window(
    role: DataRole, sessions: XNYSSessionSequence, start: int, end: int
) -> DataWindow:
    return DataWindow(
        dataset="sip-us-equities-daily",
        snapshot="vintage-2026-09-14",
        role=role,
        start=sessions.sessions[start].session_date,
        end=sessions.sessions[end].session_date,
    )


def test_build_features_uses_only_known_inputs_and_the_exact_fifth_future_session() -> None:
    """Changing the target to a calendar-day offset or using a late input must fail this test."""
    sessions = session_sequence()
    cutoff = sessions.sessions[64].close_at
    inputs = list(inputs_for(sessions, through=64))
    late = next(
        index
        for index, item in enumerate(inputs)
        if item.bar.symbol == "MSFT" and item.bar.timestamp == cutoff
    )
    inputs[late] = inputs[late].model_copy(update={"available_at": cutoff + timedelta(minutes=1)})

    rows = build_features(
        inputs,
        sessions,
        as_of=cutoff,
        symbols=("AAPL", "MSFT"),
        universe_id="liquid-us-v1",
        source_id="fixture-v1",
    )

    assert [row.symbol for row in rows] == ["AAPL"]
    assert rows[0].as_of == cutoff
    assert rows[0].input_start_at == sessions.sessions[4].close_at
    assert rows[0].target_at == sessions.sessions[69].close_at
    assert rows[0].horizon == 5
    assert rows[0].target_session_index - rows[0].asof_session_index == 5
    assert len(rows[0].calendar_hash) == 64
    assert len(rows[0].values) == len(FEATURE_NAMES)
    assert all(isfinite(value) for value in rows[0].values)
    assert len(rows[0].identity) == 64


def test_build_features_requires_the_cutoff_to_be_an_exact_session_close() -> None:
    sessions = session_sequence()

    with pytest.raises(ValueError, match="session close"):
        build_features(
            inputs_for(sessions, through=64),
            sessions,
            as_of=sessions.sessions[64].close_at + timedelta(minutes=1),
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
        )


def test_dataset_labels_exact_sessions_and_purges_targets_at_the_next_split() -> None:
    """Changing purge from target-before-boundary to feature-before-boundary leaks outcomes."""
    sessions = session_sequence()
    subject = build_dataset(
        inputs_for(sessions, through=84),
        sessions,
        symbols=("AAPL", "MSFT"),
        universe_id="liquid-us-v1",
        source_id="fixture-v1",
        train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
        validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
        holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
    )

    train = [row for row in subject.rows if row.role is DataRole.DISCOVERY]
    validation = [row for row in subject.rows if row.role is DataRole.VALIDATION]
    assert {row.features.as_of for row in train} == {
        sessions.sessions[index].close_at for index in range(60, 65)
    }
    assert {row.features.as_of for row in validation} == {
        sessions.sessions[index].close_at for index in range(70, 80)
    }
    assert all(row.features.target_at.date() < subject.holdout_window.start for row in validation)
    first = next(row for row in validation if row.features.symbol == "AAPL")
    assert first.features.target_at == sessions.sessions[75].close_at
    assert first.excess_return == pytest.approx(first.raw_return - first.benchmark_return)
    assert {row.role for row in subject.rows} == {DataRole.DISCOVERY, DataRole.VALIDATION}
    assert len(subject.identity) == 64


def test_a_missing_fifth_session_bar_is_not_compressed_to_the_next_observed_bar() -> None:
    """Using the fifth observed bar would silently shorten or lengthen the calendar horizon."""
    sessions = session_sequence()
    target = sessions.sessions[75].close_at
    inputs = tuple(
        item
        for item in inputs_for(sessions, through=84)
        if not (item.bar.symbol == "AAPL" and item.bar.timestamp == target)
    )
    with pytest.raises(ValueError, match="missing exact target"):
        build_dataset(
            inputs,
            sessions,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
            validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
            holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
        )


@pytest.mark.parametrize("late_symbol", ["AAPL", "SPY"])
def test_a_target_published_after_its_session_cannot_become_that_sessions_label(
    late_symbol: str,
) -> None:
    """Using a later revision as the earlier target is outcome look-ahead."""
    sessions = session_sequence()
    target = sessions.sessions[75].close_at
    inputs = list(inputs_for(sessions, through=84))
    late = next(
        index
        for index, item in enumerate(inputs)
        if item.bar.symbol == late_symbol and item.bar.timestamp == target
    )
    inputs[late] = inputs[late].model_copy(
        update={"available_at": sessions.sessions[85].close_at}
    )

    with pytest.raises(ValueError, match="target was unavailable"):
        build_dataset(
            inputs,
            sessions,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
            validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
            holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
        )


def test_dataset_rejects_any_already_materialized_input_beyond_validation() -> None:
    """Removing the early range check would let protected prices reach feature code."""
    sessions = session_sequence()

    with pytest.raises(ValueError, match="protected or undeclared"):
        build_dataset(
            inputs_for(sessions, through=85),
            sessions,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            train_window=window(DataRole.DISCOVERY, sessions, 50, 69),
            validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
            holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
        )


def test_dataset_requires_canonical_ordered_split_roles() -> None:
    sessions = session_sequence()

    with pytest.raises(ValueError, match="train_window must be DISCOVERY"):
        build_dataset(
            inputs_for(sessions, through=84),
            sessions,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            train_window=window(DataRole.VALIDATION, sessions, 50, 69),
            validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
            holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
        )


def test_duplicate_symbol_session_input_is_refused() -> None:
    sessions = session_sequence()
    inputs = inputs_for(sessions, through=64)

    with pytest.raises(ValueError, match="duplicate daily input"):
        build_features(
            (*inputs, inputs[0]),
            sessions,
            as_of=sessions.sessions[64].close_at,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
        )


def test_nonfinite_features_and_labels_are_refused() -> None:
    sessions = session_sequence()
    with pytest.raises(ValueError, match="finite"):
        FeatureRow(
            as_of=sessions.sessions[0].close_at,
            input_start_at=sessions.sessions[0].close_at,
            target_at=sessions.sessions[5].close_at,
            asof_session_index=0,
            target_session_index=5,
            calendar_hash="c" * 64,
            symbol="AAPL",
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            base_close=100.0,
            benchmark_close=300.0,
            values=(float("nan"),) * len(FEATURE_NAMES),
            source_hash="a" * 64,
        )


def test_feature_values_must_match_the_fixed_schema() -> None:
    sessions = session_sequence()
    with pytest.raises(ValueError, match="fixed features"):
        FeatureRow(
            as_of=sessions.sessions[0].close_at,
            input_start_at=sessions.sessions[0].close_at,
            target_at=sessions.sessions[5].close_at,
            asof_session_index=0,
            target_session_index=5,
            calendar_hash="c" * 64,
            symbol="AAPL",
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            base_close=100.0,
            benchmark_close=300.0,
            values=(0.0,) * (len(FEATURE_NAMES) - 1),
            source_hash="a" * 64,
        )


def test_label_requires_exact_excess_return_and_forbids_protected_fitting() -> None:
    sessions = session_sequence()
    features = build_features(
        inputs_for(sessions, through=64),
        sessions,
        as_of=sessions.sessions[64].close_at,
        symbols=("AAPL",),
        universe_id="liquid-us-v1",
        source_id="fixture-v1",
    )[0]
    with pytest.raises(ValueError, match="exactly equal"):
        LabelRow(
            features=features,
            role=DataRole.DISCOVERY,
            raw_return=0.10,
            benchmark_return=0.04,
            excess_return=0.07,
            target_source_hash="b" * 64,
        )

    subject = build_dataset(
        inputs_for(sessions, through=84),
        sessions,
        symbols=("AAPL",),
        universe_id="liquid-us-v1",
        source_id="fixture-v1",
        train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
        validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
        holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
    )
    with pytest.raises(ValueError, match="only DISCOVERY"):
        subject.rows_for_fit(DataRole.PROTECTED_EVALUATION)
    with pytest.raises(ValueError, match="only DISCOVERY"):
        subject.rows_for_fit(DataRole.VALIDATION)


def test_daily_input_cannot_claim_a_bar_before_the_session_exists() -> None:
    sessions = session_sequence()
    item = inputs_for(sessions, through=0)[0]
    with pytest.raises(ValueError, match="cannot be available before"):
        DailyInput(bar=item.bar, available_at=item.bar.timestamp - timedelta(seconds=1))


def test_feature_schema_cannot_silently_change_its_horizon_or_lookback_range() -> None:
    sessions = session_sequence()
    common = {
        "as_of": sessions.sessions[10].close_at,
        "input_start_at": sessions.sessions[0].close_at,
        "target_at": sessions.sessions[15].close_at,
        "asof_session_index": 10,
        "target_session_index": 15,
        "calendar_hash": "c" * 64,
        "symbol": "AAPL",
        "universe_id": "liquid-us-v1",
        "source_id": "fixture-v1",
        "base_close": 100.0,
        "benchmark_close": 300.0,
        "values": (0.0,) * len(FEATURE_NAMES),
        "source_hash": "a" * 64,
    }
    with pytest.raises(ValueError, match="five-session"):
        FeatureRow(**common, horizon=4)
    with pytest.raises(ValueError, match="input_start_at"):
        FeatureRow(**{**common, "input_start_at": sessions.sessions[11].close_at})
    with pytest.raises(ValueError, match="session index"):
        FeatureRow(**{**common, "target_session_index": 14})


def test_dataset_canonicalizes_rows_and_refuses_duplicate_observations() -> None:
    sessions = session_sequence()
    subject = build_dataset(
        inputs_for(sessions, through=84),
        sessions,
        symbols=("MSFT", "AAPL"),
        universe_id="liquid-us-v1",
        source_id="fixture-v1",
        train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
        validation_window=window(DataRole.VALIDATION, sessions, 70, 84),
        holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
    )
    rebuilt = Dataset(**{**subject.model_dump(), "rows": tuple(reversed(subject.rows))})
    assert rebuilt.rows == subject.rows
    assert rebuilt.identity == subject.identity
    assert subject.symbols == ("AAPL", "MSFT")

    with pytest.raises(ValueError, match="duplicate learner observation"):
        Dataset(**{**subject.model_dump(), "rows": (*subject.rows, subject.rows[0])})


def test_dataset_refuses_mixed_split_snapshots() -> None:
    sessions = session_sequence()
    with pytest.raises(ValueError, match="same snapshot"):
        build_dataset(
            inputs_for(sessions, through=84),
            sessions,
            symbols=("AAPL",),
            universe_id="liquid-us-v1",
            source_id="fixture-v1",
            train_window=window(DataRole.DISCOVERY, sessions, 60, 69),
            validation_window=window(DataRole.VALIDATION, sessions, 70, 84).model_copy(
                update={"snapshot": "other-vintage"}
            ),
            holdout_window=window(DataRole.PROTECTED_EVALUATION, sessions, 85, 94),
        )
