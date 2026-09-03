"""What is actually running, and the four ways that answer goes wrong (#190).

The inventory that produced this module established which commit was checked out, whether
anything was running and which database was authoritative by reading the filesystem, the process
table, the registry, the ARP cache and the git reflog. Those answers were unambiguous only
because nothing was running. Each test here is one question that archaeology would have left
open with a live process on an old checkout.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from quantbot.domain import StrategyIdentity
from quantbot.operations.deployment import (
    UNKNOWN_COMMIT,
    DeploymentRegistry,
    DeploymentWarning,
    RuntimeContext,
    RuntimeRole,
    database_fingerprint,
    describe_runtime,
    instance_id,
    summarize,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import SCHEMA_VERSION

NOW = datetime(2026, 9, 2, 21, 0, tzinfo=UTC)
STARTED = NOW - timedelta(minutes=5)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.create(tmp_path / "quantbot.db")
    try:
        yield db
    finally:
        db.close()


def _identity(*, commit: str = "abc1234") -> StrategyIdentity:
    return StrategyIdentity(
        strategy_id="adaptive-momentum-v1",
        version="1.2.0",
        git_commit=commit,
        configuration_hash="c" * 64,
        deployment_timestamp=STARTED,
    )


def _context(
    tmp_path: Path,
    *,
    role: RuntimeRole = RuntimeRole.COORDINATOR,
    process: str = "pid-1",
    commit: str = "aaaa111",
    mode: str = "PAPER",
    host: str = "omen",
) -> RuntimeContext:
    return RuntimeContext(
        role=role,
        database_path=tmp_path / "quantbot.db",
        trading_mode=mode,
        process_started_at=STARTED,
        host_id=host,
        process_id=process,
        git_commit=commit,
    )


def test_an_unrecorded_runtime_says_so_rather_than_looking_healthy(
    database: Database,
) -> None:
    """The state the coordinator was actually found in. Silence is not health."""
    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW)

    assert status.ok is False
    assert status.warnings == (DeploymentWarning.NO_RUNTIME_RECORDED,)
    assert status.latest_coordinator is None
    assert status.active == ()
    assert "no process has ever recorded what it is running" in status.reasons[0]

    report = summarize(status)
    assert report["ok"] is False
    assert report["coordinator"] is None
    assert report["active_workers"] == []


def test_a_recorded_coordinator_answers_the_question_archaeology_had_to(
    database: Database, tmp_path: Path
) -> None:
    """Commit, host, process, database identity and schema, from the runtime itself."""
    context = _context(tmp_path)
    with database.transaction() as session:
        DeploymentRegistry(session).record(context.observe(now=NOW, identity=_identity()))

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW)

    assert status.ok is True
    assert status.warnings == ()
    coordinator = status.latest_coordinator
    assert coordinator is not None
    assert coordinator.host_id == "omen"
    assert coordinator.instance_id == "pid-1"
    assert coordinator.git_commit == "aaaa111"
    assert coordinator.trading_mode == "PAPER"
    assert coordinator.schema_version == SCHEMA_VERSION
    assert coordinator.strategy_id == "adaptive-momentum-v1"
    assert coordinator.configuration_hash == "c" * 64
    assert coordinator.database_fingerprint == database_fingerprint(tmp_path / "quantbot.db")
    assert status.active_coordinators == (coordinator,)

    # The fingerprint identifies the store without publishing where it lives.
    assert str(tmp_path) not in coordinator.database_fingerprint
    assert len(coordinator.database_fingerprint) == 64


def test_a_coordinator_that_stopped_is_not_reported_as_running(
    database: Database, tmp_path: Path
) -> None:
    """A dead coordinator cannot update its own row to say it died.

    Which is why the record is append-only observations rather than mutable state: absence of a
    recent observation is the signal, and a process that crashed cannot suppress it.
    """
    with database.transaction() as session:
        DeploymentRegistry(session).record(_context(tmp_path).observe(now=NOW))

    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        still_running = registry.status(now=NOW + timedelta(hours=99))
        assert still_running.ok is True

        stopped = registry.status(now=NOW + timedelta(hours=101))
        assert DeploymentWarning.NO_ACTIVE_COORDINATOR in stopped.warnings
        assert stopped.latest_coordinator is not None, "and the last one seen is still readable"
        assert stopped.active_coordinators == ()
        assert NOW.isoformat() in " ".join(stopped.reasons)


def test_two_active_coordinators_are_the_second_writer_condition_made_visible(
    database: Database, tmp_path: Path
) -> None:
    """#190 Phase 4's hazard, reported rather than inferred from a stray process listing.

    The writer lock stops two coordinators against one database. It says nothing about two
    hosts against one paper account with two databases, which is exactly what the stale
    `E:\\...` watchdog would have produced.
    """
    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        registry.record(_context(tmp_path, host="omen", process="pid-1").observe(now=NOW))
        registry.record(
            _context(tmp_path, host="old-desktop", process="pid-9").observe(
                now=NOW + timedelta(minutes=1)
            )
        )

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW + timedelta(minutes=2))

    assert DeploymentWarning.MULTIPLE_ACTIVE_COORDINATORS in status.warnings
    assert len(status.active_coordinators) == 2
    detail = " ".join(status.reasons)
    assert "old-desktop/pid-9" in detail and "omen/pid-1" in detail


@pytest.mark.parametrize(
    ("commits", "stale", "unidentified"),
    [
        (("aaaa111", "bbbb222"), False, False),
        (("aaaa111", "bbbb222", "aaaa111"), True, False),
        (("aaaa111", "bbbb222", "cccc333"), False, False),
        (("aaaa111", ""), False, True),
    ],
    ids=["upgrade", "reversion", "multi-step-upgrade", "unknown"],
)
def test_stale_checkout_requires_a_reappearing_commit_in_history(
    database: Database, tmp_path: Path, commits: tuple[str, ...], stale: bool, unidentified: bool
) -> None:
    """Only A -> B -> A proves a checkout reversion from append-only history."""
    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        for index, commit in enumerate(commits):
            registry.record(
                _context(tmp_path, commit=commit, process="pid-1").observe(
                    now=NOW + timedelta(minutes=index)
                )
            )

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW + timedelta(minutes=len(commits)))

    assert (DeploymentWarning.STALE_CHECKOUT in status.warnings) is stale
    assert (DeploymentWarning.UNIDENTIFIED_CHECKOUT in status.warnings) is unidentified
    if unidentified:
        assert status.latest_coordinator is not None
        assert status.latest_coordinator.git_commit == UNKNOWN_COMMIT


def test_worker_availability_is_reported_separately_from_coordinator_authority(
    database: Database, tmp_path: Path
) -> None:
    """A worker being up says nothing about whether anything is trading (#190)."""
    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        registry.record(
            _context(tmp_path, role=RuntimeRole.WORKER, process="kronos-1").observe(now=NOW)
        )

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW)

    assert len(status.active_workers) == 1
    assert status.active_coordinators == ()
    assert DeploymentWarning.NO_ACTIVE_COORDINATOR in status.warnings, (
        "an available worker must not be readable as a running coordinator"
    )
    report = summarize(status)
    assert report["coordinator"] is None
    active_workers = cast(list[dict[str, object]], report["active_workers"])
    assert [item["instance_id"] for item in active_workers] == ["kronos-1"]


