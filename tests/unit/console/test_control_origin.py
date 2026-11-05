"""Every console POST requires local, unambiguous browser intent before side effects."""

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.storage import Database

POST_ROUTES = (
    "/kill-switch/engage",
    "/kill-switch/clear",
    "/operations/reconcile",
    "/operations/sync-data",
    "/config/policy/preview",
    "/config/policy/apply",
    "/config/policy/rollback",
    "/learner/alerts/learner/PAPER_PROMOTION_REQUIRED/acknowledge",
)


@pytest.mark.parametrize(
    ("base_url", "peer", "headers"),
    [
        ("http://127.0.0.1", "127.0.0.1", []),
        ("http://127.0.0.1", "127.0.0.1", [("Origin", "null")]),
        ("http://127.0.0.1", "127.0.0.1", [("Origin", "https://foreign.example")]),
        ("http://127.0.0.1", "127.0.0.1", [("Origin", "not-an-origin")]),
        ("http://127.0.0.1", "127.0.0.1", [("Origin", "http://127.0.0.1:8788")]),
        ("http://foreign.example", "127.0.0.1", [("Origin", "http://foreign.example")]),
        ("http://127.0.0.1", "192.0.2.1", [("Origin", "http://127.0.0.1")]),
        ("http://127.0.0.1", None, [("Origin", "http://127.0.0.1")]),
        ("http://127.0.0.1", "127.0.0.1", [("Origin", "https://127.0.0.1")]),
        (
            "http://127.0.0.1",
            "127.0.0.1",
            [("Origin", "http://127.0.0.1"), ("Origin", "https://foreign.example")],
        ),
        (
            "http://127.0.0.1",
            "127.0.0.1",
            [("Origin", "http://127.0.0.1"), ("Host", "127.0.0.1"), ("Host", "foreign.example")],
        ),
    ],
)
def test_invalid_control_origin_precedes_body_database_and_callbacks(
    tmp_path, monkeypatch, base_url, peer, headers
):
    def forbidden(*args, **kwargs):
        pytest.fail("Rejected control request reached a side effect or body parser")

    async def forbidden_async(*args, **kwargs):
        forbidden()

    learner = tmp_path / "absent-learner"
    with Database.create(tmp_path / "console.db") as database:
        app = create_app(
            database,
            measure=forbidden_async,
            reconcile=forbidden_async,
            sync_data=forbidden_async,
            learner_directory=learner,
        )
        monkeypatch.setattr(database, "transaction", forbidden)
        monkeypatch.setattr(Request, "body", forbidden_async)
        client = TestClient(app, base_url=base_url, client=(peer, 50000) if peer else None)
        for route in POST_ROUTES:
            response = client.post(route, headers=headers, data={"reason": "untrusted"})
            assert response.status_code == 403, route
    assert not learner.exists()


@pytest.mark.parametrize(
    ("origin", "peer"),
    [
        ("http://127.0.0.1:8787", "127.0.0.1"),
        ("http://localhost:8787", "127.0.0.1"),
        ("http://[::1]:8787", "::1"),
    ],
)
def test_local_same_origin_control_remains_available(tmp_path, origin, peer):
    calls = []

    async def reconcile():
        calls.append("reconcile")
        return {}

    with Database.create(tmp_path / "console.db") as database:
        # This Starlette/httpx adapter cannot parse IPv6 URL netlocs. The Host
        # header still supplies the actual IPv6 authority to the ASGI request.
        client = TestClient(
            create_app(database, reconcile=reconcile),
            base_url="http://localhost:8787" if peer == "::1" else origin,
            client=(peer, 50000),
            headers={"Origin": origin, "Host": origin.removeprefix("http://")},
        )
        response = client.post("/operations/reconcile", follow_redirects=False)
        assert response.status_code == 303
        assert calls == ["reconcile"]
