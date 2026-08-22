"""The control surface over a real socket (#38).

`test_control.py` covers the rules -- what is authorized, what is asymmetric, what a refusal
says -- against the transport-free module. This covers the wiring: that a real HTTP request
reaches those rules and that the thin layer above them does not undo any of it.

Worth having separately because the failures here are different in kind. A handler that returned
200 on a refusal, or leaked an exception message, or accepted an unbounded body, would pass every
unit test in the other file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest
import scripts.control_server as control_server  # noqa: E402

from quantbot.brokers.base import BrokerHealth
from quantbot.domain import Bar, ReconciliationStatus
from quantbot.operations.control import OperatorControl
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.readiness import measure_readiness
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
ACCOUNT = "paper-1"
TOKEN = "an-adequately-long-control-token"


class FakeBroker:
    def __init__(self, *, healthy: bool) -> None:
        self._healthy = healthy

    async def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=self._healthy,
            provider="alpaca",
            environment="paper",
            account_id=ACCOUNT,
            reasons=() if self._healthy else ("BROKER_UNAVAILABLE",),
        )


def seed(database: Database, *, reconciled_at: datetime) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_reconciliation(
            "reconciliation-1",
            status=ReconciliationStatus.RECONCILED,
            started_at=reconciled_at - timedelta(minutes=1),
            completed_at=reconciled_at,
            diffs=(),
        )
        repository.save_bars(
            [
                Bar(
                    symbol="SPY",
                    timestamp=NOW - timedelta(hours=20),
                    open="100",
                    high="110",
                    low="95",
                    close="105",
                    volume="1000",
                    adjustment="1",
                )
            ],
            provider="alpaca",
        )


@pytest.fixture
def served(tmp_path) -> Iterator[tuple[int, Database]]:
    """A real server on a real port, torn down afterwards."""
    database = Database(tmp_path / "quantbot.db")
    seed(database, reconciled_at=NOW - timedelta(hours=2))

    async def measure():
        return await measure_readiness(
            database,
            FakeBroker(healthy=True),
            paper_mode=True,
            expected_account_id=ACCOUNT,
            risk_caps_valid=True,
            now=NOW,
        )

    control_server.ControlHandler.control = OperatorControl(
        KillSwitchController(database), measure, token=TOKEN, clock=lambda: NOW
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), control_server.ControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], database
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        database.close()


def post(port: int, path: str, *, token: str | None, reason: str = "because") -> tuple[int, dict]:
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers[control_server.TOKEN_HEADER] = token
    connection.request("POST", path, body=json.dumps({"reason": reason}), headers=headers)
    response = connection.getresponse()
    payload = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, payload


def test_engaging_over_http_stops_trading(served) -> None:
    port, database = served

    status, payload = post(port, "/kill-switch/engage", token=TOKEN, reason="looks wrong")

    assert status == 200
    assert payload["engaged"] is True
    assert KillSwitchController(database).get().engaged is True


def test_clearing_over_http_runs_the_probes(served) -> None:
    port, database = served
    post(port, "/kill-switch/engage", token=TOKEN)

    status, payload = post(port, "/kill-switch/clear", token=TOKEN, reason="checked it")

    assert status == 200
    assert payload["engaged"] is False
    assert "BROKER" in payload["probes"], "the probes ran on the far side of the socket"
    assert KillSwitchController(database).get().engaged is False


def test_an_unauthorized_request_is_403_and_changes_nothing(served) -> None:
    """The rule the module enforces, asserted through the transport that could have dropped it."""
    port, database = served
    KillSwitchController(database).engage(reason="baseline", updated_at=NOW)

    status, payload = post(port, "/kill-switch/clear", token="wrong-token-but-long")

    assert status == 403
    assert payload == {"error": "not authorized"}
    state = KillSwitchController(database).get()
    assert state.engaged is True and state.reason == "baseline"


def test_a_missing_token_header_is_refused_identically(served) -> None:
    port, _ = served

    status, payload = post(port, "/kill-switch/engage", token=None)

    assert status == 403
    assert payload == {"error": "not authorized"}


def test_an_oversized_body_is_refused_without_being_read(served) -> None:
    """An unbounded read from an unauthenticated caller is how a small server becomes a problem."""
    port, _ = served
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    oversized = json.dumps({"reason": "x" * (control_server.MAX_BODY_BYTES + 100)})
    connection.request(
        "POST",
        "/kill-switch/engage",
        body=oversized,
        headers={"Content-Type": "application/json", control_server.TOKEN_HEADER: TOKEN},
    )
    response = connection.getresponse()
    response.read()
    connection.close()

    assert response.status == 413


def test_unknown_paths_are_404_and_not_routed(served) -> None:
    """Two actions exist. Anything else must not find a handler by accident."""
    port, _ = served

    for path in ("/kill-switch/promote", "/submit-order", "/../etc/passwd", "/kill-switch"):
        status, _ = post(port, path, token=TOKEN)
        assert status == 404, path


def test_the_control_page_is_served_and_carries_no_token(served) -> None:
    """The page is a form, not a place to leave a credential where a browser can cache it."""
    port, _ = served
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    connection.request("GET", "/")
    response = connection.getresponse()
    body = response.read().decode("utf-8")
    connection.close()

    assert response.status == 200
    assert "Elrond kill switch" in body
    assert TOKEN not in body, "the server token must never reach the page"
    assert response.getheader("Cache-Control") == "no-store"
