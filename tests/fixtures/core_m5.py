"""Accepted populated v33 replay; fixture copies only, never operational stores."""

import gzip
import json
from datetime import timedelta
from pathlib import Path

from quantbot.domain import Account, Position
from quantbot.paper_core.contracts import CoreConfig, LearningReceipt, ModelArtifact, Observation
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database, StorageRepository


def accepted(path):
    fixture = json.loads(
        gzip.decompress((Path(__file__).parent / "core_v33_m4.json.gz").read_bytes())
    )
    db = Database.create(path / "core.db")
    with db.engine.begin() as connection:
        connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")
        for name, rows in fixture.items():
            if name in {"alembic_version", "schema_version"} or not rows:
                continue
            if name == "kill_switch_state":
                connection.exec_driver_sql("DELETE FROM kill_switch_state")
            for row in rows:
                keys = tuple(row)
                connection.exec_driver_sql(
                    f'INSERT INTO "{name}" ({",".join(keys)}) VALUES '
                    f"({','.join('?' for _ in keys)})",
                    tuple(row[k] for k in keys),
                )
    config = CoreConfig.model_validate_json(fixture["core_identities"][0]["payload"])
    store = CoreStore(db, config)
    receipt = LearningReceipt.model_validate_json(fixture["core_learning_runs"][0]["payload"])
    parent = store.read("core_models", receipt.parent_model_id, ModelArtifact)
    candidate = store.read("core_models", receipt.resulting_model_id, ModelArtifact)
    baseline = json.loads(fixture["core_fixture_baselines"][0]["payload"])
    now = receipt.available_at + timedelta(hours=14)
    broker = SyntheticBroker(
        account_id=config.account_id,
        now=now,
        initial_account=Account.model_validate(baseline["account"]),
        initial_positions=tuple(Position.model_validate(p) for p in baseline["positions"]),
    )
    with db.transaction() as session:
        repo = StorageRepository(session)
        broker.orders = {o.client_order_id: o for o in repo.list_broker_orders()}
        broker.fills = list(repo.list_fills())
    proof = store.latest_proof()
    broker.cash = proof.account.cash
    broker.quantities = dict(proof.actual_quantities)
    broker.basis = {p.symbol: p.average_entry_price for p in proof.positions}
    broker.set_mark(config.symbol, 100, as_of=now)
    observation = Observation.model_validate_json(fixture["core_observations"][0]["payload"])
    positive = next(
        Observation.model_validate_json(r["payload"])
        for r in fixture["core_observations"]
        if dict(Observation.model_validate_json(r["payload"]).features)["return_5m"] > 0
    )
    observation = positive.model_copy(
        update={
            "recorded_at_utc": now,
            "available_at": now,
            "source_end": now,
            "source_start": now - timedelta(minutes=35),
            "dependency_available_at": (now,),
            "session_close": now + timedelta(hours=5),
            "sequence": 1000,
            "raw_data_digest": "m5-later-unseen-positive",
        }
    )
    return db, store, broker, parent, candidate, receipt, observation
