"""Real SQLite contention must not kill independent PAPER exit supervision."""
import asyncio
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy.exc import OperationalError

from quantbot.storage import StorageRepository
from tests.unit.learner.test_paper_deadlines import (
    fill_entry,
    paper_store,
    prepare,
)

setup = paper_store


@pytest.mark.asyncio
@pytest.mark.parametrize("after_exit", [False, True])
async def test_real_busy_retries_fresh_cycle_without_false_heartbeat_or_duplicate_exit(
    setup, monkeypatch, after_exit, capsys,
):
    record, broker, supervisor = prepare(setup)
    database, _, _ = setup
    if after_exit:
        fill_entry(setup, record, broker)
        broker.now = record.deadline_at
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=0")
    writer = sqlite3.connect(database.path, timeout=0)
    stop = asyncio.Event()
    original_run = supervisor.run_once
    original_signal = StorageRepository.record_signal
    attempts = []
    failures = []
    waits = []
    original_wait_for = asyncio.wait_for

    async def observed_wait_for(awaitable, timeout):
        if timeout == 0.02:
            waits.append(len(attempts))
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(asyncio, "wait_for", observed_wait_for)

    if not after_exit:
        writer.execute("BEGIN IMMEDIATE")
    else:
        def blocked_publication(self, *args, **kwargs):
            if kwargs.get("symbol") == "PAPER_DEADLINES" and not failures:
                writer.execute("BEGIN IMMEDIATE")
            return original_signal(self, *args, **kwargs)
        monkeypatch.setattr(StorageRepository, "record_signal", blocked_publication)

    async def observed_run():
        attempts.append(broker.now)
        broker.now += timedelta(seconds=10)
        try:
            result = await original_run()
        except OperationalError as error:
            failures.append(error.orig.sqlite_errorcode)
            # A failed publication must never manufacture a healthy heartbeat.
            with database.transaction() as session:
                assert not [s for s in StorageRepository(session).list_signals()
                            if s.symbol == "PAPER_DEADLINES"]
            writer.rollback()
            raise
        stop.set()
        return result

    monkeypatch.setattr(supervisor, "run_once", observed_run)
    try:
        await asyncio.wait_for(supervisor.serve(stop, interval_seconds=0.02), timeout=4)
    finally:
        writer.rollback()
        writer.close()
    assert failures == [sqlite3.SQLITE_BUSY]
    assert len(attempts) == 2
    # Windows monotonic resolution can be 15.6ms. Prove each attempt awaits
    # the configured interruptible interval instead of timing two coarse samples.
    assert waits == [1, 2]
    with database.transaction() as session:
        heartbeats = [s for s in StorageRepository(session).list_signals()
                      if s.symbol == "PAPER_DEADLINES"]
    assert len(heartbeats) == 1
    assert heartbeats[0].payload["healthy"] is True
    assert len(broker.submitted) == (1 if after_exit else 0)
    output = capsys.readouterr().out
    assert "PAPER_DEADLINE_SQLITE_RETRY" in output
    assert "sqlite_code=5" in output
    assert "INSERT" not in output and "parameters" not in output


@pytest.mark.asyncio
async def test_nonbusy_sqlite_error_remains_fatal(setup, monkeypatch):
    _, _, supervisor = prepare(setup)
    underlying = sqlite3.OperationalError("private SQL must never be logged")
    underlying.sqlite_errorcode = sqlite3.SQLITE_IOERR
    failure = OperationalError("secret SQL", {"credential": "secret"}, underlying)

    async def fail():
        raise failure

    monkeypatch.setattr(supervisor, "run_once", fail)
    with pytest.raises(OperationalError) as raised:
        await supervisor.serve(asyncio.Event(), interval_seconds=0.02)
    assert raised.value is failure


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [sqlite3.SQLITE_LOCKED, sqlite3.SQLITE_BUSY_SNAPSHOT])
async def test_extended_contention_codes_retry_but_stop_remains_responsive(
    setup, monkeypatch, code, capsys,
):
    _, _, supervisor = prepare(setup)
    stop = asyncio.Event()
    underlying = sqlite3.OperationalError("private payload")
    underlying.sqlite_errorcode = code

    async def fail():
        stop.set()
        raise OperationalError("private SQL", {"secret": "private"}, underlying)

    monkeypatch.setattr(supervisor, "run_once", fail)
    await asyncio.wait_for(supervisor.serve(stop, interval_seconds=10), timeout=1)
    output = capsys.readouterr().out
    assert f"sqlite_code={code}" in output
    assert "private" not in output


