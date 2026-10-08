"""Read-only operational fills bridge into separate synthetic experiment evidence."""

import hashlib
from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.experiment_lots import LotEvent, record_lot_event, resolve_lot
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.paper_deadlines import HEARTBEAT
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository
from quantbot.storage.lot_ownership import register_new_lot
from tests.unit.learner.test_experiment_contract import manifest_data
from tests.unit.learner.test_lot_ownership import capture
from tests.unit.learner.test_paper import NOW
from tests.unit.learner.test_trade_learning import episode
from tests.unit.learner.test_trade_learning import store as _store


@pytest.fixture
def context(tmp_path):
    data = manifest_data()
    data.update(registered_at=NOW - timedelta(days=1), prospective_start_at=NOW)
    data["roster"] = tuple(
        dict(row, symbol="DIA" if i == 0 else row["symbol"], verified_at=NOW - timedelta(days=2))
        for i, row in enumerate(data["roster"])
    )
    manifest = ExperimentManifest(**data)
    journal = ExperimentStore(tmp_path / "journal.db", create=True)
    journal.append(manifest, idempotency_key="manifest")
    for operational in _store.__wrapped__(tmp_path):

        def before_orders(session, record):
            lot_id = register_new_lot(
                session,
                record,
                capture(),
                run_id="run",
                experiment_id=manifest.experiment_id,
                decision_id="b" * 64,
                now=NOW,
                clock_healthy=True,
                synthetic=True,
            )
            record_lot_event(
                journal,
                LotEvent(
                    experiment_id=manifest.experiment_id,
                    lot_id=lot_id,
                    decision_id="b" * 64,
                    symbol="DIA",
                    account_id_hash=record.account_id_hash,
                    recorded_at=NOW,
                    occurred_at=NOW,
                    kind="ENTRY_DECLARED",
                    order_id=record.entry_intent.intent_id,
                    quantity=0,
                    position_before_entry=0,
                    source_hash=content_id(capture().evidence()),
                    synthetic=True,
                ),
            )

        record = episode(operational, before_orders=before_orders)
        yield manifest, journal, operational[0], record


def test_verified_operational_fills_bridge_without_mutating_runtime_database(context):
    manifest, journal, database, record = context
    with database.transaction() as session:
        StorageRepository(session).record_signal(
            "synthetic-heartbeat",
            run_id="run",
            strategy_id=record.identity.strategy_id,
            symbol="PAPER_DEADLINES",
            occurred_at=NOW + timedelta(minutes=1),
            payload={
                "kind": HEARTBEAT,
                "account_id_hash": record.account_id_hash,
                "healthy": True,
                "reasons": (),
            },
        )
        StorageRepository(session).finish_run(
            "run", finished_at=record.deadline_at, status="SUCCEEDED", detail={}
        )
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    reader = import_module("quantbot.learner.experiment_lot_reader")
    events = reader.refresh_lot_journal(
        journal, manifest, operational_path=database.path, now=NOW + timedelta(hours=1)
    )
    assert any(row.kind == "FILL" for row in events)
    outcome = resolve_lot(journal, events[0].lot_id, now=NOW + timedelta(hours=1))
    assert outcome.status == "COMPLETE"
    assert outcome.net_pnl == Decimal("1.70")
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
    assert (
        reader.refresh_lot_journal(
            journal, manifest, operational_path=database.path, now=NOW + timedelta(hours=2)
        )
        == ()
    )


def test_unobserved_protection_never_becomes_verified_profit(context):
    manifest, journal, database, _ = context
    reader = import_module("quantbot.learner.experiment_lot_reader")
    events = reader.refresh_lot_journal(
        journal, manifest, operational_path=database.path, now=NOW + timedelta(hours=1)
    )
    outcome = resolve_lot(journal, events[0].lot_id, now=NOW + timedelta(hours=1))
    assert outcome.net_pnl is None
    assert "PROTECTION_UNVERIFIED" in outcome.reasons


def test_deadline_broker_clock_bridge_preserves_original_timestamps(context):
    from sqlalchemy import update

    from quantbot.learner.experiment_lots import DeadlineBrokerClockEvidence
    from quantbot.storage.schema import broker_orders, fills, order_intents

    manifest, journal, database, record = context
    local = record.deadline_at
    with database.transaction() as session:
        session.execute(
            update(order_intents)
            .where(order_intents.c.side == "SELL")
            .values(created_at=local.isoformat())
        )
        session.execute(
            update(broker_orders)
            .where(broker_orders.c.side == "SELL")
            .values(submitted_at=(local - timedelta(milliseconds=121)).isoformat())
        )
        session.execute(
            update(fills)
            .where(fills.c.side == "SELL")
            .values(occurred_at=(local - timedelta(milliseconds=117)).isoformat())
        )
        repo = StorageRepository(session)
        repo.record_signal(
            "clock-heartbeat",
            run_id="run",
            strategy_id=record.identity.strategy_id,
            symbol="PAPER_DEADLINES",
            occurred_at=NOW + timedelta(minutes=1),
            payload={
                "kind": HEARTBEAT,
                "account_id_hash": record.account_id_hash,
                "healthy": True,
                "reasons": (),
            },
        )
        repo.finish_run("run", finished_at=local, status="SUCCEEDED", detail={})
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    reader = import_module("quantbot.learner.experiment_lot_reader")
    events = reader.refresh_lot_journal(
        journal, manifest, operational_path=database.path, now=NOW + timedelta(hours=1)
    )
    outcome = resolve_lot(journal, events[0].lot_id, now=NOW + timedelta(hours=1))
    assert outcome.status == "COMPLETE"
    assert outcome.net_pnl == Decimal("1.70")
    exit_link = next(row for row in events if row.kind == "ORDER_LINK" and row.side == "SELL")
    exit_fill = next(row for row in events if row.kind == "FILL" and row.side == "SELL")
    assert exit_link.occurred_at == local
    assert exit_fill.occurred_at == local - timedelta(milliseconds=117)
    anchors = [
        row.decode(DeadlineBrokerClockEvidence)
        for row in journal.read_as_of(manifest.experiment_id, NOW + timedelta(hours=1))
        if row.kind == "DeadlineBrokerClockEvidence"
    ]
    assert len(anchors) == 1
    assert anchors[0].submitted_at == local - timedelta(milliseconds=121)
    assert anchors[0].ownership_hash == content_id(exit_link)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before
    assert (
        reader.refresh_lot_journal(
            journal, manifest, operational_path=database.path, now=NOW + timedelta(hours=2)
        )
        == ()
    )
