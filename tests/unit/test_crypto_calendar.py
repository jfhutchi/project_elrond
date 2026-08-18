"""A 24/7 calendar must satisfy the same contract the strategy enforces for XNYS.

The point of synthesising one session per UTC day is that the existing deterministic
machinery — evaluate at a completed close, act at the next open, monthly roster bounded by
calendar months — applies unchanged. That only holds if the synthesised sessions pass every
invariant the strategy checks, so these tests exercise those invariants rather than just
asserting the shape of the output.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from quantbot.market_data.calendar import (
    crypto_session_sequence,
    crypto_sessions,
    latest_completed_session,
)


def test_one_session_per_utc_day_covering_the_whole_day() -> None:
    sessions = crypto_sessions(date(2026, 8, 1), date(2026, 8, 31))

    assert len(sessions) == 31
    first = sessions[0]
    assert first.open_at == datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    assert first.close_at == datetime(2026, 8, 1, 23, 59, 59, 999999, tzinfo=UTC)


def test_consecutive_sessions_are_strictly_increasing_and_do_not_overlap() -> None:
    """The sequence validator rejects overlap, so a day's close must precede the next open."""
    sessions = crypto_sessions(date(2026, 8, 1), date(2026, 8, 10))

    for previous, current in zip(sessions, sessions[1:], strict=False):
        assert current.open_at > previous.close_at
        assert current.session_date > previous.session_date

    sequence = crypto_session_sequence(sessions)
    assert sequence.calendar == "CRYPTO24"
    assert sequence.sessions == sessions


def test_close_to_open_transition_resolves_for_every_adjacent_pair() -> None:
    """transition_index is what the strategy uses to prove it acts on the next session."""
    sessions = crypto_sessions(date(2026, 7, 28), date(2026, 8, 4))
    sequence = crypto_session_sequence(sessions)

    for index, session in enumerate(sessions[:-1]):
        resolved = sequence.transition_index(session.close_at, sessions[index + 1].open_at)
        assert resolved == index


def test_month_boundary_resolves_so_the_monthly_roster_can_expire() -> None:
    """A 24/7 calendar still has calendar months, which is what bounds the roster."""
    sessions = crypto_sessions(date(2026, 7, 1), date(2026, 9, 5))
    sequence = crypto_session_sequence(sessions)
    first_august = next(i for i, s in enumerate(sessions) if s.session_date == date(2026, 8, 1))

    expiry = sequence.roster_expiry_after(first_august)

    assert expiry == datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


def test_latest_completed_session_treats_an_in_progress_day_as_incomplete() -> None:
    """Mid-day, the current day is not yet a completed session — no look-ahead."""
    sessions = crypto_sessions(date(2026, 8, 15), date(2026, 8, 17))

    midday = latest_completed_session(sessions, datetime(2026, 8, 17, 12, 0, tzinfo=UTC))
    end_of_day = latest_completed_session(
        sessions, datetime(2026, 8, 17, 23, 59, 59, 999999, tzinfo=UTC)
    )

    assert midday is not None and midday.session_date == date(2026, 8, 16)
    assert end_of_day is not None and end_of_day.session_date == date(2026, 8, 17)


def test_a_single_day_cannot_form_a_sequence() -> None:
    """The strategy needs at least a close and a following open."""
    from quantbot.market_data import MarketDataProtocolError

    with pytest.raises(MarketDataProtocolError, match="strictly increasing"):
        crypto_session_sequence(crypto_sessions(date(2026, 8, 1), date(2026, 8, 1)))


def test_inverted_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="on or after start"):
        crypto_sessions(date(2026, 8, 10), date(2026, 8, 1))