def test_worker_logs_failure_class_and_stage_without_sql_or_parameters(
    tmp_path, monkeypatch, capsys,
):
    from types import SimpleNamespace

    import quantbot.runtime
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.learner import execution_worker

    database = tmp_path / "paper.db"
    database.touch()
    environment = tmp_path / "fixture.env"
    environment.write_text("TRADING_MODE=PAPER\n")
    monkeypatch.setattr("sys.argv", ["deadline", "--commit", "d" * 40, "--database",
                                   str(database), "--env-file", str(environment)])
    monkeypatch.setattr(execution_worker.subprocess, "check_output",
                        lambda argv, **kwargs: "d" * 40 if argv[1] == "rev-parse" else "")
    monkeypatch.setattr(execution_worker, "Settings", lambda: SimpleNamespace(
        TRADING_MODE=TradingMode.PAPER, BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
        EXPECTED_ACCOUNT_ID="synthetic-paper-account"))
    monkeypatch.setattr(quantbot.runtime, "runtime_paths",
                        lambda: SimpleNamespace(database=database))

    async def fail(settings):
        raise OperationalError("private SQL", {"credential": "secret"},
                               sqlite3.OperationalError("private exception message"))

    monkeypatch.setattr(execution_worker, "serve_deadlines", fail)
    assert execution_worker.main() == 1
    output = capsys.readouterr().out
    assert "PAPER_DEADLINE_START_FAILED" in output
    assert "stage=SERVE" in output and "error_class=OperationalError" in output
    assert "private" not in output and "secret" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize("lookup_found", [False, True])
async def test_busy_after_broker_acceptance_never_resubmits_uncertain_exit(
    setup, monkeypatch, lookup_found,
):
    from quantbot.domain import IntentState, OrderSide
    from quantbot.execution import coordinator

    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.now = record.deadline_at
    database, _, _ = setup
    with database.engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA busy_timeout=0")
    writer = sqlite3.connect(database.path, timeout=0)
    original = coordinator.persist_broker_acknowledgement
    blocked = []

    def blocked_ack(repository, intent, order):
        if intent.side is OrderSide.SELL and not blocked:
            blocked.append(intent.intent_id)
            writer.execute("BEGIN IMMEDIATE")
            try:
                return original(repository, intent, order)
            finally:
                writer.rollback()
        return original(repository, intent, order)

    monkeypatch.setattr(coordinator, "persist_broker_acknowledgement", blocked_ack)
    try:
        first = await supervisor.run_once()
        assert not first.healthy and len(broker.submitted) == 1
        assert len(blocked) == 1
        with database.transaction() as session:
            stored = StorageRepository(session).get_order_intent(blocked[0])
            assert stored.state is IntentState.SUBMITTING
        original_lookup = broker.get_order_by_client_id

        async def lookup(client, *, expected=None):
            if expected is not None and expected.side is OrderSide.SELL and not lookup_found:
                return None
            return await original_lookup(client, expected=expected)

        monkeypatch.setattr(broker, "get_order_by_client_id", lookup)
        broker.now += timedelta(seconds=10)
        result = await supervisor.run_once()
        assert len(broker.submitted) == 1
        with database.transaction() as session:
            state = StorageRepository(session).get_order_intent(blocked[0]).state
        if lookup_found:
            assert result.healthy and state is IntentState.BROKER_ACCEPTED
        else:
            assert not result.healthy
            assert state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}
    finally:
        writer.close()
