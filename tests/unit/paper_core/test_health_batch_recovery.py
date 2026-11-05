"""Batch recovery preserves scoped authority without replaying the full health log per symbol."""

import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import event
from tests.integration.paper_core.test_daily_selection import NOW, prepare

from quantbot.paper_core import health
from quantbot.paper_core.contracts import Health
from quantbot.paper_core.store import certify_inventory, table


def failure(store, *, symbol="AAPL", scope="SYMBOL", state="RETRYABLE_FAILURE", **updates):
    value = Health(
        recorded_at_utc=NOW,
        role="TRADER",
        scope=scope,
        scope_id=store.config.account_id if scope == "ACCOUNT" else symbol,
        state=state,
        objections=("RETRY",),
        first_blocker="RETRY",
        first_seen=NOW,
        last_seen=NOW,
        **updates,
    )
    return health.record_health(store, value)


def ready(store):
    return sorted(
        (d for d in store.documents("core_health_events") if d["state"] == "READY"),
        key=lambda d: d["sequence"],
    )


def test_batch_preserves_sequential_scope_provenance_and_reads_history_once(tmp_path):
    db, store, broker, proof, *_ = prepare(tmp_path)
    try:
        historic = Health(
            recorded_at_utc=NOW,
            role="TRADER",
            scope="SYMBOL",
            scope_id="HISTORY",
            state="READY",
            objections=(),
            first_blocker=None,
            first_seen=NOW,
            last_seen=NOW,
        )
        with db.transaction() as session:
            documents = [historic.model_copy(update={"sequence": n}) for n in range(1, 1001)]
            session.execute(
                table("core_health_events").insert(),
                [{"id": h.id, "payload": h.model_dump_json()} for h in documents],
            )
        account = failure(store, scope="ACCOUNT")
        aapl = failure(store)
        msft = failure(store, symbol="MSFT")
        unrelated = failure(store, symbol="NVDA")
        reads = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "core_health_events" in statement
                and "WHERE" not in statement
            ):
                reads.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        health.recover_health_many(
            store, proof, now=NOW, symbols=("MSFT", "AAPL", "MSFT", *(f"S{i}" for i in range(10)))
        )
        event.remove(db.engine, "before_cursor_execute", capture)
        recovered = [d for d in ready(store) if d["scope_id"] != "HISTORY"]
        assert [(d["scope_id"], d["recovered_from"]) for d in recovered] == [
            ("MSFT", [account, msft]),
            ("AAPL", [aapl]),
        ]
        assert [d["sequence"] for d in recovered] == [1005, 1006]
        assert len(store.documents("core_health_events")) == 1006
        assert all(d["new_proof_id"] == proof.id for d in recovered)
        assert len(reads) == 1
        with db.transaction() as session:
            assert [h.id for h in health.active_health(session)] == [unrelated]
        assert not broker.posts
    finally:
        db.close()


def test_batch_writer_cannot_interleave_recovery_or_sequence(tmp_path):
    db, store, broker, proof, *_ = prepare(tmp_path)
    peer = sqlite3.connect(db.engine.url.database, timeout=0)
    try:
        failure(store)
        observations = []

        def compete(conn, cursor, statement, parameters, context, executemany):
            if (
                statement.lstrip().upper().startswith("SELECT")
                and "core_health_events" in statement
                and "WHERE" not in statement
            ):
                with pytest.raises(sqlite3.OperationalError) as caught:
                    peer.execute("BEGIN IMMEDIATE")
                assert caught.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
                observations.append("writer excluded")

        event.listen(db.engine, "after_cursor_execute", compete)
        health.recover_health_many(store, proof, now=NOW, symbols=("AAPL",))
        event.remove(db.engine, "after_cursor_execute", compete)
        assert observations == ["writer excluded"]
        assert ready(store)[0]["sequence"] == 2
        later = failure(store, state="MANUAL_PAUSE")
        with db.transaction() as session:
            assert later in {h.id for h in health.active_health(session)}
        assert not broker.posts
    finally:
        peer.close()
        db.close()