def test_a_live_trading_runtime_is_reported_and_never_acted_on(
    database: Database, tmp_path: Path
) -> None:
    """Reported, because an operator must see it. Not enforced, deliberately.

    A deployed-state check with authority to halt would be a new way for the system to stop
    itself over a bookkeeping disagreement, and `LIVE_TRADING` already has its own gates that
    no agent may pass.
    """
    with database.transaction() as session:
        DeploymentRegistry(session).record(_context(tmp_path, mode="live").observe(now=NOW))

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW)

    assert DeploymentWarning.LIVE_TRADING_RECORDED in status.warnings
    assert status.latest_coordinator is not None
    assert status.latest_coordinator.trading_mode == "LIVE", "normalised, not silently accepted"
    assert "never acted on" in " ".join(status.reasons)


def test_the_last_successful_cycle_is_read_from_the_run_ledger_not_stored_twice(
    database: Database, tmp_path: Path
) -> None:
    """A second copy of a timestamp is a second thing that can be wrong."""
    identity = _identity()
    finished = NOW + timedelta(minutes=3)
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.create_run("run-1", identity, started_at=NOW)
        repository.finish_run("run-1", status="SUCCEEDED", finished_at=finished)
        repository.create_run("run-2", identity, started_at=finished)
        repository.finish_run("run-2", status="HALTED", finished_at=finished + timedelta(minutes=1))
        DeploymentRegistry(session).record(_context(tmp_path).observe(now=NOW, identity=identity))

    with database.transaction() as session:
        status = DeploymentRegistry(session).status(now=NOW + timedelta(minutes=5))

    assert status.last_successful_cycle_at == finished, (
        "a halted cycle after a successful one does not become the last success"
    )


