import json

import httpx
import pytest
from scripts import intraday_research as subject


def test_collection_paginates_and_preserves_only_exchange_session_bars(tmp_path, monkeypatch):
    env = tmp_path / "paper.env"
    env.write_text(
        "TRADING_MODE=PAPER\nALPACA_PAPER_API_KEY=fake\nALPACA_PAPER_API_SECRET=fake-secret\n"
    )
    requested = []

    def row(timestamp):
        return {"t": timestamp, "o": 100, "h": 102, "l": 99, "c": 101, "v": 1000}

    def respond(request):
        requested.append(request)
        if request.url.path.endswith("calendar"):
            return httpx.Response(
                200, json=[{"date": "2026-03-02", "open": "09:30", "close": "16:00"}]
            )
        if "page_token" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "bars": {
                        "SPY": [
                            row("2026-03-02T14:45:00Z"),
                            row("2026-03-02T14:15:00Z"),
                            row("2026-03-02T21:00:00Z"),
                        ]
                    },
                    "next_page_token": "next",
                },
            )
        return httpx.Response(
            200, json={"bars": {"SPY": [row("2026-03-02T14:30:00Z")]}, "next_page_token": None}
        )

    original = httpx.Client
    monkeypatch.setattr(
        subject.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    bundle = tmp_path / "input.json"
    subject.collect(env, bundle)
    document = json.loads(bundle.read_text())
    assert len(requested) == 3
    assert document["content_hash"] == subject.digest(document["content"])
    rows = document["content"]["histories"]["SPY"]
    assert [row["timestamp"] for row in rows] == ["2026-03-02T14:30:00Z", "2026-03-02T14:45:00Z"]
    assert "fake-secret" not in bundle.read_text()
    assert document["content"]["feed"] == "sip"
    with pytest.raises(ValueError, match="already exists"):
        subject.collect(env, bundle)
    assert len(requested) == 3


def test_changed_input_is_refused_before_opening_a_database(tmp_path):
    bundle = tmp_path / "input.json"
    bundle.write_text(json.dumps({"content": {"rule": "edited"}, "content_hash": "wrong"}))
    with pytest.raises(ValueError, match="hash differs"):
        subject.measure(bundle, tmp_path / "must-not-exist.db", tmp_path / "result.json")
    assert not (tmp_path / "must-not-exist.db").exists()
    assert not (tmp_path / "result.json").exists()


def test_failed_measurement_remains_charged_and_cannot_be_replayed(tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from quantbot.research.registry import HypothesisRegistry
    from quantbot.storage import Database
    from quantbot.storage.schema import search_runs

    content = {
        "rule": subject.RULE,
        "symbols": list(subject.SYMBOLS),
        "start": subject.START,
        "end": subject.END,
        "feed": "sip",
        "timeframe": "15Min",
        "adjustment": "raw",
        "histories": {},
        "sessions": [],
    }
    bundle = tmp_path / "input.json"
    bundle.write_text(json.dumps({"content": content, "content_hash": subject.digest(content)}))
    database = tmp_path / "research.db"
    with Database.create(database) as db:
        with db.transaction() as session:
            before = HypothesisRegistry(session).cumulative_trials()

    called = []

    def fail(*args, **kwargs):
        called.append(True)
        raise RuntimeError("interrupted computation")

    monkeypatch.setattr("quantbot.research.intraday.simulate", fail)
    with pytest.raises(RuntimeError, match="interrupted computation"):
        subject.measure(bundle, database, tmp_path / "result.json")
    with pytest.raises(ValueError, match="attempt already charged"):
        subject.measure(bundle, database, tmp_path / "result.json")
    assert len(called) == 1
    assert not (tmp_path / "result.json").exists()
    with Database(database) as db:
        with db.transaction() as session:
            assert session.scalar(select(func.count()).select_from(search_runs)) == 1
            assert HypothesisRegistry(session).cumulative_trials() == before + 1