@pytest.mark.parametrize("state", ["INTEGRITY_FAILURE", "MANUAL_PAUSE"])
@pytest.mark.parametrize("scope", ["ACCOUNT", "SYMBOL"])
def test_batch_preserves_active_quarantine_scope(tmp_path, state, scope):
    db, store, broker, proof, *_ = prepare(tmp_path)
    try:
        failure(store)
        failure(store, symbol="MSFT")
        blocked = failure(store, state=state, scope=scope)
        health.recover_health_many(store, proof, now=NOW, symbols=("AAPL", "MSFT"))
        assert [d["scope_id"] for d in ready(store)] == ([] if scope == "ACCOUNT" else ["MSFT"])
        with db.transaction() as session:
            assert blocked in {h.id for h in health.active_health(session)}
        assert not broker.posts
    finally:
        db.close()


@pytest.mark.parametrize(
    "refusal", ["same_generation", "same_evidence", "uncertified", "superseded"]
)
def test_batch_does_not_recover_without_new_current_certification(tmp_path, refusal):
    db, store, broker, proof, *_ = prepare(tmp_path)
    try:
        failure(
            store,
            failed_generation=proof.generation if refusal == "same_generation" else 0,
            evidence_ids=(proof.id,) if refusal == "same_evidence" else (),
        )
        candidate = proof
        if refusal in {"uncertified", "superseded"}:
            newer = proof.model_copy(
                update={
                    "generation": proof.generation + 1,
                    "recorded_at_utc": NOW + timedelta(seconds=1),
                }
            )
            if refusal == "superseded":
                certify_inventory(store, newer)
            else:
                candidate = newer
        health.recover_health_many(store, candidate, now=NOW, symbols=("AAPL",))
        assert ready(store) == []
        assert not broker.posts
    finally:
        db.close()


def test_batch_checks_current_funds_once_and_observes_prelock_quarantine(tmp_path, monkeypatch):
    from quantbot.paper_core import funds

    db, store, broker, proof, *_ = prepare(tmp_path)
    try:
        failure(store)
        failure(store, symbol="MSFT")
        original = funds.entry_inventory_objections
        checks = []

        def checked(*args, **kwargs):
            checks.append(1)
            return original(*args, **kwargs)

        monkeypatch.setattr(funds, "entry_inventory_objections", checked)
        # This committed quarantine must be read by the next locked snapshot.
        failure(store, scope="ACCOUNT", state="INTEGRITY_FAILURE")
        health.recover_health_many(store, proof, now=NOW, symbols=("AAPL", "MSFT"))
        assert len(checks) == 1
        assert ready(store) == []
        assert not broker.posts
    finally:
        db.close()


def test_batch_manual_kill_switch_and_single_symbol_wrapper(tmp_path):
    from quantbot.storage import StorageRepository

    db, store, broker, proof, *_ = prepare(tmp_path)
    try:
        failure(store)
        with db.transaction() as session:
            StorageRepository(session).set_kill_switch(True, reason="manual", updated_at=NOW)
        health.recover_health(store, proof, now=NOW, symbol="AAPL")
        assert ready(store) == []
        with db.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state().engaged
        assert not broker.posts
    finally:
        db.close()


@pytest.mark.asyncio
async def test_trader_recovers_only_fresh_members_in_one_batch(tmp_path, monkeypatch):
    from tests.fixtures.core_m3 import prepare as prepare_timed

    from quantbot.paper_core.contracts import ObservationCohort
    from quantbot.paper_core.trader import _acquire_entry_authority

    db, store, broker, proof, observation, model, clocks = prepare_timed(tmp_path)
    try:
        observations = tuple(
            observation.model_copy(update={"symbol": symbol}) for symbol in ("XLE", "AAPL")
        ) + (
            observation.model_copy(
                update={
                    "symbol": "MSFT",
                    "dependency_available_at": (NOW - timedelta(seconds=121),),
                }
            ),
        )
        captured = ObservationCohort(
            source_end=observation.source_end,
            session_open=broker.clock_session_open,
            session_close=observation.session_close,
            observations=observations,
        )
        original = health.recover_health_many
        calls = []

        def recover(*args, **kwargs):
            calls.append(kwargs["symbols"])
            return original(*args, **kwargs)

        monkeypatch.setattr(health, "recover_health_many", recover)
        await _acquire_entry_authority(store, broker, observations[0], captured, proof)
        assert calls == [("XLE", "AAPL")]
        assert not broker.posts
    finally:
        db.close()