def test_recording_the_same_observation_twice_does_not_invent_a_second_runtime(
    database: Database, tmp_path: Path
) -> None:
    """Re-running is a re-run. A duplicate insert would fabricate a coordinator to find."""
    observation = _context(tmp_path).observe(now=NOW)
    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        registry.record(observation)
        registry.record(observation)

    with database.transaction() as session:
        registry = DeploymentRegistry(session)
        assert len(registry.observations()) == 1
        status = registry.status(now=NOW)
    assert DeploymentWarning.MULTIPLE_ACTIVE_COORDINATORS not in status.warnings


def test_a_backwards_clock_records_the_runtime_instead_of_aborting_the_cycle(
    tmp_path: Path,
) -> None:
    """The record is built while the coordinator holds the writer lock at the top of a cycle.

    `PROCESS_STARTED_AT` is real wall clock and the observation time is the caller's, so an NTP
    correction between the two can invert them. Raising there would abort a trading cycle over a
    bookkeeping timestamp, which is a worse outcome than recording "up since no later than now".
    The clamp is visible rather than silent: the two timestamps come out exactly equal, which
    they otherwise never would.
    """
    observation = describe_runtime(
        role=RuntimeRole.COORDINATOR,
        database_path=tmp_path / "quantbot.db",
        trading_mode="PAPER",
        now=NOW,
        process_started_at=NOW + timedelta(seconds=1),
    )

    assert observation.process_started_at == observation.observed_at == NOW

    ordinary = describe_runtime(
        role=RuntimeRole.COORDINATOR,
        database_path=tmp_path / "quantbot.db",
        trading_mode="PAPER",
        now=NOW,
        process_started_at=STARTED,
    )
    assert ordinary.process_started_at == STARTED < ordinary.observed_at


def test_two_processes_on_one_host_get_two_instance_ids() -> None:
    """Otherwise the second-writer check has nothing to distinguish."""
    assert instance_id(pid=10) != instance_id(pid=11)
    assert instance_id(pid=10, started_at=NOW) != instance_id(
        pid=10, started_at=NOW + timedelta(seconds=1)
    )


def test_the_commit_comes_from_the_environment_when_the_caller_does_not_supply_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "  env0c0mmit  ")
    observation = describe_runtime(
        role=RuntimeRole.COORDINATOR,
        database_path=tmp_path / "quantbot.db",
        trading_mode="PAPER",
        now=NOW,
        process_started_at=STARTED,
    )
    assert observation.git_commit == "env0c0mmit"

    monkeypatch.setenv("QUANTBOT_GIT_COMMIT", "   ")
    blank = describe_runtime(
        role=RuntimeRole.COORDINATOR,
        database_path=tmp_path / "quantbot.db",
        trading_mode="PAPER",
        now=NOW,
        process_started_at=STARTED,
    )
    assert blank.git_commit == UNKNOWN_COMMIT
    assert blank.identifies_its_checkout is False
