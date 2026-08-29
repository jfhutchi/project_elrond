"""That the suite cannot reach the operator's deployment.

The incident this pins: `QUANTBOT_DB_PATH` was exported in a developer shell, `quantbot.cli.main`
builds a `Database` from it for every command, and `Database.__init__` upgrades whatever it opens
to the current checkout's head revision. A unit test invoking the CLI therefore migrated a live
trading store to a schema version that existed only on an unmerged branch.

The failure mode this file guards is specifically that **nothing else would fail**. If the
isolation stopped applying, the suite would go back to reading the operator's environment and
would stay green while doing it -- which is how it went unnoticed the first time. So the property
is asserted directly rather than being relied upon.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.conftest import REDIRECTED


def test_every_write_bearing_path_lands_in_this_test_s_own_directory(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Not merely "set to something" -- set to somewhere pytest owns.

    Asserting only that the variables are populated would pass on the exact configuration that
    caused the incident, because they were populated then too.
    """
    basetemp = tmp_path_factory.getbasetemp().resolve()

    for name in REDIRECTED:
        value = os.environ.get(name)
        assert value is not None, f"{name} is unset, so a default path would be used instead"
        resolved = Path(value).resolve()
        assert basetemp in resolved.parents, (
            f"{name} points outside pytest's temporary directory: {resolved}"
        )


def test_opening_a_database_here_cannot_touch_the_deployment() -> None:
    """The end-to-end version: build a `Database` the way the CLI does and see where it lands.

    `Database.__init__` runs migrations on whatever it opens, so the question that matters is not
    whether the variable looks right but where a real open actually writes.
    """
    from quantbot.storage import Database

    path = Path(os.environ["QUANTBOT_DB_PATH"])
    assert not path.exists(), "each test starts with its own empty store"

    database = Database.create(path)
    try:
        assert path.exists(), "the database was created somewhere other than the path given"
    finally:
        database.close()


def test_a_test_that_wants_its_own_path_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fixture is a floor, not a ceiling.

    Several suites deliberately set these variables; if the autouse fixture overrode them the
    isolation would have been bought by breaking the tests that configure themselves.
    """
    chosen = tmp_path / "chosen.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(chosen))

    assert os.environ["QUANTBOT_DB_PATH"] == str(chosen)
