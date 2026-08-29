"""Freshness judged against the exchange calendar, case by case (#74).

Every case here is one a wall-clock ceiling gets wrong. The old rule was a single number, and a
single number cannot separate "Friday's close, on Sunday" from "Tuesday's close, on Wednesday" --
the first is perfectly current and the second is a missing session, and the second is *newer*.

Sol's list on #74: normal weekend, long weekend, holiday, half-day, missing prior session, and an
intraday feed that stops during an open session. Each is a test below, written against the rule
rather than through a portfolio fixture so a failure names the calendar behaviour that broke.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from quantbot.risk.freshness import (
    DataCadence,
    FreshnessOutcome,
    MarketFreshnessPolicy,
    MarketState,
    assess_freshness,
    expected_freshness,
)
from quantbot.strategy import XNYSSession

#: Regular XNYS hours in UTC while daylight saving is in effect: 09:30-16:00 Eastern.
OPEN_UTC = time(13, 30)
CLOSE_UTC = time(20, 0)
#: A half-day closes at 13:00 Eastern. The calendar carries it as a shorter session, so nothing
#: in the rule needs to know half-days exist -- which is the point of reading the calendar.
HALF_DAY_CLOSE_UTC = time(17, 0)


def session(day: date, *, close: time = CLOSE_UTC) -> XNYSSession:
    return XNYSSession(
        session_date=day,
        open_at=datetime.combine(day, OPEN_UTC, tzinfo=UTC),
        close_at=datetime.combine(day, close, tzinfo=UTC),
    )


def assess(
    *,
    sessions: tuple[XNYSSession, ...],
    latest: datetime | None,
    now: datetime,
    policy: MarketFreshnessPolicy | None = None,
):
    resolved = policy or MarketFreshnessPolicy()
    return assess_freshness(
        expectation=expected_freshness(sessions=sessions, observed_at=now, policy=resolved),
        latest_observation_at=latest,
        observed_at=now,
        symbol="SPY",
        tolerated_intervals=resolved.tolerated_intervals,
    )


# 2026-08-27 is a Thursday; 08-28 Friday; 08-29/30 the weekend; 08-31 Monday.
THURSDAY = date(2026, 8, 27)
FRIDAY = date(2026, 8, 28)
MONDAY = date(2026, 8, 31)
TUESDAY = date(2026, 9, 1)


def test_a_friday_close_is_current_all_weekend() -> None:
    """The case the four-day ceiling existed to survive, now with no ceiling at all.

    Sunday evening is ~55 hours after Friday's close. Under a wall clock that is a number to be
    compared against a threshold somebody guessed; under the calendar it is simply the newest
    session there is.
    """
    sessions = (session(THURSDAY), session(FRIDAY))
    friday_close = datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC)

    for moment in (
        friday_close + timedelta(hours=1),
        datetime(2026, 8, 29, 18, 0, tzinfo=UTC),
        datetime(2026, 8, 30, 23, 0, tzinfo=UTC),
    ):
        verdict = assess(sessions=sessions, latest=friday_close, now=moment)
        assert verdict.outcome is FreshnessOutcome.FRESH, moment
        assert verdict.market_state is MarketState.CLOSED


def test_a_long_weekend_needs_no_extra_allowance() -> None:
    """A Monday holiday makes the gap longer, and the rule does not notice.

    This is what "no magic number" buys: nothing was tuned for a three-day weekend, and the
    calendar simply has no Monday session in it.
    """
    sessions = (session(FRIDAY), session(TUESDAY))
    friday_close = datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC)

    # Tuesday morning, before the open: Friday is still the last completed session.
    verdict = assess(
        sessions=sessions,
        latest=friday_close,
        now=datetime.combine(TUESDAY, time(12, 0), tzinfo=UTC),
    )

    assert verdict.outcome is FreshnessOutcome.FRESH
    assert verdict.expected_session == FRIDAY


def test_a_holiday_is_not_a_missing_session() -> None:
    """A day the market did not trade cannot be a day the feed failed to deliver."""
    sessions = (session(FRIDAY), session(TUESDAY))
    friday_close = datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC)

    # Monday afternoon. A calendar-day rule would be counting; the exchange was shut.
    verdict = assess(
        sessions=sessions,
        latest=friday_close,
        now=datetime.combine(MONDAY, time(18, 0), tzinfo=UTC),
    )

    assert verdict.outcome is FreshnessOutcome.FRESH
    assert verdict.expected_session == FRIDAY, "Monday never closed, so it is not expected"


def test_a_half_day_close_is_the_session_close() -> None:
    """Half-days need no special case because the calendar carries the real close time.

    A rule that assumed 16:00 would call the half-day session incomplete for three hours after it
    had actually ended, and refuse every entry in that window.
    """
    sessions = (session(THURSDAY), session(FRIDAY, close=HALF_DAY_CLOSE_UTC))
    half_day_close = datetime.combine(FRIDAY, HALF_DAY_CLOSE_UTC, tzinfo=UTC)

    # 17:30 UTC: after the early close, before a regular one would have happened.
    verdict = assess(
        sessions=sessions,
        latest=half_day_close,
        now=datetime.combine(FRIDAY, time(17, 30), tzinfo=UTC),
    )

    assert verdict.outcome is FreshnessOutcome.FRESH
    assert verdict.expected_session == FRIDAY
    assert verdict.market_state is MarketState.CLOSED


def test_a_missing_session_is_stale_within_hours() -> None:
    """The case the four-day ceiling got wrong in the *permissive* direction.

    Thursday's close, judged on Friday evening, is about 25 hours old — comfortably inside a
    four-day allowance and missing an entire session the market completed.
    """
    sessions = (session(THURSDAY), session(FRIDAY))
    thursday_close = datetime.combine(THURSDAY, CLOSE_UTC, tzinfo=UTC)

    verdict = assess(
        sessions=sessions,
        latest=thursday_close,
        now=datetime.combine(FRIDAY, time(20, 30), tzinfo=UTC),
    )

    assert verdict.outcome is FreshnessOutcome.EXPECTED_SESSION_MISSING
    assert verdict.expected_session == FRIDAY
    assert verdict.observed_session == THURSDAY
    assert "already closed" in verdict.detail


def test_an_intraday_feed_that_stops_during_the_session_is_stale_in_minutes() -> None:
    """The case #56 would otherwise inherit a four-day allowance for.

    The same daily rule would call this fresh: the observation is from the current session, and
    the session is the latest one. Cadence is what distinguishes a live feed from a dead one
    while the market is open.
    """
    sessions = (session(THURSDAY), session(FRIDAY))
    minute = MarketFreshnessPolicy(
        cadence=DataCadence.INTRADAY, expected_interval_seconds=60, tolerated_intervals=3
    )
    midday = datetime.combine(FRIDAY, time(16, 0), tzinfo=UTC)

    live = assess(sessions=sessions, latest=midday - timedelta(seconds=90), now=midday,
                  policy=minute)
    stopped = assess(sessions=sessions, latest=midday - timedelta(minutes=20), now=midday,
                     policy=minute)

    assert live.outcome is FreshnessOutcome.FRESH
    assert live.market_state is MarketState.OPEN

    assert stopped.outcome is FreshnessOutcome.FEED_STALE
    assert "the feed has stopped while the market has not" in stopped.detail

    # And the identical observation under the daily rule is fine, which is the whole point.
    daily = assess(sessions=sessions, latest=midday - timedelta(minutes=20), now=midday)
    assert daily.outcome is FreshnessOutcome.FRESH


def test_a_closed_market_is_not_a_stopped_feed() -> None:
    """`MARKET_CLOSED` and `FEED_STALE` are different facts (#74 item 4).

    An intraday feed is *supposed* to go quiet overnight. Measuring elapsed time without asking
    whether the venue was open is how a normal night reads as an outage.
    """
    sessions = (session(THURSDAY), session(FRIDAY))
    minute = MarketFreshnessPolicy(
        cadence=DataCadence.INTRADAY, expected_interval_seconds=60, tolerated_intervals=3
    )
    friday_close = datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC)

    overnight = assess(
        sessions=sessions,
        latest=friday_close,
        now=friday_close + timedelta(hours=6),
        policy=minute,
    )

    assert overnight.outcome is FreshnessOutcome.FRESH
    assert overnight.market_state is MarketState.CLOSED, (
        "six hours of silence with the market shut is not a feed outage"
    )


def test_every_missing_input_fails_closed() -> None:
    """No calendar, no observation, no cadence: each rejects rather than assuming."""
    sessions = (session(THURSDAY), session(FRIDAY))
    friday_close = datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC)
    later = friday_close + timedelta(hours=1)

    # Asserted at the API level, not only through the assessment: an empty calendar must yield
    # *no expectation at all*. Checking only the downstream outcome would pass on a version that
    # happily built an expectation claiming a calendar it does not have.
    assert (
        expected_freshness(sessions=(), observed_at=later, policy=MarketFreshnessPolicy())
        is None
    )
    no_calendar = assess(sessions=(), latest=friday_close, now=later)
    assert no_calendar.outcome is FreshnessOutcome.FRESHNESS_METADATA_UNAVAILABLE

    no_observation = assess(sessions=sessions, latest=None, now=later)
    assert no_observation.outcome is FreshnessOutcome.FRESHNESS_METADATA_UNAVAILABLE

    future = assess(sessions=sessions, latest=later + timedelta(days=1), now=later)
    assert future.outcome is FreshnessOutcome.FRESHNESS_METADATA_UNAVAILABLE

    # An intraday policy with no declared cadence has no answer to "how stale is too stale", and
    # is refused at construction rather than given a default nobody chose.
    with pytest.raises(ValueError, match="expected observation interval"):
        MarketFreshnessPolicy(cadence=DataCadence.INTRADAY)


def test_the_rule_a_decision_ran_under_can_be_named() -> None:
    """#57 compares refusals across rule versions and cannot group on prose (#74 item 5)."""
    daily = MarketFreshnessPolicy()
    minute = MarketFreshnessPolicy(cadence=DataCadence.INTRADAY, expected_interval_seconds=60)

    assert daily.identity.startswith("freshness-policy-v1-")
    assert daily.identity != minute.identity

    # Cadence alone must move the hash. The pair above also differ by interval, so it would pass
    # on an identity that ignored cadence entirely -- these differ in nothing else.
    same_interval_daily = MarketFreshnessPolicy(expected_interval_seconds=60)
    same_interval_intraday = MarketFreshnessPolicy(
        cadence=DataCadence.INTRADAY, expected_interval_seconds=60
    )
    assert same_interval_daily.identity != same_interval_intraday.identity
    assert daily.identity != MarketFreshnessPolicy(tolerated_intervals=9).identity
    assert daily.identity != MarketFreshnessPolicy(calendar="XCME").identity

    sessions = (session(THURSDAY), session(FRIDAY))
    verdict = assess(
        sessions=sessions,
        latest=datetime.combine(FRIDAY, CLOSE_UTC, tzinfo=UTC),
        now=datetime.combine(FRIDAY, time(21, 0), tzinfo=UTC),
    )
    assert verdict.policy_identity == daily.identity
