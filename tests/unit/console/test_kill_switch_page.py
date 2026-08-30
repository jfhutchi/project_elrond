"""Stopping and resuming from the console, and why only one of them is easy (#82).

Engaging is ungated. Stopping is never the dangerous direction, and a control that made it hard
would be worse than useless in the moment somebody reaches for it.

Clearing cannot be faked from this surface, and that property predates the console:
`KillSwitchController.clear` accepts only a `MeasuredReadiness`, and the token that makes one is
private to the module that owns the gate. So the page can *ask* whether it is safe to resume and
relay the answer; it cannot assert it. That is what makes adding the button safe -- the gate is
the measurement, not the caller.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.brokers.base import BrokerHealth
from quantbot.console.app import create_app
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.readiness import measure_readiness
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def health(healthy: bool) -> BrokerHealth:
    return BrokerHealth(
        healthy=healthy,
        provider="alpaca",
        environment="paper",
        account_id="acct",
        reasons=() if healthy else ("the broker reported itself unhealthy",),
    )


class HealthyBroker:
    async def health_check(self) -> BrokerHealth:
        return health(True)


class SickBroker:
    async def health_check(self) -> BrokerHealth:
        return health(False)


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "kill.db")
    try:
        yield db
    finally:
        db.close()


def probe_for(database: Database, broker: object | None, *, caps: bool = True):
    async def probe():
        return await measure_readiness(
            database,
            broker,  # type: ignore[arg-type]
            paper_mode=True,
            expected_account_id="acct",
            risk_caps_valid=caps,
            now=NOW,
        )

    return probe


def engaged(database: Database, reason: str = "halted for a reason") -> None:
    KillSwitchController(database).engage(reason=reason, updated_at=NOW)


def state(database: Database):
    with database.transaction() as session:
        return StorageRepository(session).get_kill_switch_state()


# --- stopping is easy ---------------------------------------------------------------------------


def test_engaging_needs_only_a_reason(database: Database) -> None:
    """No measurement, no probe, no ceremony. Stopping must never be gated."""
    client = TestClient(create_app(database, measure=None))

    response = client.post(
        "/kill-switch/engage", data={"reason": "unexpected fills"}, follow_redirects=False
    )

    assert response.status_code == 303
    assert state(database).engaged is True
    assert state(database).reason == "unexpected fills"


def test_engaging_without_a_reason_is_refused(database: Database) -> None:
    """An engagement nobody can explain is one nobody can safely clear later.

    A fresh store is already engaged -- the kill switch is fail-closed, and a store with no
    recorded state reads as stopped. So the assertion is that *nothing was recorded*, not that
    the switch is off: asserting `engaged is False` here would be asserting against the safe
    default rather than against this route.
    """
    before = state(database)
    client = TestClient(create_app(database, measure=None))

    response = client.post("/kill-switch/engage", data={"reason": "  "})

    assert response.status_code == 200
    assert "needs a reason" in response.text
    assert state(database).reason == before.reason, "a blank reason was recorded"


# --- resuming is not ------------------------------------------------------------------------------


def test_a_console_that_cannot_measure_does_not_offer_to_clear(database: Database) -> None:
    """A button that refuses for reasons nobody can read is worse than a page explaining why
    there is no button.
    """
    engaged(database)
    client = TestClient(create_app(database, measure=None))

    body = client.get("/kill-switch").text

    assert "cannot measure readiness" in body
    assert 'action="/kill-switch/clear"' not in body


def test_clearing_without_a_probe_is_refused_even_if_posted_directly(
    database: Database,
) -> None:
    """The form is hidden, which is not the same as the route being closed.

    Anything can POST to a local port, so the absence of a button is a UI convenience and the
    refusal is the control.
    """
    engaged(database)
    client = TestClient(create_app(database, measure=None))

    response = client.post("/kill-switch/clear", data={"reason": "looks fine to me"})

    assert response.status_code == 200
    assert "cannot measure readiness" in response.text
    assert state(database).engaged is True


def test_an_unhealthy_broker_blocks_the_clear_and_says_which_condition(
    database: Database,
) -> None:
    """The refusal is the useful part. An operator needs to know what to go and fix."""
    engaged(database)
    client = TestClient(
        create_app(database, measure=probe_for(database, SickBroker()))
    )

    response = client.post("/kill-switch/clear", data={"reason": "trying anyway"})

    assert response.status_code == 200
    assert "conditions for resuming are not met" in response.text
    assert "broker" in response.text.lower(), "the operator is told which condition failed"
    assert state(database).engaged is True, "a blocked clear changed nothing"


def test_an_unprobed_condition_blocks_exactly_as_an_unmet_one_does(
    database: Database,
) -> None:
    """Not knowing is not the same as being fine.

    A console started without a broker cannot establish broker health. Treating that as
    satisfied is how a system resumes into the fault that stopped it.
    """
    engaged(database)
    client = TestClient(create_app(database, measure=probe_for(database, None)))

    response = client.post("/kill-switch/clear", data={"reason": "no broker configured"})

    assert response.status_code == 200
    assert "nobody could probe this" in response.text
    assert state(database).engaged is True


def test_clearing_without_a_reason_is_refused(database: Database) -> None:
    """The reason is recorded permanently beside the change; there is no blank one to record."""
    engaged(database)
    client = TestClient(
        create_app(database, measure=probe_for(database, HealthyBroker()))
    )

    response = client.post("/kill-switch/clear", data={"reason": ""})

    assert response.status_code == 200
    assert "needs a reason" in response.text
    assert state(database).engaged is True


def test_the_page_never_claims_it_can_reach_live_trading(database: Database) -> None:
    """The kill switch decides whether *paper* orders are placed.

    Worth stating on the page rather than assuming a reader knows: this is the most
    consequential-looking control in the console, and the one most likely to be mistaken for a
    capital control.
    """
    client = TestClient(create_app(database, measure=None))

    body = client.get("/kill-switch").text

    assert "can enable live trading or move real capital" in body
    assert "Nothing on this page" in body


def test_the_route_table_gains_exactly_two_mutations(database: Database) -> None:
    """Asserted here as well as in the allowlist, because this is the change that added them.

    A future edit that adds a third kill-switch route should have to come past this test and the
    allowlist both.
    """
    client = TestClient(create_app(database, measure=None))
    routes = {
        (route.path, method)
        for route in client.app.routes
        for method in getattr(route, "methods", set())
        if "kill-switch" in route.path
    }

    assert routes == {
        ("/kill-switch", "GET"),
        ("/kill-switch/engage", "POST"),
        ("/kill-switch/clear", "POST"),
    }
