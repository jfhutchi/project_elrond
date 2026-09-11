from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.market_status import MarketStatusCache, assess_market
from quantbot.console.state import read_console_state
from quantbot.domain.models import MarketClock
from quantbot.storage import Database
from quantbot.strategy import XNYSSession


def stamp(text):
    return datetime.fromisoformat(text).astimezone(UTC)


def measured(now, *, opened=True, sessions=None):
    if sessions is None:
        sessions = (
            XNYSSession(
                session_date=now.date(),
                open_at=now.replace(hour=13, minute=30, second=0, microsecond=0),
                close_at=now.replace(hour=20, minute=0, second=0, microsecond=0),
            ),
        )
    return assess_market(
        MarketClock(
            timestamp=now,
            is_open=opened,
            next_open=now + timedelta(days=1),
            next_close=now + timedelta(hours=6),
        ),
        sessions,
        now=now,
    )


@pytest.mark.parametrize(
    "instant,opened,expected",
    [
        ("2026-09-11T13:29:00Z", False, False),
        ("2026-09-11T13:30:00Z", True, True),
        ("2026-09-11T20:00:00Z", False, False),
    ],
)
def test_regular_session_boundaries(instant, opened, expected):
    status = measured(stamp(instant), opened=opened)
    assert status.expected_open is expected
    assert status.reported_open is opened
    assert status.warning is None


@pytest.mark.parametrize("instant", ["2026-09-12T15:00:00Z", "2026-12-25T15:00:00Z"])
def test_calendar_holiday_or_weekend_is_normal_closed(instant):
    status = measured(stamp(instant), opened=False, sessions=())
    assert status.label == "Market closed"
    assert status.warning is None


@pytest.mark.parametrize(
    "instant,open_time,close_time,opened",
    [
        ("2026-11-27T18:00:00Z", "09:30:00-05:00", "13:00:00-05:00", False),
        ("2026-11-02T14:30:00Z", "09:30:00-05:00", "16:00:00-05:00", True),
    ],
)
def test_calendar_handles_early_close_and_standard_time(instant, open_time, close_time, opened):
    now = stamp(instant)
    day = now.date().isoformat()
    sessions = (
        XNYSSession(
            session_date=now.date(),
            open_at=stamp(day + "T" + open_time),
            close_at=stamp(day + "T" + close_time),
        ),
    )
    assert measured(now, opened=opened, sessions=sessions).expected_open is opened


def test_unexpected_closed_report_needs_attention():
    status = measured(stamp("2026-09-11T15:00:00Z"), opened=False)
    assert status.label == "Market closed"
    assert "scheduled to be open" in status.warning


def test_old_clock_is_not_current_status():
    now = stamp("2026-09-11T15:00:00Z")
    clock = MarketClock(
        timestamp=now - timedelta(minutes=10),
        is_open=False,
        next_open=now + timedelta(days=1),
        next_close=now + timedelta(hours=5),
    )
    assert assess_market(clock, (), now=now).label == "Market status unavailable"


@pytest.mark.parametrize("ahead,available", [(0.02, True), (10, False)])
def test_small_broker_clock_skew_is_tolerated_but_not_large_skew(ahead, available):
    now = stamp("2026-09-11T15:00:00Z")
    clock = MarketClock(
        timestamp=now + timedelta(seconds=ahead),
        is_open=False,
        next_open=now + timedelta(days=1),
        next_close=now + timedelta(hours=5),
    )
    assert (assess_market(clock, (), now=now).reported_open is not None) is available


@pytest.mark.parametrize(
    "instant,opened,should_warn",
    [
        ("2026-09-11T12:00:00Z", False, False),
        ("2026-09-11T13:40:00Z", True, False),
        ("2026-09-11T14:15:00Z", True, True),
    ],
)
def test_overnight_silence_only_warns_after_missed_open_session_checks(
    tmp_path, instant, opened, should_warn
):
    now = stamp(instant)
    with Database.create(tmp_path / "console.db") as db:
        with db.transaction() as session:
            state = read_console_state(session, now=now, market=measured(now, opened=opened))
        health = state.health.model_copy(
            update={
                "last_observation_at": now - timedelta(days=1),
                "minutes_since_last_observation": 1440,
                "open_positions": 1,
                "last_protection_at": now - timedelta(days=1),
                "minutes_since_last_protection": 1440,
            }
        )
        state = state.model_copy(update={"health": health})
        assert any("market-hours loop" in item for item in state.attention) is should_warn
        assert any("position-protection loop" in item for item in state.attention) is should_warn


