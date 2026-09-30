import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantbot.console.performance_reader import (
    exchange_today,
    read_configured_performance,
    read_performance,
)
from quantbot.operations.daily_performance import CloseLedger
from tests.unit.operations.test_daily_performance import evidence

NOW = datetime(2026, 11, 27, 18, 3, tzinfo=UTC)


def bundle(path: Path):
    path.mkdir(exist_ok=True)
    CloseLedger(path / "closes.db", create=True).finalize(evidence())
    document = {
        "calendar": "XNYS",
        "start": "2026-11-25",
        "end": "2026-12-31",
        "sessions": [
            {
                "session_date": "2026-11-25",
                "open_at": "2026-11-25T14:30:00Z",
                "close_at": "2026-11-25T21:00:00Z",
            },
            {
                "session_date": "2026-11-27",
                "open_at": "2026-11-27T14:30:00Z",
                "close_at": "2026-11-27T18:00:00Z",
            },
        ],
    }
    (path / "calendar.json").write_text(json.dumps(document))
    return document


def test_valid_final_is_read_only_and_authoritative(tmp_path, monkeypatch):
    bundle(tmp_path)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}
    real_connect = sqlite3.connect
    calls = []

    def connect(path, **kwargs):
        calls.append((path, kwargs))
        return real_connect(path, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    panel = read_performance(tmp_path, now=NOW)
    assert panel.available
    assert str(panel.rows[0].net_pnl) == "10"
    assert panel.calendar_start == date(2026, 11, 25)
    assert date(2026, 11, 27) in panel.session_dates
    assert calls and calls[0][0].endswith("?mode=ro") and calls[0][1]["uri"]
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.iterdir()}


def test_missing_files_never_created(tmp_path):
    path = tmp_path / "absent"
    assert not read_performance(path, now=NOW).available
    assert not path.exists()
    assert not read_configured_performance({}, now=NOW).available
    assert not read_configured_performance(
        {"QUANTBOT_DAILY_PERFORMANCE_DIRECTORY": "relative"}, now=NOW
    ).available


@pytest.mark.parametrize("change", ["stale", "extra", "duplicate", "naive", "close_mismatch"])
def test_bad_calendar_fails_closed(tmp_path, change):
    document = bundle(tmp_path)
    if change == "stale":
        document["end"] = "2026-11-26"
    if change == "extra":
        document["secret"] = "DO_NOT_DISPLAY"
    if change == "duplicate":
        document["sessions"].append(document["sessions"][0])
    if change == "naive":
        document["sessions"][0]["open_at"] = "2026-11-25T14:30:00"
    if change == "close_mismatch":
        document["sessions"][1]["close_at"] = "2026-11-27T19:00:00Z"
    (tmp_path / "calendar.json").write_text(json.dumps(document))
    result = read_performance(tmp_path, now=NOW)
    assert not result.available and not result.rows
    assert "DO_NOT_DISPLAY" not in str(result)


def test_status_codes_only_and_stale_status_ignored(tmp_path):
    bundle(tmp_path)
    status = tmp_path / "capture-status.json"
    status.write_text(
        json.dumps({"session_date": "2026-11-25", "status": "INVALID", "reason": "FLOW_UNVERIFIED"})
    )
    assert len(read_performance(tmp_path, now=NOW).rows) == 1
    status.write_text(
        json.dumps({"session_date": "2026-11-27", "status": "INVALID", "reason": "secret-account"})
    )
    result = read_performance(tmp_path, now=NOW)
    assert not result.available and "secret-account" not in str(result)


def test_today_is_exchange_day_not_host_day():
    assert exchange_today(datetime(2026, 10, 1, 1, tzinfo=UTC)) == date(2026, 9, 30)


def test_default_directory_and_current_missing_status(tmp_path):
    performance = tmp_path / "performance"
    bundle(performance)
    result = read_configured_performance({"QUANTBOT_LEARNER_DIRECTORY": str(tmp_path)}, now=NOW)
    assert result.available and len(result.rows) == 1
    CloseLedger(performance / "closes.db")
    with sqlite3.connect(performance / "closes.db") as connection:
        connection.execute("DELETE FROM paper_daily_closes_v1")
    status = performance / "capture-status.json"
    status.write_text(
        json.dumps(
            {"session_date": "2026-11-27", "status": "MISSING", "reason": "BASELINE_MISSING"}
        )
    )
    result = read_performance(performance, now=NOW)
    assert result.available and result.rows[0].status == "MISSING"
    assert (
        result.rows[0].net_pnl is None
        and result.rows[0].reason == "Previous close baseline unavailable."
    )


@pytest.mark.parametrize("change", ["wrong_previous", "string_bool", "extra_field", "date_key"])
def test_invalid_receipt_rejected_without_arbitrary_text(tmp_path, change):
    bundle(tmp_path)
    with sqlite3.connect(tmp_path / "closes.db") as connection:
        document = json.loads(
            connection.execute("SELECT evidence_json FROM paper_daily_closes_v1").fetchone()[0]
        )
        if change == "wrong_previous":
            document["expected_previous_session_date"] = document["previous_session_date"] = (
                "2026-11-24"
            )
        if change == "string_bool":
            document["flows_verified"] = "true"
        if change == "extra_field":
            document["unsafe"] = "secret-account"
        connection.execute(
            "UPDATE paper_daily_closes_v1 SET evidence_json=?", (json.dumps(document),)
        )
        if change == "date_key":
            connection.execute("UPDATE paper_daily_closes_v1 SET session_date='2026-11-26'")
    result = read_performance(tmp_path, now=NOW)
    assert not result.available and not result.rows
    assert "secret-account" not in str(result)


def test_default_month_uses_exchange_clock(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from quantbot.console.app import create_app
    from quantbot.storage import Database

    monkeypatch.setattr("quantbot.console.app.exchange_today", lambda: date(2026, 9, 30))
    database = Database.create(tmp_path / "operations.db")
    try:
        assert TestClient(create_app(database)).get("/api/calendar").json()["month"] == "2026-09"
    finally:
        database.close()


def test_cli_connects_read_only_reader(tmp_path, monkeypatch):
    import argparse
    import io

    from fastapi.testclient import TestClient

    from quantbot import cli
    from quantbot.storage import Database

    directory = tmp_path / "performance"
    bundle(directory)
    database = Database.create(tmp_path / "operations.db")
    database.close()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "operations.db"))
    monkeypatch.setenv("QUANTBOT_DAILY_PERFORMANCE_DIRECTORY", str(directory))
    for name in (
        "_console_readiness_probe",
        "_console_runtime_panel",
        "_console_reconcile",
        "_console_sync_data",
    ):
        monkeypatch.setattr(cli, name, lambda *args: None)
    monkeypatch.setattr("quantbot.console.market_status.paper_market_probe", lambda: None)
    monkeypatch.setattr(
        "quantbot.console.performance_reader.read_configured_performance",
        lambda: read_configured_performance(now=NOW),
    )
    rendered = []

    def run(app, **kwargs):
        rendered.append(TestClient(app).get("/calendar?month=2026-11").text)

    monkeypatch.setattr("uvicorn.run", run)
    assert cli._console(argparse.Namespace(host=None, port=None), io.StringIO()) == 0
    assert "+$10.00" in rendered[0]


def test_duplicate_json_fields_fail_closed(tmp_path):
    bundle(tmp_path)
    path = tmp_path / "calendar.json"
    document = path.read_text()
    path.write_text(
        document.replace('"calendar": "XNYS"', '"calendar": "UNTRUSTED", "calendar": "XNYS"')
    )
    assert not read_performance(tmp_path, now=NOW).available
