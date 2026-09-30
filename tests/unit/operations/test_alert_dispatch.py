from datetime import UTC, datetime
from uuid import uuid4

import pytest

from quantbot.operations.alert_dispatch import DeliveryQueue, GitHubTransport, dispatch_one

NOW = datetime(2026, 10, 1, 14, tzinfo=UTC)


def test_initial_delivery_baselines_history_and_sends_current_only(tmp_path):
    import json
    import sqlite3

    from quantbot.operations.alert_delivery import parse_comment

    source_path = tmp_path / "alerts.db"
    active = dict(code="LEARNER_NO_PROGRESS", severity="CRITICAL",
                  first_seen=NOW.isoformat(), resolved_at=None, acknowledged_at=None)
    recovered = active | {"code": "LEARNER_HEARTBEAT_STALE", "resolved_at": NOW.isoformat()}
    with sqlite3.connect(source_path) as source_db:
        source_db.execute("CREATE TABLE alerts(code TEXT, document TEXT)")
        source_db.execute("CREATE TABLE events(id INTEGER, document TEXT)")
        for event in (active, recovered):
            source_db.execute("INSERT INTO alerts VALUES (?,?)", (event['code'], json.dumps(event)))
        source_db.execute("INSERT INTO events VALUES (100,?)", (json.dumps(recovered),))
    original = source_path.read_bytes()
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        identifier = uuid4()
        queue.enqueue(source(), now=NOW, identifier=identifier)
        dispatch_one(queue, Transport())
        queue.confirm_canary(str(identifier))
        queue.initialize_source(source_path, now=NOW)
        assert parse_comment(queue.pending()[1])["code"] == "LEARNER_NO_PROGRESS"
        queue.ingest(source_path, now=NOW)
        assert queue.connection.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 2
        with pytest.raises(ValueError, match="ALREADY_COMMISSIONED"):
            queue.initialize_source(source_path, now=NOW)
    assert source_path.read_bytes() == original


def source():
    return dict(code="DELIVERY_CANARY", event="CANARY", severity="INFO", first_seen=NOW.isoformat())


class Transport:
    def __init__(self):
        self.comments = {}
        self.posts = 0
        self.fail_after_post = False

    def find(self, identifier, wire):
        return self.comments.get(identifier)

    def post(self, wire):
        import json

        self.posts += 1
        identifier = json.loads(wire.split("\n", 1)[1])["delivery_id"]
        self.comments[identifier] = (
            "https://github.com/jfhutchi/project_elrond/pull/255#issuecomment-1"
        )
        if self.fail_after_post:
            raise OSError("secret network detail")
        return self.comments[identifier]


def test_queue_reopens_with_same_uuid_and_identical_wire(tmp_path):
    identifier = uuid4()
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        wire = queue.enqueue(source(), now=NOW, identifier=identifier)
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        assert queue.pending() == (str(identifier), wire)
        assert queue.enqueue(source(), now=NOW, identifier=identifier) == wire


def test_ambiguous_post_retry_finds_original_and_does_not_duplicate(tmp_path):
    transport = Transport()
    transport.fail_after_post = True
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        queue.enqueue(source(), now=NOW)
        assert dispatch_one(queue, transport) == "RETRY_PENDING"
        assert queue.pending() is not None
        assert dispatch_one(queue, transport) == "COMMENT_CREATED_CHAT_UNCONFIRMED"
        assert transport.posts == 1
        assert queue.pending() is None
        assert dispatch_one(queue, transport) == "NO_PENDING_DELIVERY"


def test_runtime_events_remain_gated_until_explicit_canary_receipt(tmp_path):
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        with pytest.raises(ValueError, match="CHAT_ROUTE_UNCONFIRMED"):
            queue.enqueue(
                dict(
                    code="LEARNER_NO_PROGRESS",
                    event="OPENED",
                    severity="CRITICAL",
                    first_seen=NOW.isoformat(),
                ),
                now=NOW,
            )
        identifier = uuid4()
        queue.enqueue(source(), now=NOW, identifier=identifier)
        with pytest.raises(ValueError, match="CANARY_COMMENT_UNVERIFIED"):
            queue.confirm_canary(str(identifier))
        dispatch_one(queue, Transport())
        queue.confirm_canary(str(identifier))
        queue.enqueue(
            dict(
                code="LEARNER_NO_PROGRESS",
                event="OPENED",
                severity="CRITICAL",
                first_seen=NOW.isoformat(),
            ),
            now=NOW,
        )
        assert queue.pending() is not None


