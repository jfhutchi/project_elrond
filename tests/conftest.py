"""Suite-wide isolation from the operator's real deployment.

**This exists because the test suite migrated a live trading database.**

`Database.__init__` calls `command.upgrade(config, "head")`, so merely *opening* a store applies
every migration the current checkout carries. `quantbot.cli.main` builds one for every command
from `QUANTBOT_DB_PATH`, read out of the ambient environment. On a machine where the operator has
exported that variable -- which is the machine this is developed on -- a unit test that invoked
the CLI opened the deployment's database and upgraded it to a schema version that existed only on
an unmerged branch. Nothing was lost, because the migration was additive, but every build at the
older version then refused to open the store at all.

CI never saw any of it. CI's environment is clean, which is exactly why this could not be left to
be noticed: the suite was green while reaching outside the repository and writing to it.

So every test gets its own paths, and a test that wants specific ones still sets them itself --
`monkeypatch.setenv` inside a test runs after this fixture and wins.

Only the variables that name somewhere the process will **write** are redirected. Credentials and
endpoint configuration are deliberately left alone: a test that opts into a live endpoint should
keep being able to find it, and scrubbing those here would silently disable coverage rather than
protect anything.
"""

from __future__ import annotations

from pathlib import Path

import pytest

#: Environment variables naming somewhere the process will write. `QUANTBOT_DB_PATH` is the one
#: that caused the incident; the others are here because the reasoning is identical -- a lock
#: file or a report written into the operator's deployment is still a test reaching outside the
#: repository.
REDIRECTED = ("QUANTBOT_DB_PATH", "QUANTBOT_LOCK_PATH", "QUANTBOT_REPORTS_DIR")


@pytest.fixture(autouse=True)
def isolate_deployment_paths(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point every write-bearing path at this test's own directory.

    Autouse and unconditional. A fixture that only acted when the variable was already set would
    leave the default relative `quantbot.db` being created in whatever directory the suite ran
    from, which is the same class of problem one level quieter.
    """
    sandbox: Path = tmp_path_factory.mktemp("deployment")
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(sandbox / "quantbot.db"))
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(sandbox / "quantbot.lock"))
    monkeypatch.setenv("QUANTBOT_REPORTS_DIR", str(sandbox / "reports"))
