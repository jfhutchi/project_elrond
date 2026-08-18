"""Tranche identity on rosters, and the equivalence the whole generalisation rests on.

Tranching is the largest free effect measured in this project: $61.24 of terminal wealth per
$100 is decided by nothing but which day of the month the rotation happens to run. Removing
that dispersion needs five independent rosters on five staggered sessions, which means roster
construction can no longer assume month-end.

The safety argument for landing that change before anything switches it on is an equivalence:
**with one tranche, every tranche-aware rule reduces to the month-end rule it replaces.** These
tests hold that equivalence to the calendar rather than to a docstring, because if it ever
breaks it breaks silently for four deployed strategy identities.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from quantbot.strategy.adaptive_momentum import (
    MonthlyRoster,
    XNYSSession,
    XNYSSessionSequence,
)

CALENDAR = "XNYS"


def _sessions(start: datetime, count: int) -> XNYSSessionSequence:
    """Weekday sessions, which is enough calendar realism for a month-boundary rule."""
    schedules: list[XNYSSession] = []
    day = start
    while len(schedules) < count:
        if day.weekday() < 5:
            schedules.append(
                XNYSSession(
                    session_date=day.date(),
                    open_at=day.replace(hour=14, minute=30),
                    close_at=day.replace(hour=21, minute=0),
                )
            )
        day += timedelta(days=1)
    return XNYSSessionSequence(calendar=CALENDAR, sessions=tuple(schedules))


SEQUENCE = _sessions(datetime(2024, 1, 1, tzinfo=UTC), 200)


def _month_end_indices() -> list[int]:
    """Sessions the month-end rule can actually evaluate on.

    Deliberately excludes the sequence's final session. The schedule counts it as a month end,
    correctly — the month simply has not finished — but `build_monthly_roster` needs a following
    session to be effective on, so it can never be an evaluation date. The two therefore differ
    by exactly that one index, and the tests below say so rather than papering over it.
    """
    out = []
    for index in range(len(SEQUENCE.sessions) - 1):
        this = SEQUENCE.sessions[index].session_date.strftime("%Y-%m")
        following = SEQUENCE.sessions[index + 1].session_date.strftime("%Y-%m")
        if this != following:
            out.append(index)
    return out


LAST_INDEX = len(SEQUENCE.sessions) - 1


def test_one_tranche_rebalances_exactly_on_month_end_sessions() -> None:
    """The schedule must select the same sessions the month-end rule does, and no others."""
    schedule = SEQUENCE.tranche_schedule_map(1)

    assert sorted(schedule) == [*_month_end_indices(), LAST_INDEX]
    assert set(schedule.values()) == {0}


def test_one_tranche_expiry_equals_the_month_boundary_rule_it_replaces() -> None:
    """The equivalence the generalisation depends on, checked at every month in the sequence.

    `tranche_expiry_after` is the tranche-aware replacement for `roster_expiry_after`. If these
    two ever disagree at one tranche, four deployed identities change behaviour with no diff to
    show for it.
    """
    checked = 0
    for evaluation_index in _month_end_indices():
        effective_index = evaluation_index + 1
        try:
            expected = SEQUENCE.roster_expiry_after(effective_index)
        except ValueError:
            continue  # near the end of the sequence there is no following month to expire into
        actual = SEQUENCE.tranche_expiry_after(evaluation_index, tranche=0, tranches=1)
        assert actual == expected, f"diverged at session index {evaluation_index}"
        checked += 1
    assert checked >= 5, "the equivalence must be exercised over several months, not one"


def test_five_tranches_rebalance_on_five_distinct_staggered_sessions() -> None:
    """The actual mechanism: five rebalance dates per month, one per tranche."""
    schedule = SEQUENCE.tranche_schedule_map(5)

    by_month: dict[str, list[int]] = {}
    for index, tranche in schedule.items():
        month = SEQUENCE.sessions[index].session_date.strftime("%Y-%m")
        by_month.setdefault(month, []).append(tranche)

    full_months = [t for m, t in by_month.items() if len(t) == 5]
    assert len(full_months) >= 5, "most months should carry all five rebalances"
    for tranches in full_months:
        assert sorted(tranches) == [0, 1, 2, 3, 4], "each tranche rebalances once per month"


def test_the_last_tranche_still_lands_on_month_end() -> None:
    """Keeps the tranched schedule anchored to the same calendar the untranched one used."""
    schedule = SEQUENCE.tranche_schedule_map(5)
    month_ends = set(_month_end_indices())

    landed = [index for index, tranche in schedule.items() if tranche == 4]
    assert landed, "the final tranche must rebalance"
    assert all(index in month_ends or index == LAST_INDEX for index in landed)


def test_each_tranche_expires_when_that_same_tranche_next_rebalances() -> None:
    """Rosters are owned by a tranche; expiry is that tranche's next turn, not the calendar."""
    schedule = SEQUENCE.tranche_schedule_map(5)
    for tranche in range(5):
        owned = sorted(index for index, owner in schedule.items() if owner == tranche)
        assert len(owned) >= 2
        first, second = owned[0], owned[1]
        expiry = SEQUENCE.tranche_expiry_after(first, tranche=tranche, tranches=5)
        assert expiry == SEQUENCE.sessions[second + 1].open_at


def test_a_roster_defaults_to_the_untranched_identity() -> None:
    """A new field must not change what every deployed version means."""
    roster = MonthlyRoster(
        calendar=CALENDAR,
        evaluated_at=SEQUENCE.sessions[20].close_at,
        effective_at=SEQUENCE.sessions[21].open_at,
        expires_at=SEQUENCE.sessions[40].open_at,
        symbols=("SPY", "QQQ"),
        rankings=(),
    )

    assert roster.tranche == 0
    assert roster.tranche_count == 1


def test_a_roster_can_carry_which_tranche_opened_it() -> None:
    """Exits cannot be attributed without this, and a mis-attributed exit halts trading."""
    roster = MonthlyRoster(
        calendar=CALENDAR,
        evaluated_at=SEQUENCE.sessions[20].close_at,
        effective_at=SEQUENCE.sessions[21].open_at,
        expires_at=SEQUENCE.sessions[40].open_at,
        symbols=("SPY",),
        rankings=(),
        tranche=3,
        tranche_count=5,
    )

    assert (roster.tranche, roster.tranche_count) == (3, 5)
