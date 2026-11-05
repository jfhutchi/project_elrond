"""Normal Console reads expose fresh cost accounting without altering final close history."""

import json
import sqlite3
from copy import deepcopy
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from quantbot.console import performance_reader
from quantbot.console.app import create_app
from quantbot.console.calendar import PerformanceData
from quantbot.operations.daily_performance import DailyPerformance
from quantbot.reporting.paper_costs import read_report
from quantbot.storage import Database
from tests.unit.reporting.test_paper_costs import database_fixture, digest, fixture


def test_normal_api_dashboard_and_reports_refresh_after_late_fee(tmp_path, monkeypatch):
    data = fixture()
    cost_path = tmp_path / "costs.db"
    database_fixture(cost_path, data)
    calls = []
    with Database.create(tmp_path / "console.db") as database:

        def read(path, *, as_of):
            assert path == database.path
            calls.append(path)
            return read_report(cost_path, as_of=data["as_of"])

        monkeypatch.setattr(performance_reader, "read_paper_cost_report", read, raising=False)
        final = PerformanceData(rows=(DailyPerformance(date(2026, 10, 27), "FINAL", Decimal("7.00")),))
        client = TestClient(create_app(database, daily_performance=lambda: final))
        first = client.get("/api/state").json()["paper_costs"]
        assert first["available"] is True
        assert "account_hash" not in first
        today = first["days"][-1]
        assert today["gross_realized"] == "0.01"
        assert today["net_after_observed_fees"] is None
        assert today["all_in_verified_net"] is None
        for route in ("/", "/reports", "/calendar?month=2026-10"):
            page = client.get(route)
            assert page.status_code == 200
            assert "Executed PAPER daily costs" in page.text
            assert "Net after observed fees" in page.text
            assert "Modeled fee scenario" in page.text
            assert "not verified" in page.text
        calendar = client.get("/api/calendar?month=2026-10").json()
        cell = next(c for w in calendar["weeks"] for c in w if c["session_date"] == "2026-10-28")
        assert cell["paper_costs"]["gross_realized"] == "0.01"
        assert cell["amount"] is None
        assert calendar["summary"]["sample_size"] == 1
        assert "7.00" in calendar["summary"]["total"]
        prior = next(c for w in calendar["weeks"] for c in w if c["session_date"] == "2026-10-27")
        assert prior["status"] == "FINAL" and prior["amount"] == "+$7.00"
        assert prior["paper_costs"]["net_after_observed_fees"] == "-0.01"
        later = deepcopy(data["documents"][0]["document"])
        later["rows"].append(
            dict(
                id="late-fee",
                activity_type="FEE",
                date="2026-10-28",
                created_at="2026-10-28T21:00:00Z",
                net_amount="-0.02",
                currency="USD",
                status="executed",
            )
        )
        with sqlite3.connect(cost_path) as c:
            c.execute(
                "INSERT INTO core_provider_acquisitions VALUES (?,?)",
                (digest(later), json.dumps(later)),
            )
            c.execute("UPDATE core_provider_progress SET document_id=?", (digest(later),))
        updated = client.get("/api/state").json()["paper_costs"]["days"][-1]
        assert updated["net_after_observed_fees"] == "-0.01"
        assert updated["fee_scenario"] is None
        assert updated["all_in_verified_net"] is None
        for route in ("/", "/reports", "/calendar?month=2026-10"):
            response = client.get(route)
            assert response.status_code == 200
            day_row = response.text.split("2026-10-28</td>", 1)[1].split("</tr>", 1)[0]
            assert "$-0.02" in day_row
            assert "$-0.01" in day_row
            assert "Not observed" not in day_row
        calendar = client.get("/api/calendar?month=2026-10").json()
        cell = next(c for w in calendar["weeks"] for c in w if c["session_date"] == "2026-10-28")
        assert cell["paper_costs"]["net_after_observed_fees"] == "-0.01"
        assert cell["amount"] is None
        assert calendar["summary"]["sample_size"] == 1
        assert "7.00" in calendar["summary"]["total"]
        prior = next(c for w in calendar["weeks"] for c in w if c["session_date"] == "2026-10-27")
        assert prior["status"] == "FINAL" and prior["amount"] == "+$7.00"
        assert prior["paper_costs"]["net_after_observed_fees"] == "-0.01"
        assert len(calls) >= 5


def test_cost_reader_failure_is_explicit_and_never_overwrites_final_calendar(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("SECRET-account-password-path")

    monkeypatch.setattr(performance_reader, "read_paper_cost_report", broken, raising=False)
    final = PerformanceData(rows=(DailyPerformance(date(2026, 10, 27), "FINAL", Decimal("7.00")),))
    with Database.create(tmp_path / "console.db") as database:
        client = TestClient(create_app(database, daily_performance=lambda: final))
        panel = client.get("/api/state").json()["paper_costs"]
        assert panel["available"] is False and panel["days"] == []
        assert "SECRET" not in json.dumps(panel)
        for route in ("/", "/reports", "/calendar?month=2026-10"):
            response = client.get(route)
            assert response.status_code == 200
            assert "Cost accounting unavailable" in response.text
            assert "SECRET" not in response.text
        calendar = client.get("/api/calendar?month=2026-10").json()
        assert calendar["summary"]["sample_size"] == 1
        assert "7.00" in calendar["summary"]["total"]


def test_unconfigured_noncore_database_does_not_invent_zero_or_gross_net(tmp_path):
    with Database.create(tmp_path / "console.db") as database:
        client = TestClient(create_app(database))
        panel = client.get("/api/state").json()["paper_costs"]
        assert panel["available"] is False
        assert panel["days"] == []
        assert client.get("/").status_code == 200
