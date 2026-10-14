"""Predecessor capture recipe: run ONLY at accepted fbdfca2, before M5 changes.

The original invocation was an isolated pytest test at
tests/integration/paper_core/test_m5_capture.py. This archival copy uses an
explicit workspace destination so its recipe can be read outside that path.
The frozen .gz fixture, not re-execution, is the accepted prior-state witness.
"""

import gzip
import json
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from tests.fixtures.core_m4 import ExecutionClock, episodes, learning_calendar


@pytest.mark.asyncio
async def test_capture_accepted_v33(tmp_path):
    from quantbot.paper_core.contracts import LearningPolicy
    from quantbot.paper_core.learning import consider_learning

    db, store, broker, parent, trips, joins = await episodes(tmp_path)
    try:
        cutoff = broker.now + timedelta(hours=8)
        receipt = consider_learning(
            store, LearningPolicy(parent_model=parent), cutoff=cutoff,
            calendar=learning_calendar(), execution_clock=ExecutionClock(cutoff),
        )
        assert receipt.status == "AWAITING_ACTIVATION"
        with sqlite3.connect(db.path) as connection:
            connection.row_factory = sqlite3.Row
            tables = [r[0] for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ) if not r[0].startswith("sqlite_")]
            capture = {name: [dict(r) for r in connection.execute(f'SELECT * FROM "{name}"')]
                       for name in tables}
            assert connection.execute("SELECT version FROM schema_version").fetchone()[0] == 33
        output = Path.cwd() / "tests/fixtures/core_v33_m4.json.gz"
        if not output.exists():
            output.write_bytes(gzip.compress(json.dumps(capture, sort_keys=True).encode(), mtime=0))
        assert len(capture["core_round_trips"]) == 40
        assert len(capture["core_learning_runs"]) == 1
        print("M5_ACCEPTED_V33:" + json.dumps({
            "source": "fbdfca23f74bec146d19c630f001786c1123b2a6",
            "path": str(output), "receipt_id": receipt.id,
            "candidate_id": receipt.resulting_model_id, "table_count": len(tables),
        }, sort_keys=True))
    finally:
        db.close()
