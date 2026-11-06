from datetime import UTC, datetime

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.operations.alerts import AlertStore, Condition
from quantbot.storage import Database


def test_same_origin_local_acknowledgement_keeps_gate_active(tmp_path):
    learner = tmp_path / "learner"
    path = learner / "state" / "alerts.db"
    with AlertStore(path) as s:
        s.update(
            [
                Condition(
                    "PAPER_PROMOTION_REQUIRED",
                    "paper_entry",
                    "CRITICAL",
                    "OPERATOR_ACTION_REQUIRED",
                    "Operator",
                    "Review exact evidence.",
                )
            ],
            now=datetime.now(UTC),
        )
    with Database.create(tmp_path / "console.db") as database:
        app = create_app(database, learner_directory=learner)
        with TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
            route = "/learner/alerts/learner/PAPER_PROMOTION_REQUIRED/acknowledge"
            assert (
                client.post(route, headers={"Origin": "https://untrusted.example"}).status_code
                == 403
            )
            assert client.post(route).status_code == 403
            assert (
                client.post(
                    route, headers={"Origin": "http://127.0.0.1"}, follow_redirects=False
                ).status_code
                == 303
            )
            page = client.post(route, headers={"Origin": "http://127.0.0.1"})
            assert "Acknowledgement saved" in page.text
            assert "condition remains active until measured recovery" in page.text
    with AlertStore(path) as s:
        alert = s.active()[0]
        assert alert["acknowledged_at"] is not None
        assert alert["resolved_at"] is None


def test_acknowledgement_never_creates_store_and_refuses_remote_client(tmp_path):
    learner = tmp_path / "learner"
    with Database.create(tmp_path / "console.db") as database:
        app = create_app(database, learner_directory=learner)
        route = "/learner/alerts/monitor/PROTECTION_HEARTBEAT_STALE/acknowledge"
        with (
            TestClient(app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as local,
            TestClient(app, base_url="http://127.0.0.1", client=("192.0.2.1", 50000)) as remote,
        ):
            headers = {"Origin": "http://127.0.0.1"}
            assert remote.post(route, headers=headers).status_code == 403
            assert local.post(route, headers=headers).status_code == 404
            assert not learner.exists()
