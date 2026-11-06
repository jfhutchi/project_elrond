"""Opening evidence is acquired honestly and is reusable after a restart."""

from datetime import timedelta

import pytest
from sqlalchemy import event
from tests.fixtures.core_m3 import NOW, prepare

from quantbot.paper_core.capital import opening_for_session
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.time_evidence import acquire_time_evidence


@pytest.mark.asyncio
async def test_future_session_does_not_scan_historical_opening_evidence(tmp_path):
    db, store, broker, proof, _, _, clocks = prepare(tmp_path)
    try:
        broker.clock_session_open = NOW + timedelta(days=1)
        broker.clock_session_close = broker.clock_session_open + timedelta(hours=6)
        broker.clock_session_id = broker.clock_session_open.date().isoformat()
        timing = await acquire_time_evidence(broker, clocks, context=store.config)
        statements = []

        def capture(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)

        event.listen(db.engine, "before_cursor_execute", capture)
        assert opening_for_session(store, timing, preferred_proof=proof) is None
        assert not statements, "A future session cannot have a witnessed opening yet"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_first_minute_opening_survives_restart_without_reanchoring(tmp_path):
    db, store, broker, _, _, _, clocks = prepare(tmp_path)
    try:
        broker.clock_session_open = NOW
        timing = await acquire_time_evidence(broker, clocks, context=store.config)
        store.record("core_time_evidence", timing)
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        opening = opening_for_session(store, timing, preferred_proof=proof)
        assert opening is not None
        clocks.advance(600)
        broker.now = clocks.wall
        restarted = CoreStore(db, store.config, clocks=clocks)
        later = await acquire_time_evidence(broker, clocks, context=store.config)
        assert opening_for_session(restarted, later) == opening
        assert opening.equity == proof.account.equity
        # No opening can be invented for a missed subsequent session.
        clocks.advance(86400)
        broker.clock_session_open = NOW + timedelta(days=1)
        broker.clock_session_close = broker.clock_session_open + timedelta(hours=6)
        broker.clock_session_id = broker.clock_session_open.date().isoformat()
        missing = await acquire_time_evidence(broker, clocks, context=store.config)
        assert opening_for_session(restarted, missing, preferred_proof=proof) is None
        assert not broker.posts
    finally:
        db.close()
