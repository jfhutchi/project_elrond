from datetime import UTC, datetime, timedelta

import pytest

from quantbot.operations.alerts import AlertPolicy, AlertStore, Condition

NOW = datetime(2026, 10, 1, 14, 0, tzinfo=UTC)
GATE = Condition(
    "PAPER_PROMOTION_REQUIRED",
    "paper_entry",
    "CRITICAL",
    "OPERATOR_ACTION_REQUIRED",
    "Operator",
    "Review model evidence; explicitly approve "
    "the exact PAPER model deployment. Local forecasts may continue.",
)


def test_critical_lifecycle_survives_restart_ack_is_not_resolution(tmp_path):
    path = tmp_path / "alerts.db"
    with AlertStore(path) as store:
        events = store.update([GATE], now=NOW)
        assert [e["event"] for e in events] == ["OPENED"]
        assert events[0]["severity"] == "CRITICAL"
        assert store.update([GATE], now=NOW + timedelta(minutes=1)) == []
        store.acknowledge("PAPER_PROMOTION_REQUIRED", now=NOW + timedelta(minutes=2))
    with AlertStore(path) as store:
        alert = store.active()[0]
        assert alert["first_seen"] == NOW.isoformat()
        assert alert["acknowledged_at"] is not None
        assert alert["resolved_at"] is None
        events = store.update([GATE], now=NOW + timedelta(hours=1))
        assert events[0]["event"] == "REMINDER"
        assert events[0]["escalation"] == 1
        recovered = store.update([], now=NOW + timedelta(hours=2))
        assert recovered[0]["event"] == "RECOVERED"
        assert recovered[0]["duration_seconds"] == 7200
        assert store.active() == []


def test_reopen_resets_acknowledgement_and_policy_routes_severity(tmp_path):
    with AlertStore(
        tmp_path / "alerts.db", policy=AlertPolicy(notify_severities=("CRITICAL",))
    ) as s:
        warning = Condition(
            "DATA_WAIT",
            "forecast",
            "WARNING",
            "MISSING_COMPONENT",
            "Engineering",
            "Inspect data capture.",
        )
        assert s.update([warning], now=NOW) == []
        assert s.active()[0]["severity"] == "WARNING"
        assert s.update([], now=NOW + timedelta(minutes=1)) == []
        s.update([GATE], now=NOW + timedelta(minutes=2))
        s.acknowledge(GATE.code, now=NOW + timedelta(minutes=3))
        s.update([], now=NOW + timedelta(minutes=4))
        s.update([GATE], now=NOW + timedelta(minutes=5))
        assert s.active()[0]["acknowledged_at"] is None


def test_invalid_clock_or_acknowledgement_cannot_clear_alert(tmp_path):
    with AlertStore(tmp_path / "alerts.db") as s:
        s.update([GATE], now=NOW)
        with pytest.raises(ValueError):
            s.update([], now=NOW - timedelta(seconds=1))
        with pytest.raises(ValueError):
            s.acknowledge("UNKNOWN", now=NOW)
        assert len(s.active()) == 1


def test_cli_acknowledges_existing_store_without_creating_missing_one(tmp_path, capsys):
    from quantbot.operations.alerts import main

    path = tmp_path / "alerts.db"
    with AlertStore(path) as store:
        store.update([GATE], now=datetime.now(UTC))
    assert main(["--database", str(path), "--acknowledge", GATE.code]) == 0
    assert "ACKNOWLEDGED" in capsys.readouterr().out
    with AlertStore(path) as store:
        assert store.active()[0]["resolved_at"] is None
    absent = tmp_path / "missing.db"
    assert main(["--database", str(absent), "--acknowledge", GATE.code]) == 1
    assert not absent.exists()