def test_dedup_id_cannot_be_reused_for_changed_content(tmp_path):
    identifier = uuid4()
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        queue.enqueue(source(), now=NOW, identifier=identifier)
        with pytest.raises(ValueError, match="DELIVERY_ID_CONFLICT"):
            queue.enqueue(source(), now=NOW.replace(minute=1), identifier=identifier)


def test_concurrent_dispatch_cannot_post_twice(tmp_path):
    import threading

    path = tmp_path / "queue.db"
    entered, release = threading.Event(), threading.Event()

    class SlowTransport(Transport):
        def find(self, identifier, wire):
            entered.set()
            assert release.wait(3)
            return super().find(identifier, wire)

    transport = SlowTransport()
    with DeliveryQueue(path) as queue:
        queue.enqueue(source(), now=NOW)
    results = []

    def run():
        with DeliveryQueue(path) as queue:
            results.append(dispatch_one(queue, transport))

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(3)
    try:
        with DeliveryQueue(path) as queue:
            queue.connection.execute("PRAGMA busy_timeout=10")
            assert dispatch_one(queue, transport) == "DISPATCH_BUSY"
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results == ["COMMENT_CREATED_CHAT_UNCONFIRMED"]
    assert transport.posts == 1


def test_github_host_is_pinned_despite_ambient_enterprise_host(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("GH_HOST", "unapproved.example")
    monkeypatch.setenv("ALPACA_PAPER_API_SECRET", "must-not-reach-transport")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        assert "ALPACA_PAPER_API_SECRET" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setattr("quantbot.operations.alert_dispatch.subprocess.run", run)
    transport = object.__new__(GitHubTransport)
    transport.executable = "gh"
    transport._api("user")
    assert calls[0][1:4] == ["api", "--hostname", "github.com"]


def test_scheduled_checkout_check_refuses_wrong_pin_without_queue_write(tmp_path, monkeypatch):
    from quantbot.operations.alert_dispatch import main

    monkeypatch.setattr("quantbot.operations.alert_dispatch._git", lambda *args: "a" * 40)
    path = tmp_path / "queue.db"
    assert main(["--queue", str(path), "--commit", "b" * 40, "--check"]) == 1
    assert not path.exists()


def test_source_database_cannot_be_initialized_as_queue(tmp_path):
    import sqlite3

    from quantbot.operations.alert_dispatch import main

    path = tmp_path / "source.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, document TEXT)")
    original = path.read_bytes()
    assert main(["--queue", str(path), "--events-database", str(path)]) == 1
    assert path.read_bytes() == original
    with pytest.raises(ValueError, match="DELIVERY_QUEUE_SCHEMA_REFUSED"):
        DeliveryQueue(path)
    assert path.read_bytes() == original


def test_ingestion_is_read_only_bounded_and_idempotent(tmp_path):
    import json
    import sqlite3

    path = tmp_path / "alerts.db"
    raw = dict(
        code="LEARNER_NO_PROGRESS",
        event="OPENED",
        severity="CRITICAL",
        first_seen=NOW.isoformat(),
        action="secret",
        account_value=12345,
    )
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, document TEXT)")
        connection.executemany(
            "INSERT INTO events VALUES (?, ?)", [(i, json.dumps(raw)) for i in range(1, 26)]
        )
    original = path.read_bytes()
    with DeliveryQueue(tmp_path / "queue.db") as queue:
        identifier = uuid4()
        queue.enqueue(source(), now=NOW, identifier=identifier)
        dispatch_one(queue, Transport())
        queue.confirm_canary(str(identifier))
        queue.ingest(path, now=NOW)
        assert queue.connection.execute("SELECT COUNT(*) FROM ingested").fetchone()[0] == 20
        queue.ingest(path, now=NOW)
        queue.ingest(path, now=NOW)
        wires = queue.connection.execute("SELECT wire FROM deliveries").fetchall()
        assert len(wires) == 26
        assert all("12345" not in wire and "secret" not in wire for (wire,) in wires)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "response",
    [
        {"login": "wrong", "type": "User"},
        {"login": "jfhutchi", "type": "Bot"},
    ],
)
def test_transport_refuses_unexpected_existing_identity(monkeypatch, response):
    monkeypatch.setattr("quantbot.operations.alert_dispatch.shutil.which", lambda _: "gh")
    monkeypatch.setattr(GitHubTransport, "_api", lambda self, endpoint: response)
    with pytest.raises(ValueError, match="GITHUB_ORIGIN_REFUSED"):
        GitHubTransport()