async def test_cache_bounds_failure_and_does_not_expose_errors():
    async def fail():
        raise ValueError("secret-account-and-key")

    cache = MarketStatusCache(fail)
    status = await cache.refresh()
    assert status.label == "Market status unavailable"
    assert "secret" not in status.model_dump_json()


async def test_cache_reuses_fresh_measurement_and_refreshes_at_open():
    now = stamp("2026-09-11T13:29:59Z")
    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        return measured(now, opened=now.minute == 30)

    cache = MarketStatusCache(probe, now=lambda: now)
    assert (await cache.refresh()).reported_open is False
    await cache.refresh()
    assert calls == 1
    now += timedelta(seconds=1)
    assert (await cache.refresh()).reported_open is True
    assert calls == 2


def test_page_and_api_show_same_market_measurement(tmp_path):
    async def probe():
        return measured(datetime.now(UTC), opened=False, sessions=())

    db = Database.create(tmp_path / "console.db")
    try:
        with TestClient(create_app(db, market_status=probe)) as client:
            assert "Market closed" in client.get("/").text
            assert client.get("/api/state").json()["market"]["reported_open"] is False
    finally:
        db.close()


@pytest.mark.parametrize("days_old,warning", [(0, True), (1, False)])
def test_only_current_session_closed_log_warns(tmp_path, days_old, warning):
    from quantbot.console.state import ObservationRow, SessionPanel

    now = stamp("2026-09-11T15:00:00Z")
    at = now - timedelta(days=days_old, minutes=1)
    with Database.create(tmp_path / "console.db") as db:
        with db.transaction() as session:
            state = read_console_state(session, now=now, market=measured(now))
    state = state.model_copy(
        update={
            "health": state.health.model_copy(update={"last_observation_at": at}),
            "session": SessionPanel(
                observations=(ObservationRow(observed_at=at, market_open=False, outcome="HEALTHY"),)
            ),
        }
    )
    assert any("recorded the market closed" in item for item in state.attention) is warning


async def test_slow_probe_times_out_and_releases_resources(monkeypatch):
    import asyncio

    import quantbot.console.market_status as module

    monkeypatch.setattr(module, "PROBE_TIMEOUT_SECONDS", 0.02)
    cleaned = False

    async def slow():
        nonlocal cleaned
        try:
            await asyncio.Event().wait()
        finally:
            cleaned = True

    cache = MarketStatusCache(slow)
    assert (await cache.refresh()).reported_open is None
    assert cleaned


async def test_concurrent_requests_share_one_probe():
    import asyncio

    calls = 0

    async def probe():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return measured(datetime.now(UTC), opened=False, sessions=())

    cache = MarketStatusCache(probe)
    statuses = await asyncio.gather(*(cache.refresh() for _ in range(8)))
    assert calls == 1
    assert all(status.reported_open is False for status in statuses)


async def test_refresh_failure_does_not_keep_old_closed_status():
    now = stamp("2026-09-11T13:29:59Z")

    async def probe():
        if now.minute == 30:
            raise ValueError("unreachable")
        return measured(now, opened=False)

    cache = MarketStatusCache(probe, now=lambda: now)
    assert (await cache.refresh()).reported_open is False
    now += timedelta(seconds=1)
    assert (await cache.refresh()).reported_open is None


@pytest.mark.parametrize("boundary", ["2026-09-11T13:30:00Z", "2026-09-11T20:00:00Z"])
@pytest.mark.parametrize("skew", [-0.02, 0.02])
def test_clock_skew_cannot_carry_status_across_session_boundary(boundary, skew):
    edge = stamp(boundary)
    now = edge - timedelta(seconds=skew / 2)
    broker_at = now + timedelta(seconds=skew)
    session = XNYSSession(
        session_date=edge.date(),
        open_at=stamp("2026-09-11T13:30:00Z"),
        close_at=stamp("2026-09-11T20:00:00Z"),
    )
    clock = MarketClock(
        timestamp=broker_at,
        is_open=session.open_at <= broker_at < session.close_at,
        next_open=edge + timedelta(days=1),
        next_close=edge + timedelta(days=1),
    )
    assert assess_market(clock, (session,), now=now).reported_open is None


@pytest.mark.parametrize(
    "boundary,opened",
    [
        ("2026-09-11T13:30:00Z", False),
        ("2026-09-11T20:00:00Z", True),
    ],
)
def test_cached_measurement_expires_at_exact_boundary(boundary, opened):
    edge = stamp(boundary)
    status = measured(edge - timedelta(seconds=1), opened=opened)
    assert status.current(edge - timedelta(microseconds=1)).reported_open is opened
    assert status.current(edge).reported_open is None
