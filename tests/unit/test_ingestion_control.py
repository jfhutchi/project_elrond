"""The ingestion control plane, and the distinctions it exists to keep from collapsing (#198).

Every test here names one specific way collection can lie about itself. The failure mode this
file guards is that **all of them are silent**: a stopped collector, a checkpoint that ran ahead
of its data, a backfill counted as forward evidence, and a paused source all look identical from
the outside, and each is only discovered when a hypothesis needs a window that nobody collected.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.ingestion import (
    CursorAdvance,
    CursorConflict,
    EventKind,
    HeartbeatStatus,
    IngestionControlPlane,
    IngestionError,
    RecordCounts,
    RunMode,
    RunStatus,
    SourceHealth,
)
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 9, 2, 21, 0, tzinfo=UTC)
SOURCE = "alpaca-us-equities-daily"
STREAM = "SPY"
CONFIG_HASH = "a" * 64
PROVIDER = "alpaca:iex:all:1Day"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.create(tmp_path / "quantbot.db")
    try:
        yield db
    finally:
        db.close()


def _register(plane: IngestionControlPlane, *, enabled: bool = True) -> None:
    plane.register_source(
        source_id=SOURCE,
        provider="alpaca",
        source_kind="equity-daily-bars",
        configuration_hash=CONFIG_HASH,
        enabled=enabled,
        now=NOW - timedelta(days=30),
    )


def _bar(day: int, *, close: str = "100") -> Bar:
    return Bar(
        symbol=STREAM,
        timestamp=datetime(2026, 9, day, 20, 0, tzinfo=UTC),
        open="99",
        high="101",
        low="98",
        close=close,
        volume="1000",
        adjustment="1",
    )


def _collect(
    plane: IngestionControlPlane,
    repository: StorageRepository,
    *,
    run_id: str,
    bars: list[Bar],
    now: datetime,
    mode: RunMode = RunMode.LIVE,
    expected_version: int | None = None,
) -> int:
    """One honest collection: open the run, store, then checkpoint past what was stored."""
    plane.begin_run(
        ingestion_run_id=run_id,
        source_id=SOURCE,
        mode=mode,
        now=now,
        requested_start=bars[0].timestamp.isoformat(),
        requested_end=bars[-1].timestamp.isoformat(),
    )
    inserted = repository.save_bars(bars, provider=PROVIDER)
    plane.complete_run(
        run_id,
        counts=RecordCounts(
            seen=len(bars), inserted=inserted, duplicate=len(bars) - inserted
        ),
        now=now,
        cursor=CursorAdvance(
            stream_key=STREAM,
            cursor_value=bars[-1].timestamp.isoformat(),
            last_observation_at=bars[-1].timestamp,
            expected_version=expected_version,
        ),
    )
    return inserted


def test_a_collector_killed_mid_batch_resumes_without_loss_or_duplication(
    database: Database,
) -> None:
    """The checkpoint and the rows it points past are one transaction, or neither.

    A cursor that survives a crash its observations did not is worse than no cursor: the next
    run starts after data that was never stored, and the gap is permanent and invisible. So the
    kill is simulated the way a kill actually behaves -- the transaction never commits -- and
    both halves are asserted, not just the cursor.
    """
    with database.transaction() as session:
        _register(IngestionControlPlane(session))

    with pytest.raises(RuntimeError, match="process killed mid-batch"):
        with database.transaction() as session:
            plane = IngestionControlPlane(session)
            repository = StorageRepository(session)
            plane.begin_run(
                ingestion_run_id="run-1", source_id=SOURCE, mode=RunMode.LIVE, now=NOW
            )
            repository.save_bars([_bar(1), _bar(2)], provider=PROVIDER)
            raise RuntimeError("process killed mid-batch")

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.cursor(SOURCE, STREAM) is None, "a cursor survived its own observations"
        assert plane.get_run("run-1") is None, "a run record survived a rolled-back batch"
        assert StorageRepository(session).list_bars(symbol=STREAM) == []

    # Restart. Nothing was committed, so the collector correctly starts from the beginning and
    # every bar lands exactly once.
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        inserted = _collect(
            plane,
            StorageRepository(session),
            run_id="run-2",
            bars=[_bar(1), _bar(2)],
            now=NOW,
        )
        assert inserted == 2

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        cursor = plane.cursor(SOURCE, STREAM)
        assert cursor is not None
        assert cursor.cursor_value == _bar(2).timestamp.isoformat()
        assert cursor.version == 1
        assert len(StorageRepository(session).list_bars(symbol=STREAM)) == 2


def test_a_provider_replaying_rows_does_not_duplicate_observations(database: Database) -> None:
    """Re-running the same range is idempotent, and the run history says so."""
    with database.transaction() as session:
        _register(IngestionControlPlane(session))

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert (
            _collect(
                plane, StorageRepository(session), run_id="run-1", bars=[_bar(1), _bar(2)], now=NOW
            )
            == 2
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        # The provider serves the same two bars plus one new one.
        inserted = _collect(
            plane,
            StorageRepository(session),
            run_id="run-2",
            bars=[_bar(1), _bar(2), _bar(3)],
            now=NOW + timedelta(days=1),
            expected_version=1,
        )
        assert inserted == 1

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        stored = StorageRepository(session).list_bars(symbol=STREAM)
        assert len(stored) == 3
        assert len({bar.timestamp for bar in stored}) == 3

        replay = plane.get_run("run-2")
        assert replay is not None
        assert replay.counts.seen == 3
        assert replay.counts.inserted == 1
        assert replay.counts.duplicate == 2, (
            "a replayed row must be counted as a duplicate, not quietly dropped from the record"
        )


def test_a_failed_run_cannot_checkpoint_past_what_it_never_stored(database: Database) -> None:
    """There is no API that advances a cursor on a failure. That is the guarantee.

    A partial batch that could checkpoint would skip whatever it never reached, and the skip is
    unrecoverable once the provider stops serving the window. `fail_run` therefore takes no
    `CursorAdvance` at all -- the property is enforced by the signature rather than by a caller
    remembering not to pass one.
    """
    with database.transaction() as session:
        _register(IngestionControlPlane(session))

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        plane.begin_run(ingestion_run_id="run-1", source_id=SOURCE, mode=RunMode.LIVE, now=NOW)
        StorageRepository(session).save_bars([_bar(1)], provider=PROVIDER)
        plane.fail_run(
            "run-1",
            error_code="PROVIDER_RATE_LIMITED",
            now=NOW,
            counts=RecordCounts(seen=5, inserted=1),
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.cursor(SOURCE, STREAM) is None

        run = plane.get_run("run-1")
        assert run is not None
        assert run.status is RunStatus.FAILED
        assert run.error_code == "PROVIDER_RATE_LIMITED"
        assert run.counts.unaccounted == 4, (
            "four rows were seen and never accounted for; that residue is the interesting number"
        )
        assert [event.event_kind for event in plane.open_incidents(SOURCE)] == [
            EventKind.PROVIDER_FAILURE
        ]

    assert "cursor" not in IngestionControlPlane.fail_run.__annotations__


def test_two_collectors_racing_on_one_stream_are_refused_rather_than_merged(
    database: Database,
) -> None:
    """A lost checkpoint update is a permanently skipped window nobody ever sees."""
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        plane.begin_run(ingestion_run_id="run-2", source_id=SOURCE, mode=RunMode.LIVE, now=NOW)
        with pytest.raises(CursorConflict, match="is at version 1"):
            plane.complete_run(
                "run-2",
                counts=RecordCounts(seen=1, inserted=1),
                now=NOW,
                # Stale: this collector still believes the stream has never been checkpointed.
                cursor=CursorAdvance(stream_key=STREAM, cursor_value="anything"),
            )

    with database.transaction() as session:
        cursor = IngestionControlPlane(session).cursor(SOURCE, STREAM)
        assert cursor is not None
        assert cursor.version == 1
        assert cursor.cursor_value == _bar(1).timestamp.isoformat()


def test_a_backfill_cannot_make_a_stopped_feed_look_current(database: Database) -> None:
    """Historical catch-up is not forward evidence, and cannot be mistaken for it.

    `CursorAdvance` has no `last_success_at` field; the control plane derives it from the run
    mode. So a source whose only successful runs are backfills reports `NEVER_COLLECTED` --
    which is true -- however many rows those backfills stored.
    """
    with database.transaction() as session:
        _register(IngestionControlPlane(session))

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _collect(
            plane,
            StorageRepository(session),
            run_id="backfill-1",
            bars=[_bar(1), _bar(2)],
            now=NOW,
            mode=RunMode.BACKFILL,
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        cursor = plane.cursor(SOURCE, STREAM)
        assert cursor is not None
        assert cursor.last_observation_at == _bar(2).timestamp, (
            "the backfill really did observe those bars"
        )
        assert cursor.last_success_at is None, (
            "but it did not establish that live collection is running"
        )

        status = plane.status(
            SOURCE, stream_key=STREAM, expected_success_after=NOW - timedelta(hours=1)
        )
        assert status.health is SourceHealth.NEVER_COLLECTED
        assert status.collecting is False

    # A live run afterwards does establish it, and the backfill's position is kept.
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _collect(
            plane,
            StorageRepository(session),
            run_id="live-1",
            bars=[_bar(3)],
            now=NOW + timedelta(days=1),
            expected_version=1,
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        status = plane.status(
            SOURCE, stream_key=STREAM, expected_success_after=NOW - timedelta(hours=1)
        )
        assert status.health is SourceHealth.HEALTHY
        assert status.last_success_at == NOW + timedelta(days=1)


def test_a_live_run_that_never_arrives_becomes_stale_and_opens_one_incident(
    database: Database,
) -> None:
    """A collector that is alive but producing nothing must not report healthy.

    And repeated observation must not stack duplicate incidents: an operator scrolling a wall
    of identical alerts learns less than one who sees a single one that has been open for three
    weeks.
    """
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)

    three_weeks = NOW + timedelta(days=21)
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        for offset in range(3):
            status = plane.status(
                SOURCE,
                stream_key=STREAM,
                expected_success_after=three_weeks - timedelta(hours=1),
                record_incident=True,
                now=three_weeks + timedelta(minutes=offset),
            )
            assert status.health is SourceHealth.STALE
            assert status.collecting is False
        assert "predates the expected window" in status.reason

        incidents = plane.open_incidents(SOURCE, event_kind=EventKind.STALE_DETECTED)
        assert len(incidents) == 1, "three observations of one outage are one incident"
        assert incidents[0].occurred_at == three_weeks

    # Recovery is observed, not inferred from the incident going quiet.
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _collect(
            plane,
            StorageRepository(session),
            run_id="run-2",
            bars=[_bar(2)],
            now=three_weeks + timedelta(hours=1),
            expected_version=1,
        )
        status = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=three_weeks - timedelta(hours=1),
            record_incident=True,
            now=three_weeks + timedelta(hours=1),
        )
        assert status.health is SourceHealth.HEALTHY
        assert plane.open_incidents(SOURCE) == []


def test_a_closed_market_is_not_a_feed_outage(database: Database) -> None:
    """`expected_success_after=None` means nothing was due, which is what a holiday is.

    The calendar question lives with the caller on purpose. Deciding when a collection was owed
    is a market-session question, and an ingestion module that owned one would have to be told
    about half-days and holidays to avoid crying wolf every long weekend.
    """
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        holiday = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=None,
            record_incident=True,
            now=NOW + timedelta(days=4),
        )
        assert holiday.health is SourceHealth.HEALTHY
        assert holiday.reason == "nothing was due since the last success"
        assert plane.open_incidents(SOURCE) == [], "a closed market opened an incident"

        # The identical clock reading, with a session that did complete, is stale.
        trading_day = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=NOW + timedelta(days=3),
            now=NOW + timedelta(days=4),
        )
        assert trading_day.health is SourceHealth.STALE


def test_a_disabled_source_is_disabled_and_not_healthy_and_not_crashed(
    database: Database,
) -> None:
    """Three facts, three answers. A boolean would have to pick one and be wrong twice."""
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)
        plane.set_enabled(SOURCE, enabled=False, now=NOW, reason="provider quota exhausted")

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        status = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=NOW + timedelta(days=21),
            record_incident=True,
            now=NOW + timedelta(days=21),
        )
        assert status.health is SourceHealth.DISABLED
        assert status.collecting is False
        assert status.enabled is False
        assert plane.open_incidents(SOURCE) == [], "a deliberate pause is not an incident"

        # It is still refused work, rather than quietly collecting while reported as paused.
        with pytest.raises(IngestionError, match="is disabled"):
            plane.begin_run(
                ingestion_run_id="run-2", source_id=SOURCE, mode=RunMode.LIVE, now=NOW
            )

        # And the pause is on the record, so a later reader can tell it apart from a crash.
        paused = [
            event for event in _all_events(plane) if event.event_kind is EventKind.OPERATOR_PAUSED
        ]
        assert len(paused) == 1
        assert "provider quota exhausted" in paused[0].detail, (
            "the reason a source was paused is the whole point of recording the pause"
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        plane.set_enabled(SOURCE, enabled=True, now=NOW + timedelta(days=22), reason="quota reset")
        status = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=NOW + timedelta(days=21),
        )
        assert status.health is SourceHealth.STALE, (
            "resuming a paused source reveals the gap it accumulated; it does not erase it"
        )


def test_a_run_killed_mid_batch_is_closed_by_something_that_outlived_it(
    database: Database,
) -> None:
    """An unclosed run is indistinguishable from one still working, so it needs an outside call.

    This is the committed-run case, distinct from the rolled-back one above: a collector that
    committed its `RUNNING` row and then died leaves a row nothing will ever close from the
    inside.
    """
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        plane.begin_run(ingestion_run_id="run-1", source_id=SOURCE, mode=RunMode.LIVE, now=NOW)

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        assert plane.abandon_stale_runs(older_than=NOW, now=NOW + timedelta(hours=6)) == ()

        abandoned = plane.abandon_stale_runs(
            older_than=NOW + timedelta(hours=1), now=NOW + timedelta(hours=6)
        )
        assert [run.ingestion_run_id for run in abandoned] == ["run-1"]
        assert abandoned[0].status is RunStatus.ABANDONED
        assert abandoned[0].error_code == "RUN_NEVER_FINISHED"

        status = plane.status(SOURCE, stream_key=STREAM, expected_success_after=None)
        assert status.health is SourceHealth.FAILED, (
            "a source whose last run vanished is not healthy merely because nothing complained"
        )
        assert [event.event_kind for event in plane.open_incidents(SOURCE)] == [
            EventKind.RUN_ABANDONED
        ]


def test_the_operator_can_answer_when_each_source_last_succeeded(database: Database) -> None:
    """The acceptance criterion, asserted directly rather than assumed from the schema."""
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)
        plane.heartbeat(
            component_id="market-data-collector",
            instance_id="omen-1",
            status=HeartbeatStatus.HEALTHY,
            now=NOW,
            last_success_at=NOW,
        )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        status = plane.status(SOURCE, stream_key=STREAM, expected_success_after=None)
        assert status.last_success_at == NOW
        assert status.last_observation_at == _bar(1).timestamp
        assert status.latest_run_id == "run-1"
        assert status.latest_run_status is RunStatus.SUCCEEDED

        beat = plane.latest_heartbeat("market-data-collector")
        assert beat is not None
        assert beat.instance_id == "omen-1"
        assert beat.status is HeartbeatStatus.HEALTHY
        assert plane.latest_heartbeat("nobody") is None


def test_a_configuration_change_is_recorded_rather_than_applied_in_silence(
    database: Database,
) -> None:
    """A cursor carried across a configuration change points into a different dataset."""
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        plane.register_source(
            source_id=SOURCE,
            provider="alpaca",
            source_kind="equity-daily-bars",
            configuration_hash="b" * 64,
            now=NOW,
        )
        source = plane.get_source(SOURCE)
        assert source is not None
        assert source.configuration_hash == "b" * 64

        changes = [
            event
            for event in _all_events(plane)
            if event.event_kind is EventKind.CONFIGURATION_CHANGED
        ]
        assert len(changes) == 1

        # A different provider is a different source, not a reconfiguration of this one.
        with pytest.raises(IngestionError, match="a different provider or kind"):
            plane.register_source(
                source_id=SOURCE,
                provider="polygon",
                source_kind="equity-daily-bars",
                configuration_hash="c" * 64,
                now=NOW,
            )


def _all_events(plane: IngestionControlPlane) -> list[object]:
    from sqlalchemy import select

    from quantbot.ingestion.control import _event_from_row
    from quantbot.storage.schema import ingestion_events

    rows = (
        plane._session.execute(select(ingestion_events).order_by(ingestion_events.c.occurred_at))
        .mappings()
        .all()
    )
    return [_event_from_row(row) for row in rows]


def test_ingestion_never_consults_the_trading_kill_switch() -> None:
    """An engaged kill switch halts orders. It does not halt observation.

    Stopping collection because trading is halted destroys exactly the data needed to understand
    why it halted, and the coupling is the kind that gets added later by someone reaching for
    "the system is stopped" as one idea. Asserted against the module source, so adding the
    import is what fails rather than the behaviour it would enable.
    """
    import ast
    from pathlib import Path as _Path

    import quantbot.ingestion as package

    directory = _Path(package.__file__).parent
    modules = sorted(directory.glob("*.py"))
    assert modules, "the ingestion package has no modules to check"

    # Parsed rather than grepped. This file and `control.py` both *discuss* the kill switch at
    # length, and a substring check would either fail on the prose or be weakened until it
    # stopped catching the import. The AST sees identifiers, so only real references count.
    forbidden = {"kill_switch", "kill_switch_state", "KillSwitch", "KillSwitchState"}
    referenced: dict[str, set[str]] = {}
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.ImportFrom):
                names.update(part for part in (node.module or "").split("."))
                names.update(alias.name for alias in node.names)
                names.update(alias.asname for alias in node.names if alias.asname)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    names.update(alias.name.split("."))
        hits = names & forbidden
        if hits:
            referenced[module.name] = hits

    assert referenced == {}, (
        f"ingestion references trading kill-switch machinery: {referenced}. "
        "An engaged kill switch halts orders; gating collection on it destroys the observations "
        "needed to understand the halt."
    )

    # And prove the check can fail: the same walk over a module that *does* consult it.
    trading = ast.parse(
        (_Path(package.__file__).parents[1] / "operations" / "kill_switch.py").read_text(
            encoding="utf-8"
        )
    )
    trading_names = {
        node.attr for node in ast.walk(trading) if isinstance(node, ast.Attribute)
    } | {node.id for node in ast.walk(trading) if isinstance(node, ast.Name)}
    assert trading_names & forbidden, "the detector found nothing in a module that does use it"


def test_repeated_provider_failures_stay_one_visible_incident_with_a_countable_history(
    database: Database,
) -> None:
    """Every attempt is on the record; the outage is one incident, not five.

    Bounding the retry loop itself belongs to whatever drives collection -- this plane records
    attempts, it does not schedule them. What it must guarantee is that a provider failing all
    afternoon is *visible*: the attempts are countable afterwards, the source does not drift
    back to healthy because the last attempt was quiet, and the operator does not have to read
    five identical alerts to learn one thing.
    """
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-0", bars=[_bar(1)], now=NOW)

    for attempt in range(5):
        with database.transaction() as session:
            plane = IngestionControlPlane(session)
            moment = NOW + timedelta(hours=1, minutes=attempt)
            plane.begin_run(
                ingestion_run_id=f"retry-{attempt}",
                source_id=SOURCE,
                mode=RunMode.LIVE,
                now=moment,
            )
            plane.fail_run(
                f"retry-{attempt}",
                error_code="PROVIDER_RATE_LIMITED",
                now=moment,
                counts=RecordCounts(seen=0),
            )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        attempts = [run for run in plane.runs_for(SOURCE) if run.status is RunStatus.FAILED]
        assert len(attempts) == 5, "each attempt is countable after the fact"
        assert {run.error_code for run in attempts} == {"PROVIDER_RATE_LIMITED"}

        incidents = plane.open_incidents(SOURCE, event_kind=EventKind.PROVIDER_FAILURE)
        assert len(incidents) == 5, (
            "each failed attempt records its own provider failure, so the retry count is "
            "recoverable from the event log as well as the run log"
        )

        status = plane.status(SOURCE, stream_key=STREAM, expected_success_after=None)
        assert status.health is SourceHealth.FAILED, (
            "an earlier success does not rescue a source whose every recent attempt failed"
        )
        assert status.last_success_at == NOW, "and the earlier success is still readable"

    # One success clears the whole outage, and the history of it survives.
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        recovered_at = NOW + timedelta(hours=2)
        _collect(
            plane,
            StorageRepository(session),
            run_id="run-1",
            bars=[_bar(2)],
            now=recovered_at,
            expected_version=1,
        )
        status = plane.status(
            SOURCE,
            stream_key=STREAM,
            expected_success_after=NOW + timedelta(hours=1),
            record_incident=True,
            now=recovered_at,
        )
        assert status.health is SourceHealth.HEALTHY
        assert plane.open_incidents(SOURCE) == []
        assert len([run for run in plane.runs_for(SOURCE) if run.status is RunStatus.FAILED]) == 5


def test_the_status_report_answers_the_operator_question_without_writing_history(
    database: Database,
) -> None:
    """"When did each source last succeed?" -- and asking must not change the answer.

    A status command that recorded an incident would mean the act of investigating an outage
    changed the record of it. The report is asserted to be read-only by comparing the whole
    control-plane state before and after, not by inspecting what it happens to call.
    """
    from quantbot.ingestion import DEFAULT_STALE_AFTER_HOURS, ingestion_status

    assert DEFAULT_STALE_AFTER_HOURS == 100, (
        "the default matches scripts/supervisor.py::STALE_RUN_HOURS; a tighter bound cries "
        "wolf every long weekend"
    )

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        _collect(plane, StorageRepository(session), run_id="run-1", bars=[_bar(1)], now=NOW)

    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        before = (
            len(_all_events(plane)),
            len(plane.runs_for(SOURCE)),
            plane.cursor(SOURCE, STREAM),
        )

        fresh = ingestion_status(plane, now=NOW + timedelta(hours=1))
        assert fresh["unhealthy_streams"] == 0
        source = fresh["sources"][0]  # type: ignore[index]
        assert source["source_id"] == SOURCE
        assert source["enabled"] is True
        stream = source["streams"][0]
        assert stream["stream_key"] == STREAM
        assert stream["health"] == SourceHealth.HEALTHY.value
        assert stream["last_success_at"] == NOW.isoformat()
        assert stream["last_observation_at"] == _bar(1).timestamp.isoformat()
        assert stream["hours_since_success"] == 1.0
        assert source["latest_run"]["ingestion_run_id"] == "run-1"
        assert source["latest_run"]["mode"] == RunMode.LIVE.value

        # Three weeks later, with nothing collected, the same read reports the outage.
        stale = ingestion_status(plane, now=NOW + timedelta(days=21))
        assert stale["unhealthy_streams"] == 1
        assert stale["sources"][0]["streams"][0]["health"] == SourceHealth.STALE.value  # type: ignore[index]
        assert stale["sources"][0]["streams"][0]["hours_since_success"] == 504.0  # type: ignore[index]

        after = (
            len(_all_events(plane)),
            len(plane.runs_for(SOURCE)),
            plane.cursor(SOURCE, STREAM),
        )
        assert after == before, "reporting on collection changed the record of it"


def test_a_registered_source_that_never_collected_is_reported_rather_than_omitted(
    database: Database,
) -> None:
    """The silence this issue exists to break: nothing collected, nothing said.

    A per-stream loop over an empty cursor list would report a source that has never run as no
    rows at all -- which reads as "nothing to report" rather than "nothing has ever worked".
    """
    from quantbot.ingestion import ingestion_status

    with database.transaction() as session:
        _register(IngestionControlPlane(session))

    with database.transaction() as session:
        report = ingestion_status(IngestionControlPlane(session), now=NOW)
        assert report["unhealthy_streams"] == 1
        source = report["sources"][0]  # type: ignore[index]
        assert source["streams"] == [
            {
                "stream_key": None,
                "health": SourceHealth.NEVER_COLLECTED.value,
                "last_success_at": None,
                "last_observation_at": None,
                "hours_since_success": None,
                "reason": "no live run has ever succeeded, so there is no position to resume from",
            }
        ]
        assert source["latest_run"] is None


def test_an_unconfigured_collector_reports_nothing_configured_not_everything_healthy(
    database: Database,
) -> None:
    """Zero sources is the state the HP Omen was found in (#190). It is not health."""
    from quantbot.ingestion import ingestion_status

    with database.transaction() as session:
        report = ingestion_status(IngestionControlPlane(session), now=NOW)

    assert report["sources"] == []
    assert report["unhealthy_streams"] == 0
    assert "nothing is collecting, not that everything is healthy" in str(report["note"])


def test_two_attempts_at_one_instant_are_two_runs_and_not_a_duplicate_id(
    database: Database,
) -> None:
    """The defect the first real caller found within one test run.

    `f"{source_id}:{timestamp}"` looks obviously unique until a cycle is replayed under a frozen
    clock, or a retry lands inside the same second. Two attempts at one instant are two facts,
    and refusing the second as a duplicate would make a re-run crash instead of being recorded.

    The ordinal comes from what is already stored rather than from a random component, so the id
    stays reproducible -- a random suffix would make a run unfindable from its inputs.
    """
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)

        first = plane.next_run_id(SOURCE, NOW)
        plane.begin_run(
            ingestion_run_id=first, source_id=SOURCE, mode=RunMode.LIVE, now=NOW
        )
        plane.fail_run(first, error_code="PROVIDER_TIMEOUT", now=NOW)

        second = plane.next_run_id(SOURCE, NOW)
        assert second != first
        plane.begin_run(
            ingestion_run_id=second, source_id=SOURCE, mode=RunMode.LIVE, now=NOW
        )

        assert len(plane.runs_for(SOURCE)) == 2

        # Deterministic: asking again without recording anything gives the same next id.
        assert plane.next_run_id(SOURCE, NOW) == plane.next_run_id(SOURCE, NOW)
        # And a different instant is a different id regardless of the ordinal.
        assert plane.next_run_id(SOURCE, NOW + timedelta(seconds=1)) != second


def test_a_finished_run_cannot_be_finished_again(database: Database) -> None:
    """Append-only means the outcome of an attempt is written once."""
    with database.transaction() as session:
        plane = IngestionControlPlane(session)
        _register(plane)
        plane.begin_run(ingestion_run_id="run-1", source_id=SOURCE, mode=RunMode.LIVE, now=NOW)
        plane.complete_run("run-1", counts=RecordCounts(seen=1, inserted=1), now=NOW)

        with pytest.raises(IngestionError, match="already SUCCEEDED"):
            plane.complete_run("run-1", counts=RecordCounts(seen=99), now=NOW)
        with pytest.raises(IngestionError, match="already SUCCEEDED"):
            plane.fail_run("run-1", error_code="RETROACTIVE_FAILURE", now=NOW)

        run = plane.get_run("run-1")
        assert run is not None
        assert run.status is RunStatus.SUCCEEDED
        assert run.counts.seen == 1
        assert run.error_code is None
