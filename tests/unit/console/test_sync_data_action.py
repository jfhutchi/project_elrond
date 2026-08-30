"""Fetching market data from the console (#82).

The second safe operational action, after reconciliation. Safe in the specific sense that
matters: it submits no order, cancels nothing, moves no position, and `save_bars` refuses to
revise a bar it already holds, so a repeated sync cannot rewrite history.

It earns a place on this surface because `MARKET_DATA_STALE` halts a trading cycle and the fix
is almost always "fetch the bars" -- which currently needs a terminal.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.storage import Database


@pytest.fixture
def database(tmp_path):  # type: ignore[no-untyped-def]
    with Database.create(tmp_path / "sync.db") as store:
        yield store


def test_the_action_calls_the_injected_sync_and_returns_to_the_page(database) -> None:  # type: ignore[no-untyped-def]
    calls: list[str] = []

    async def sync() -> Mapping[str, object]:
        calls.append("sync")
        return {"ok": True, "bars_stored": 23}

    client = TestClient(create_app(database, sync_data=sync))
    response = client.post("/operations/sync-data", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/market-data"
    assert calls == ["sync"]


def test_a_console_without_credentials_says_so_rather_than_offering_a_dead_control(  # type: ignore[no-untyped-def]
    database,
) -> None:
    """The same rule the reconcile action follows.

    A surface that cannot reach the provider must not offer to fetch from it, and it must say
    why rather than failing with something nobody can read.
    """
    client = TestClient(create_app(database, sync_data=None))
    response = client.post("/operations/sync-data", follow_redirects=False)

    assert response.status_code == 200
    assert "cannot sync market data" in response.text


def test_a_provider_failure_is_reported_rather_than_swallowed(database) -> None:  # type: ignore[no-untyped-def]
    """An action that silently does nothing is worse than one that refuses.

    Without this, a sync that raised would redirect to a page showing the same stale data, and
    an operator would press the button again.
    """

    async def sync() -> Mapping[str, object]:
        raise RuntimeError("provider returned 502")

    client = TestClient(create_app(database, sync_data=sync))
    response = client.post("/operations/sync-data", follow_redirects=False)

    assert response.status_code == 200
    assert "market data sync failed" in response.text
    assert "502" in response.text


def test_the_button_is_on_the_market_data_page(database) -> None:  # type: ignore[no-untyped-def]
    """An action nothing links to is an action nobody takes."""

    async def sync() -> Mapping[str, object]:
        return {"ok": True}

    client = TestClient(create_app(database, sync_data=sync))
    body = client.get("/market-data").text

    assert 'action="/operations/sync-data"' in body
    assert "Sync market data now" in body


def test_the_console_still_has_no_route_that_submits_an_order(database) -> None:  # type: ignore[no-untyped-def]
    """The console's safety story is that its route table is an allowlist.

    Adding a second operational action is exactly when that story is most likely to erode, so it
    is asserted here rather than assumed. Every POST route is enumerated and checked against a
    known-safe set; a new one has to be added deliberately, which is the point.
    """

    async def sync() -> Mapping[str, object]:
        return {"ok": True}

    app = create_app(database, sync_data=sync)
    posts = {
        route.path  # type: ignore[attr-defined]
        for route in app.routes
        if "POST" in getattr(route, "methods", set())
    }

    assert posts == {
        "/kill-switch/engage",
        "/kill-switch/clear",
        "/operations/reconcile",
        "/operations/sync-data",
        "/config/policy/preview",
        "/config/policy/apply",
        "/config/policy/rollback",
    }, "a POST route appeared or moved; confirm it cannot reach the order path"
