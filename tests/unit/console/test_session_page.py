"""The market-session page: liveness, and honesty about what is not measured (#82, #143).

Rendered rather than only read. Three defects earlier on this branch -- a stale danger strip, a
`frozenset(...)` repr in a text box, and every input announcing itself as unchanged to a screen
reader -- were invisible to state-level assertions and obvious in the HTML.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.state import read_console_state
from quantbot.storage import Database
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import session_observations

NOW = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path):  # type: ignore[no-untyped-def]
    with Database.create(tmp_path / "session.db") as store:
        yield store


def observe(database, at, outcome="HEALTHY", reasons="", market_open=1) -> None:  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        session.execute(
            session_observations.insert().values(
                observation_id=f"observation-{encode_utc(at)}",
                observed_at=encode_utc(at),
                market_open=market_open,
                outcome=outcome,
                reasons=reasons,
                market_data_age_seconds=None,
                detail="{}",
            )
        )


def panel(database):  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        return read_console_state(session, now=NOW).session


def render(database) -> str:  # type: ignore[no-untyped-def]
    client = TestClient(create_app(database))
    response = client.get("/session")
    assert response.status_code == 200
    return response.text


def test_counts_come_from_the_whole_table_not_the_listed_rows(database) -> None:  # type: ignore[no-untyped-def]
    """A page showing 32 rows must still say honestly how many there have been.

    Deriving the total from the listed rows would silently cap it at the page size, so a system
    that had observed four hundred times would report thirty-two -- and the number an operator
    uses to judge whether the loop is running would be a property of the template.
    """
    for minute in range(40):
        observe(database, NOW - timedelta(minutes=15 * minute))

    result = panel(database)

    assert result.total == 40
    assert len(result.observations) == 32, "the list is capped"
    assert result.healthy == 40


def test_halted_is_counted_apart_from_degraded(database) -> None:  # type: ignore[no-untyped-def]
    """A deliberate stop is not a fault.

    Reporting a kill switch as a failure invites an operator to clear it to make the warning go
    away, which is the one thing CLAUDE.md forbids outright.
    """
    observe(database, NOW, outcome="HALTED", reasons="KILL_SWITCH_ENGAGED")
    observe(database, NOW - timedelta(minutes=15), outcome="DEGRADED", reasons="MARKET_DATA_STALE")
    observe(database, NOW - timedelta(minutes=30))

    result = panel(database)

    assert (result.healthy, result.degraded, result.halted) == (1, 1, 1)


def test_the_newest_heartbeat_is_first(database) -> None:  # type: ignore[no-untyped-def]
    """Otherwise "market state at the last heartbeat" reads the oldest row."""
    observe(database, NOW - timedelta(hours=6), market_open=0)
    observe(database, NOW, market_open=1)

    result = panel(database)

    assert result.observations[0].observed_at == NOW
    assert result.market_open_at_last_heartbeat is True


def test_never_observed_is_not_reported_as_a_closed_market(database) -> None:  # type: ignore[no-untyped-def]
    """`None` and `False` are different facts.

    A store upgraded a minute ago has no heartbeats, and saying "closed" would be asserting
    something nothing measured.
    """
    result = panel(database)

    assert result.ever_observed is False
    assert result.market_open_at_last_heartbeat is None


def test_the_page_renders_the_heartbeats_and_their_reasons(database) -> None:  # type: ignore[no-untyped-def]
    """Rendered, because state-level assertions cannot see a broken template."""
    observe(database, NOW, outcome="DEGRADED", reasons="MARKET_DATA_STALE,RISK_ENGINE_UNHEALTHY")

    body = render(database)

    assert "Market session" in body
    assert "degraded" in body
    assert "MARKET_DATA_STALE, RISK_ENGINE_UNHEALTHY" in body
    assert "frozenset" not in body, "a Python repr reached the page"


def test_an_unobserved_store_says_so_rather_than_claiming_the_daemon_is_stopped(database) -> None:  # type: ignore[no-untyped-def]
    """The distinction the page exists to keep straight."""
    body = render(database)

    assert "never run here" in body
    assert "not</strong> evidence that the daemon is stopped" in body


def test_the_page_says_what_is_not_measured_instead_of_showing_zeros(database) -> None:  # type: ignore[no-untyped-def]
    """An empty widget looks like a system with nothing to report.

    No experiment has ever run, no worker has been dispatched and no budget has been spent, so a
    queue-depth or budget gauge would read zero for a mechanism that was never wired -- which an
    operator reads as health. The page names the absence instead.
    """
    observe(database, NOW)

    body = render(database)

    assert "Not built yet, and not shown as empty" in body
    assert "None of that exists" in body
    assert "Intraday event detection additionally needs intraday data" in body


def test_the_page_states_the_loop_cannot_trade(database) -> None:  # type: ignore[no-untyped-def]
    """The safety property an operator most needs to be able to check without reading code."""
    observe(database, NOW)

    body = render(database)

    assert "cannot place an order" in body
    assert "no route from watching a session to trading in one" in body


def test_the_session_page_is_reachable_from_the_navigation(database) -> None:  # type: ignore[no-untyped-def]
    """A page nothing links to is a page nobody opens."""
    body = render(database)

    assert 'href="/session"' in body
