"""Capture accepted v34 activation/claim using the immutable accepted forty trades."""

import asyncio
import gzip
import hashlib
import json
from pathlib import Path

from tests.fixtures.core_m5 import accepted


async def capture(path):
    from quantbot.paper_core import activation
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, parent, candidate, receipt, observation = accepted(path)
    try:
        genesis = activation.initialize_fixture_model(store, parent, at=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        authority = activation.ActivationAuthority.fixture(
            store,
            candidate_id=candidate.id,
            receipt_id=receipt.id,
            expected_activation_id=genesis.id,
            at=broker.now,
        )
        activation.commission_fixture_authority(store, authority)
        event = activation.activate_model(
            store,
            expected_model_id=parent.id,
            candidate_id=candidate.id,
            authority=authority,
            at=broker.now,
        )
        decision = await trader_cycle(
            store,
            broker,
            observation,
            parent,
            store.latest_proof(),
            now=broker.now,
        )
        assert decision.activation_id == event.id and len(broker.posts) == 1
        with db.engine.connect() as connection:
            names = [
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            ]
            rows = {
                name: [
                    dict(row)
                    for row in connection.exec_driver_sql(f'SELECT * FROM "{name}"').mappings()
                ]
                for name in names
            }
        payload = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
        target = Path(__file__).with_name("core_v34_m5.json.gz")
        target.write_bytes(gzip.compress(payload, mtime=0))
        print(
            json.dumps(
                {
                    "schema": 34,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "activation_id": event.id,
                    "authority_id": authority.id,
                    "receipt_id": receipt.id,
                    "decision_id": decision.id,
                    "tables": {name: len(items) for name, items in rows.items()},
                }
            )
        )
    finally:
        db.close()


def test_capture_accepted_v34(tmp_path):
    asyncio.run(capture(tmp_path))
