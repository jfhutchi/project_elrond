from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantbot.domain import Bar
from quantbot.market_data import (
    AdjustmentMode,
    BarQuery,
    BarValidationReport,
    DataGap,
    DataGapReason,
    MarketDataBatch,
    MarketDataFeed,
    MarketDataValidationError,
    MarketSessionClose,
    align_daily_bars_to_session_closes,
    validate_bar_batch,
)
from quantbot.market_data.validation import (
    IMPLAUSIBLE_LOW_RATIO,
    implausible_range,
)


def timestamp(day: int) -> datetime:
    return datetime(2026, 8, day, 4, tzinfo=UTC)


def bar(symbol: str, day: int, close: str = "100") -> Bar:
    value = Decimal(close)
    return Bar(
        symbol=symbol,
        timestamp=timestamp(day),
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )


def batch(*bars: Bar) -> MarketDataBatch:
    return MarketDataBatch(
        provider="alpaca",
        feed=MarketDataFeed.IEX,
        adjustment=AdjustmentMode.ALL,
        timeframe="1Day",
        requested_symbols=("QQQ", "SPY"),
        start=timestamp(27),
        end=timestamp(31),
        bars=bars,
        page_count=1,
    )


def test_complete_batch_is_eligible_and_preserves_adjustment_provenance() -> None:
    report = validate_bar_batch(
        batch(bar("QQQ", 28), bar("SPY", 28), bar("QQQ", 31), bar("SPY", 31)),
        expected_timestamps=(timestamp(28), timestamp(31)),
        completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
    )

    assert report.eligible_symbols == ("QQQ", "SPY")
    assert report.ineligible_symbols == ()
    assert report.gaps == ()
    assert report.valid_bars == (
        bar("QQQ", 28),
        bar("SPY", 28),
        bar("QQQ", 31),
        bar("SPY", 31),
    )
    assert report.provider == "alpaca"
    assert report.feed is MarketDataFeed.IEX
    assert report.adjustment is AdjustmentMode.ALL
    assert report.timeframe == "1Day"


def test_missing_bar_marks_asset_ineligible_without_forward_fill() -> None:
    original = batch(bar("QQQ", 28), bar("SPY", 28), bar("SPY", 31))

    report = validate_bar_batch(
        original,
        expected_timestamps=(timestamp(28), timestamp(31)),
        completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
    )

    assert report.eligible_symbols == ("SPY",)
    assert report.ineligible_symbols == ("QQQ",)
    assert tuple((gap.symbol, gap.timestamp, gap.reason) for gap in report.gaps) == (
        ("QQQ", timestamp(31), DataGapReason.MISSING_EXPECTED_BAR),
    )
    assert report.valid_bars == original.bars
    assert len([item for item in report.valid_bars if item.symbol == "QQQ"]) == 1


@pytest.mark.parametrize(
    ("observations", "message"),
    [
        ((bar("SPY", 28), bar("SPY", 28)), "duplicate"),
        ((bar("SPY", 31), bar("SPY", 28)), "strictly increasing"),
        ((bar("DIA", 28),), "unexpected symbol"),
        ((bar("SPY", 27),), "unexpected timestamp"),
        ((bar("SPY", 31), bar("QQQ", 31), bar("SPY", 28)), "strictly increasing"),
    ],
)
def test_invalid_observations_fail_closed(
    observations: tuple[Bar, ...],
    message: str,
) -> None:
    with pytest.raises(MarketDataValidationError, match=message):
        validate_bar_batch(
            batch(*observations),
            expected_timestamps=(timestamp(28), timestamp(31)),
            completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )


def test_future_bar_fails_closed() -> None:
    future = bar("SPY", 31).model_copy(update={"timestamp": datetime(2026, 9, 1, 4, tzinfo=UTC)})
    with pytest.raises(MarketDataValidationError, match="future bar"):
        validate_bar_batch(
            batch(future),
            expected_timestamps=(datetime(2026, 9, 1, 4, tzinfo=UTC),),
            completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )


def test_validation_inputs_are_strict_and_timezone_aware() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        validate_bar_batch(
            batch(),
            expected_timestamps=(),
            completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="strictly increasing"):
        validate_bar_batch(
            batch(),
            expected_timestamps=(timestamp(31), timestamp(28)),
            completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        validate_bar_batch(
            batch(),
            expected_timestamps=(timestamp(28),),
            completed_through=datetime(2026, 8, 31, 20),
        )


def test_query_rejects_duplicate_or_padded_symbols() -> None:
    common = {
        "start": timestamp(28),
        "end": timestamp(31),
        "timeframe": "1Day",
        "feed": MarketDataFeed.IEX,
        "adjustment": AdjustmentMode.ALL,
        "limit": 10000,
    }
    with pytest.raises(ValueError, match="unique"):
        BarQuery(symbols=("SPY", "SPY"), **common)
    with pytest.raises(ValueError, match="uppercase"):
        BarQuery(symbols=(" SPY",), **common)


def test_daily_bars_align_to_actual_session_closes_across_dst() -> None:
    raw = MarketDataBatch(
        provider="alpaca",
        feed=MarketDataFeed.IEX,
        adjustment=AdjustmentMode.ALL,
        timeframe="1Day",
        requested_symbols=("SPY",),
        start=datetime(2026, 3, 6, 5, tzinfo=UTC),
        end=datetime(2026, 3, 9, 4, tzinfo=UTC),
        bars=(
            bar("SPY", 28).model_copy(update={"timestamp": datetime(2026, 3, 6, 5, tzinfo=UTC)}),
            bar("SPY", 31).model_copy(update={"timestamp": datetime(2026, 3, 9, 4, tzinfo=UTC)}),
        ),
        page_count=1,
    )
    sessions = (
        MarketSessionClose(
            calendar="XNYS",
            session_date=date(2026, 3, 6),
            close_at=datetime(2026, 3, 6, 21, tzinfo=UTC),
        ),
        MarketSessionClose(
            calendar="XNYS",
            session_date=date(2026, 3, 9),
            close_at=datetime(2026, 3, 9, 20, tzinfo=UTC),
        ),
    )

    aligned = align_daily_bars_to_session_closes(raw, sessions=sessions)

    assert tuple(item.timestamp for item in aligned.bars) == (
        datetime(2026, 3, 6, 21, tzinfo=UTC),
        datetime(2026, 3, 9, 20, tzinfo=UTC),
    )
    assert aligned.timeframe == "1Day"
    assert tuple(item.close for item in aligned.bars) == (
        raw.bars[0].close,
        raw.bars[1].close,
    )


def test_daily_alignment_rejects_missing_or_wrong_calendar_sessions() -> None:
    raw = batch(bar("SPY", 28))
    with pytest.raises(MarketDataValidationError, match="missing XNYS session"):
        align_daily_bars_to_session_closes(raw, sessions=())
    with pytest.raises(ValueError, match="XNYS"):
        MarketSessionClose(
            calendar="OTHER",
            session_date=date(2026, 8, 28),
            close_at=datetime(2026, 8, 28, 20, tzinfo=UTC),
        )


def test_batch_and_validation_report_reject_inconsistent_identity_sets() -> None:
    common = {
        "provider": "alpaca",
        "feed": MarketDataFeed.IEX,
        "adjustment": AdjustmentMode.ALL,
        "timeframe": "1Day",
        "start": timestamp(28),
        "end": timestamp(31),
        "bars": (),
        "page_count": 1,
    }
    with pytest.raises(ValueError, match="unique"):
        MarketDataBatch(requested_symbols=("SPY", "SPY"), **common)
    with pytest.raises(ValueError, match="partition"):
        BarValidationReport(
            provider="alpaca",
            feed=MarketDataFeed.IEX,
            adjustment=AdjustmentMode.ALL,
            timeframe="1Day",
            requested_symbols=("QQQ", "SPY"),
            valid_bars=(),
            gaps=(),
            eligible_symbols=("SPY",),
            ineligible_symbols=(),
            stale_symbols=(),
            completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
        )


def corrupt_bar(symbol: str, day: int) -> Bar:
    """The real SPY 2026-02-02 bar: a dropped digit in the low, everything else intact.

    It satisfies `Bar.validate_ohlc` because that check is ordinal — 68.64 really is <= the open,
    high, and close — which is exactly why it reached a backtest and cost 20.1% of equity in one
    session by tripping a stop at a price no trade occurred at.
    """
    return Bar(
        symbol=symbol,
        timestamp=timestamp(day),
        open=Decimal("685.90"),
        high=Decimal("693.21"),
        low=Decimal("68.64"),
        close=Decimal("691.70"),
        volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )


def test_a_bar_whose_extreme_the_open_and_close_do_not_corroborate_is_rejected() -> None:
    report = validate_bar_batch(
        batch(bar("QQQ", 28), bar("SPY", 28), bar("QQQ", 31), corrupt_bar("SPY", 31)),
        expected_timestamps=(timestamp(28), timestamp(31)),
        completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
    )

    assert report.ineligible_symbols == ("SPY",), "the corrupt bar must not be silently traded on"
    assert report.eligible_symbols == ("QQQ",), "one bad bar must not condemn an unrelated symbol"
    assert report.gaps == (
        DataGap(
            symbol="SPY",
            timestamp=timestamp(31),
            reason=DataGapReason.IMPLAUSIBLE_RANGE,
        ),
    ), "reported under its own reason, not as a missing bar — the bar is present, it is wrong"
    assert corrupt_bar("SPY", 31) not in report.valid_bars
    assert report.valid_bars == (bar("QQQ", 28), bar("SPY", 28), bar("QQQ", 31))


def test_the_threshold_does_not_fire_on_a_real_crash_that_moves_the_close() -> None:
    """A genuine collapse drags the close with it, so the bracket test leaves it alone.

    This is the property that makes the rule safe for volatile single names, and it is the one
    that would break if the check were rewritten in terms of absolute intraday range.
    """
    crash = Bar(
        symbol="SPY",
        timestamp=timestamp(31),
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("55"),
        close=Decimal("58"),
        volume=Decimal("1000"),
        adjustment=Decimal("1"),
    )
    assert not implausible_range(crash), "a 45% drop that holds into the close is real data"
    report = validate_bar_batch(
        batch(bar("QQQ", 28), bar("SPY", 28), bar("QQQ", 31), crash),
        expected_timestamps=(timestamp(28), timestamp(31)),
        completed_through=datetime(2026, 8, 31, 20, tzinfo=UTC),
    )
    assert report.eligible_symbols == ("QQQ", "SPY")
    assert report.gaps == ()


def test_the_margin_between_the_defect_and_real_data_is_not_a_tuned_parameter() -> None:
    """Calibration evidence, asserted so it fails if the threshold is moved into live data.

    Measured over the full 96,370-bar research history: the corrupt bar sits at a
    low/min(open, close) ratio of 0.1001 and the lowest ratio among all other bars is 0.8927.
    The threshold must stay inside that empty margin at both ends.
    """
    assert IMPLAUSIBLE_LOW_RATIO > Decimal("0.1001"), "must still catch the known defect"
    assert IMPLAUSIBLE_LOW_RATIO < Decimal("0.8927"), "must not reach the closest real bar"
