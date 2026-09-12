"""Where the generated status file lands, which is a safety property and not a tidiness one.

`operations/service.py` refuses every component when the deployed checkout is not pristine
(`CHECKOUT_NOT_CLEAN`). `write_project_status` used to write a *tracked* file into that checkout,
so a successful cycle made the tree dirty and no service could start again. The deadlock is
self-sustaining -- nothing cleans the tree -- and on the Omen it kept `Elrond Research` and
`Elrond Ingestion` refused from 2026-09-12T15:15Z onward, which is why no research cycle had ever
run against the deployed store.

These tests assert the named property: the file is written to the configured external reports
directory, and nothing appears in the working directory. The second is the one that goes red if
the fix is reverted, because the old code resolved a bare relative path against the cwd.

Every test moves the working directory into `tmp_path` first. That is not decoration: while this
fix was being written, a mutation check ran the old code with pytest's cwd at the repository root
and wrote `PROJECT_STATUS.md` straight into the checkout -- reproducing the exact defect, in the
tree of the branch that fixes it. A test for this bug must not be able to cause it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from quantbot.config import Settings
from quantbot.runtime import write_project_status
from quantbot.storage import Database


def _runtime(database: Database, reports: Path) -> SimpleNamespace:
    """The four attributes `write_project_status` actually reaches for.

    A stub rather than a wired `Runtime`: building a real one needs a broker transport and a
    market-data client, none of which this function touches, and a test that needed them could
    only fail for reasons unrelated to what it is checking.
    """
    return SimpleNamespace(
        settings=Settings(),
        database=database,
        identity=SimpleNamespace(strategy_id="full-v1:test"),
        paths=SimpleNamespace(reports=reports),
    )


def test_the_status_file_is_written_to_the_configured_reports_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    database = Database.create(tmp_path / "quantbot.db")
    reports = tmp_path / "state" / "reports"

    written = write_project_status(
        _runtime(database, reports), next_priority="Accumulate paper observations"
    )

    assert written == reports / "PROJECT_STATUS.md"
    assert written.is_file()
    assert "QuantBot Project Status" in written.read_text(encoding="utf-8")
    database.close()


def test_nothing_is_written_into_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression itself, run from a directory standing in for the deployed checkout.

    If `write_project_status` goes back to a bare relative path, a `PROJECT_STATUS.md` appears in
    the checkout and this fails -- which is precisely the state that refuses every service.
    """
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    reports = tmp_path / "external-state" / "reports"
    database = Database.create(checkout / "quantbot.db")
    monkeypatch.chdir(checkout)

    write_project_status(_runtime(database, reports), next_priority="Resolve doctor reasons")

    assert not (checkout / "PROJECT_STATUS.md").exists(), (
        "the status writer dirtied the checkout, which is what refuses every service on start"
    )
    assert (reports / "PROJECT_STATUS.md").is_file()
    database.close()


def test_the_reports_directory_is_created_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A first run on a fresh host must not fail because nothing made the directory yet."""
    monkeypatch.chdir(tmp_path)
    database = Database.create(tmp_path / "quantbot.db")
    reports = tmp_path / "never" / "existed" / "reports"
    assert not reports.exists()

    written = write_project_status(_runtime(database, reports), next_priority="First run")

    assert written.is_file()
    database.close()
