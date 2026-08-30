"""What has been written, and whether anything still is (#82).

A listing rather than a viewer. Rendering the markdown would need a renderer this project has no
other use for, for a file that opens perfectly well on its own -- and the question the panel
answers is not what last week's report said but whether reports are still being produced, which
is the question that went unanswered while the system sat stopped for ten days.

Three states that a naive implementation would collapse into one empty list: the directory is
missing, the directory is empty, and the console was started without one. Only the first is a
configuration fault.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.state import build_reports_panel, read_console_state
from quantbot.storage import Database

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def write_report(directory: Path, name: str, *, days_old: int = 0, body: str = "# week\n") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(body, encoding="utf-8")
    stamp = (NOW - timedelta(days=days_old)).timestamp()
    os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "reports.db")
    try:
        yield db
    finally:
        db.close()


def client(database: Database, panel=None) -> TestClient:
    return TestClient(create_app(database, reports=panel))


# --- three states a single empty list would hide ------------------------------------------------


def test_a_missing_directory_is_not_an_empty_one(tmp_path: Path, database: Database) -> None:
    """"Nobody wrote a report" and "the place reports go does not exist" are different faults.

    Only the second is a configuration error, and collapsing them would leave an operator
    looking for a missing report that was never going to be found where they were looking.
    """
    panel = build_reports_panel(tmp_path / "absent", now=NOW)

    assert panel.missing is True
    assert panel.reports == ()
    assert panel.days_since_latest is None

    body = client(database, panel).get("/reports").text
    assert "does not exist" in body
    assert "configuration fault" in body


def test_an_empty_directory_says_nobody_has_written_one(
    tmp_path: Path, database: Database
) -> None:
    """A fact about what has run, not a fault in the configuration."""
    (tmp_path / "reports").mkdir()
    panel = build_reports_panel(tmp_path / "reports", now=NOW)

    assert panel.missing is False
    assert panel.reports == ()

    body = client(database, panel).get("/reports").text
    assert "exists and is empty" in body


def test_no_directory_at_all_is_a_statement_about_the_console(database: Database) -> None:
    """Started without one. That describes how the console was launched, not what exists."""
    body = client(database, None).get("/reports").text

    assert "started without a reports directory" in body


# --- the listing ----------------------------------------------------------------------------------


def test_reports_are_listed_newest_first(tmp_path: Path) -> None:
    """An operator scanning for "is this current" reads the top of the list."""
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md", days_old=20)
    write_report(directory, "week-03.md", days_old=1)
    write_report(directory, "week-02.md", days_old=8)

    panel = build_reports_panel(directory, now=NOW)

    assert [row.name for row in panel.reports] == ["week-03.md", "week-02.md", "week-01.md"]
    assert panel.days_since_latest == 1


def test_only_reports_are_listed(tmp_path: Path) -> None:
    """A scratch file beside the reports is not a report, and counting one would make the
    directory look busier than it is -- which is the direction that flatters the system.
    """
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md")
    (directory / "notes.txt").write_text("scratch", encoding="utf-8")
    (directory / "manifests").mkdir()

    panel = build_reports_panel(directory, now=NOW)

    assert [row.name for row in panel.reports] == ["week-01.md"]


def test_a_stale_listing_shows_its_age(tmp_path: Path) -> None:
    """The signal that matters, and the same one the stopped-cycle notice carries."""
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md", days_old=14)

    panel = build_reports_panel(directory, now=NOW)

    assert panel.days_since_latest == 14


def test_size_is_reported_so_an_empty_file_is_visible(tmp_path: Path) -> None:
    """A report that was created and never written is worse than a missing one: it looks like
    evidence that something ran.
    """
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md", body="")

    panel = build_reports_panel(directory, now=NOW)

    assert panel.reports[0].size_bytes == 0


# --- what reaches the operator --------------------------------------------------------------------


def test_a_missing_directory_reaches_the_attention_list(
    tmp_path: Path, database: Database
) -> None:
    """A configuration fault that silently produces nothing is one nobody finds by looking."""
    panel = build_reports_panel(tmp_path / "absent", now=NOW)

    with database.transaction() as session:
        state = read_console_state(session, now=NOW, reports=panel)

    assert any("reports directory" in item and "does not exist" in item for item in state.attention)


def test_a_healthy_directory_raises_nothing(tmp_path: Path, database: Database) -> None:
    """A notice that fires on a working system is one an operator learns to scroll past."""
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md")
    panel = build_reports_panel(directory, now=NOW)

    with database.transaction() as session:
        state = read_console_state(session, now=NOW, reports=panel)

    assert not [item for item in state.attention if "reports directory" in item]


def test_a_file_that_vanishes_mid_listing_is_skipped_not_zeroed(tmp_path: Path) -> None:
    """Reported as size zero it would read as an empty report somebody wrote.

    Exercised by pointing the panel at a directory whose entry disappears between the glob and
    the stat -- rare, and the kind of race whose wrong answer is indistinguishable from data.
    """
    directory = tmp_path / "reports"
    write_report(directory, "week-01.md")
    ghost = directory / "week-02.md"
    ghost.write_text("x", encoding="utf-8")

    real_stat = Path.stat

    def vanishing(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.name == "week-02.md":
            raise OSError("file vanished")
        return real_stat(self, *args, **kwargs)

    Path.stat = vanishing  # type: ignore[method-assign]
    try:
        panel = build_reports_panel(directory, now=NOW)
    finally:
        Path.stat = real_stat  # type: ignore[method-assign]

    assert [row.name for row in panel.reports] == ["week-01.md"]
