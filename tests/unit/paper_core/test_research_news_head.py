"""Current news remains observable while historical pagination catches up."""

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from quantbot.paper_core.discovery import DiscoveryStore
from quantbot.paper_core.discovery_research import research_context, research_once

NOW = datetime(2026, 11, 6, 20, 55, tzinfo=UTC)


def article(identity, at):
    return dict(
        id=identity,
        created_at=at.isoformat(),
        updated_at=at.isoformat(),
        headline="Company earnings",
        symbols=["XLE"],
        url="https://example.com/news",
    )


async def test_head_precedes_old_cursor_preserves_first_seen_and_throttles(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTBOT_FREE_NEWS_CONFIRMED", "1")
    state = DiscoveryStore(tmp_path / "research.db")
    old_end = NOW - timedelta(hours=1)
    job = dict(
        start=(NOW - timedelta(hours=48)).isoformat(),
        end=old_end.isoformat(),
        token="backlog-1",
        seen=[],
    )
    with state.connection() as connection:
        state._set(connection, "research_news_job", job)
    calls = []

    async def request(params, *, endpoint):
        calls.append(dict(params))
        if params["sort"] == "desc":
            payload = {
                "news": [article("current", NOW - timedelta(seconds=30))],
                "next_page_token": "head-truncated",
            }
        else:
            payload = {
                "news": [article("old", old_end - timedelta(hours=30))],
                "next_page_token": "backlog-" + str(len(calls)),
            }
        return SimpleNamespace(content=json.dumps(payload).encode())

    broker = SimpleNamespace(now=NOW, market=SimpleNamespace(_request_page=request))
    await research_once(broker, state)
    assert [call["sort"] for call in calls] == ["desc", "asc"]
    assert "page_token" not in calls[0]
    assert calls[1]["page_token"] == "backlog-1"
    assert calls[1]["start"] == job["start"] and calls[1]["end"] == job["end"]
    assert datetime.fromisoformat(calls[0]["start"]) >= NOW - timedelta(minutes=5)
    with state.connection() as connection:
        original = connection.execute("select id,payload from research_documents").fetchall()
        retained_job = state._get(connection, "research_news_job")
        head = state._get(connection, "research_news_head")
    assert len(original) == 2
    assert head["coverage_truncated"] is True
    assert retained_job["token"] == "backlog-2" and retained_job["end"] == job["end"]
    refs, gaps = research_context(state, ("XLE",), now=NOW)
    assert refs
    assert "NEWS_HEAD_WINDOW_TRUNCATED" in gaps
    assert "NEWS_WINDOW_PAGINATION_INCOMPLETE" in gaps
    assert "NEWS_QUERY_END_STALE" not in gaps
    broker.now += timedelta(seconds=59)
    await research_once(broker, state)
    assert len(calls) == 2
    broker.now += timedelta(seconds=1)
    await research_once(broker, state)
    assert [call["sort"] for call in calls] == ["desc", "asc", "desc", "asc"]
    assert calls[3]["page_token"] == "backlog-2"
    with state.connection() as connection:
        assert (
            connection.execute("select id,payload from research_documents").fetchall() == original
        )
        first_seen = connection.execute("select received_at from research_symbols").fetchall()
    assert first_seen == [(NOW.isoformat(),), (NOW.isoformat(),)]


async def test_failed_head_does_not_stop_historical_page_or_bypass_throttle(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANTBOT_FREE_NEWS_CONFIRMED", "1")
    state = DiscoveryStore(tmp_path / "research.db")
    calls = []

    async def request(params, *, endpoint):
        calls.append(dict(params))
        if params["sort"] == "desc":
            raise OSError("offline current news")
        return SimpleNamespace(content=b'{"news": [], "next_page_token": null}')

    broker = SimpleNamespace(now=NOW, market=SimpleNamespace(_request_page=request))
    await research_once(broker, state)
    await research_once(broker, state)
    assert [call["sort"] for call in calls] == ["desc", "asc"]
    with state.connection() as connection:
        assert state._get(connection, "research_news_completed") == NOW.isoformat()
