from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.calendar import PerformanceData, build_calendar
from quantbot.storage import Database


@dataclass(frozen=True)
class Row:
    session_date: date
    status: str
    net_pnl: Decimal | None
    reason: str | None = None


def test_month_grid_and_navigation_cross_year_boundary():
    panel = build_calendar(2026, 1, PerformanceData())
    assert panel.previous_month == "2025-12"
    assert panel.next_month == "2026-02"
    assert [cell.day for cell in panel.weeks[0]] == [None, None, None, 1, 2, 3, 4]
    february = build_calendar(2024, 2, PerformanceData())
    assert any(cell.day == 29 for week in february.weeks for cell in week)


def test_summary_includes_flat_denominator_and_excludes_bad_or_duplicate_rows():
    rows = (
        Row(date(2025, 12, 31), "FINAL", Decimal("5")),
        Row(date(2026, 1, 2), "FINAL", Decimal("-2")),
        Row(date(2026, 1, 5), "FINAL", Decimal("0")),
        Row(date(2026, 1, 6), "PENDING", Decimal("100")),
        Row(date(2026, 1, 7), "FINAL", Decimal("NaN")),
        Row(date(2026, 1, 8), "FINAL", Decimal("10")),
        Row(date(2026, 1, 8), "FINAL", Decimal("10")),
    )
    panel = build_calendar(2026, 1, PerformanceData(rows=rows))
    assert panel.summary.total == "+$3.00"
    assert panel.summary.profitable == 1
    assert panel.summary.losing == 1
    assert panel.summary.flat == 1
    assert panel.summary.sample_size == 3
    assert panel.summary.profitable_percent == "33.3%"
    assert panel.summary.period == "2025-12-31 to 2026-01-05"
    cells = {cell.day: cell for week in panel.weeks for cell in week if cell.day}
    assert cells[2].amount == "-$2.00"
    assert cells[5].amount == "$0.00"
    assert cells[6].status == "PENDING"
    assert cells[7].status == cells[8].status == "INVALID"
    assert cells[3].status == "MISSING"  # unknown calendars never guess weekends


def test_calendar_coverage_distinguishes_non_sessions_only_inside_coverage():
    data = PerformanceData(
        session_dates=frozenset({date(2026, 1, 2)}),
        calendar_start=date(2026, 1, 1),
        calendar_end=date(2026, 1, 4),
    )
    cells = {
        cell.day: cell for week in build_calendar(2026, 1, data).weeks for cell in week if cell.day
    }
    assert cells[1].status == "HOLIDAY"
    assert cells[3].status == "WEEKEND"
    assert cells[2].status == cells[5].status == "MISSING"


def test_read_only_routes_render_signed_results_and_unknown_default(tmp_path):
    database = Database.create(tmp_path / "calendar.db")
    try:
        client = TestClient(create_app(database))
        response = client.get("/calendar?month=2026-01")
        assert response.status_code == 200
        assert "Performance unavailable" in response.text
        assert "No valid finalized days" in response.text
        assert client.get("/calendar?month=bad").status_code == 422
        data = PerformanceData(
            rows=(
                Row(date(2026, 1, 2), "FINAL", Decimal("7")),
                Row(date(2026, 1, 5), "FINAL", Decimal("-3")),
            )
        )
        client = TestClient(create_app(database, daily_performance=lambda: data))
        html = client.get("/calendar?month=2026-01").text
        assert "+$7.00" in html and "-$3.00" in html
        assert "50.0%" in html and "1 profitable / 2 valid finalized days" in html
        assert "2025-12" in html and "2026-02" in html
        assert "+$4.00" in client.get("/").text
        api = client.get("/api/calendar?month=2026-01").json()
        assert api["summary"]["sample_size"] == 2
        assert api["summary"]["total"] == "+$4.00"
    finally:
        database.close()


def test_navigation_at_date_limits_remains_valid():
    first = build_calendar(1, 1, PerformanceData())
    assert first.previous_month is None
    assert first.next_month == "0001-02"
    last = build_calendar(9999, 12, PerformanceData())
    assert last.next_month is None
    assert last.previous_month == "9999-11"


def test_calendar_renders_distinct_financial_and_unknown_states(tmp_path):
    from html.parser import HTMLParser

    class Cells(HTMLParser):
        def __init__(self):
            super().__init__()
            self.current = None
            self.cells = {}

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "td" and "data-date" in attributes:
                self.current = attributes["data-date"]
                self.cells[self.current] = {"status": attributes.get("data-status"), "text": ""}

        def handle_endtag(self, tag):
            if tag == "td":
                self.current = None

        def handle_data(self, data):
            if self.current:
                self.cells[self.current]["text"] += data

    database = Database.create(tmp_path / "states.db")
    try:
        data = PerformanceData(
            rows=(
                Row(date(2026, 1, 2), "FINAL", Decimal("7")),
                Row(date(2026, 1, 5), "FINAL", Decimal("-3")),
                Row(date(2026, 1, 6), "FINAL", Decimal("0")),
                Row(date(2026, 1, 7), "PENDING", None),
            )
        )
        html = (
            TestClient(create_app(database, daily_performance=lambda: data))
            .get("/calendar?month=2026-01")
            .text
        )
        cells = Cells()
        cells.feed(html)
        assert cells.cells["2026-01-02"]["status"] == "FINAL"
        assert "+$7.00" in cells.cells["2026-01-02"]["text"]
        assert "-$3.00" in cells.cells["2026-01-05"]["text"]
        assert "Flat" in cells.cells["2026-01-06"]["text"]
        assert "$0.00" in cells.cells["2026-01-06"]["text"]
        assert "Pending" in cells.cells["2026-01-07"]["text"]
        assert "$" not in cells.cells["2026-01-07"]["text"]
        assert cells.cells["2026-01-08"]["status"] == "MISSING"
        assert "$" not in cells.cells["2026-01-08"]["text"]
        assert "2 valid finalized days" not in html  # flat is a third valid day
        assert "1 profitable / 3 valid finalized days" in html
    finally:
        database.close()
