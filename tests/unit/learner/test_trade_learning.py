"""Synthetic PAPER trade attribution; never broker orders or learner evidence."""

import hashlib
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import update

from quantbot.domain import Fill, OrderSide
from quantbot.learner.five_minute import FiveMinuteFeatureRow
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.paper import PaperModelBinding
from quantbot.learner.paper_deadlines import (
    CLOSED_EVENT,
    EXIT_EVENT,
    PaperDeadline,
    record_deadline,
)
from quantbot.learner.trade_learning import journal_trade_episodes, read_trade_episodes
from quantbot.storage import StorageRepository
from quantbot.storage.schema import fills, order_events, order_intents, signals
from tests.integration.test_execution_coordinator import _accepted
from tests.unit.learner.test_paper import NOW, deploy, intent
from tests.unit.learner.test_paper import setup as _paper_setup


@pytest.fixture
def store(tmp_path):
    yield from _paper_setup.__wrapped__(tmp_path)


def episode(store, *, closed=True, exit_quantity=Decimal("1"), before_orders=None,
            entry_only=False):
    database, _, _ = store
    prediction = FiveMinutePrediction(
        model_id="a" * 64,
        features=FiveMinuteFeatureRow(
            symbol="DIA",
            decision_at=NOW,
            feature_at=NOW,
            target_at=NOW + timedelta(minutes=30),
            values=(0.0,) * 10,
            reference_close=100,
            benchmark_close=100,
            source_snapshot_hash="synthetic",
            receipt_at=NOW,
        ),
        recorded_at=NOW,
        score=0.002,
        cost_status="ABOVE_FIXED_COST_FLOOR",
    )
    binding = PaperModelBinding.from_config(
        model_id=prediction.model_id,
        artifact_hash="a" * 64,
        code_hash="b" * 64,
        execution_config={"bar_minutes": 5, "horizon_minutes": 30},
    )
    identity = binding.strategy_identity(git_commit="c" * 40, deployed_at=NOW)
    deploy((database, binding, identity))
    entry = intent(identity).model_copy(update={"symbol": "DIA"})
    record = PaperDeadline(
        entry_intent=entry,
        binding=binding,
        identity=identity,
        model_id=prediction.model_id,
        prediction_id=prediction.prediction_id,
        account_id_hash=PaperDeadline.account_hash("paper"),
        recorded_at=NOW,
        reference_at=NOW,
        entry_valid_until=NOW + timedelta(seconds=120),
        deadline_at=NOW + timedelta(minutes=30),
        session_exit_at=NOW.replace(hour=20),
        initial_stop=Decimal("99"),
    )
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.create_run("run", identity, started_at=NOW)
        repo.record_signal(
            "learner-forecast-" + prediction.prediction_id,
            run_id="run",
            strategy_id=identity.strategy_id,
            symbol="DIA",
            occurred_at=NOW,
            payload={
                "kind": "LEARNER_PAPER_FORECAST_V1",
                "prediction": prediction.model_dump(mode="json"),
                "binding": binding.model_dump(mode="json"),
            },
        )
        if before_orders is not None:
            before_orders(session, record)
        for order_intent, price, fee in (
            (entry, Decimal("100"), Decimal(".10")),
            (
                entry.model_copy(
                    update={
                        "intent_id": "exit",
                        "client_order_id": "exit-client",
                        "side": OrderSide.SELL,
                    }
                ),
                Decimal("102"),
                Decimal(".20"),
            ),
        ):
            if entry_only and order_intent.side is OrderSide.SELL:
                continue
            repo.create_order_intent(order_intent)
            quantity = Decimal("1") if order_intent.side == OrderSide.BUY else exit_quantity
            order = _accepted(order_intent).model_copy(
                update={
                    "broker_order_id": order_intent.intent_id + "-broker",
                    "status": "filled",
                    "filled_quantity": quantity,
                    "quantity": quantity,
                    "filled_average_price": price,
                },
            )
            repo.save_broker_order(order)
            repo.record_fill(
                Fill(
                    fill_id=order_intent.intent_id + "-fill",
                    broker_order_id=order.broker_order_id,
                    symbol="DIA",
                    side=order_intent.side,
                    quantity=quantity,
                    price=price,
                    fee=fee,
                    occurred_at=NOW if order_intent.side == OrderSide.BUY else record.deadline_at,
                )
            )
        if not entry_only:
            repo.record_order_event(
                "exit-event",
                event_type=EXIT_EVENT,
                occurred_at=record.deadline_at,
                intent_id="exit",
                detail={"handoff_id": record.handoff_id},
            )
        if closed and not entry_only:
            repo.record_order_event(
                "closed",
                event_type=CLOSED_EVENT,
                occurred_at=record.deadline_at,
                intent_id=entry.intent_id,
                detail={"handoff_id": record.handoff_id},
            )
    record_deadline(database, record, run_id="run")
    return record


def test_closed_trade_exact_forecast_fills_fees_and_read_only(store):
    database, _, _ = store
    record = episode(store)
    before = hashlib.sha256(database.path.read_bytes()).hexdigest()
    (result,) = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    assert result.prediction_id == record.prediction_id
    assert result.status == "CLOSED"
    assert result.realized_net_pnl == Decimal("1.70")
    assert result.fees == Decimal(".30")
    assert result.net_return == Decimal("1.70") / Decimal("100.10")
    assert result.entry_fill_ids == (record.entry_intent.intent_id + "-fill",)
    assert result.exit_fill_ids == ("exit-fill",)
    assert hashlib.sha256(database.path.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(
    "closed,quantity,status",
    [
        (False, Decimal("1"), "AWAITING_CLOSE_CONFIRMATION"),
        (True, Decimal(".5"), "INCOMPLETE_FILL_ATTRIBUTION"),
        (True, Decimal("2"), "INCOMPLETE_FILL_ATTRIBUTION"),
    ],
)
def test_partial_or_unconfirmed_never_becomes_training_profit(store, closed, quantity, status):
    database, _, _ = store
    episode(store, closed=closed, exit_quantity=quantity)
    (result,) = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    assert result.status == status
    assert result.realized_net_pnl is None
    assert result.net_return is None


def test_future_exit_does_not_leak_into_asof_outcome(store):
    database, _, _ = store
    episode(store)
    (result,) = read_trade_episodes(database.path, now=NOW + timedelta(minutes=10))
    assert result.status == "OPEN"
    assert result.exit_fill_ids == ()
    assert result.realized_net_pnl is None


def test_missing_database_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        read_trade_episodes(missing, now=NOW)
    assert not missing.exists()


@pytest.mark.parametrize(
    "change", ["late-forecast", "intent-quantity", "intent-client", "pre-intent-fill"]
)
def test_changed_or_unavailable_provenance_refused(store, change):
    database, _, _ = store
    record = episode(store)
    if change == "intent-client":
        # Deliberately corrupt the synthetic store without SQLite FK enforcement.
        with sqlite3.connect(database.path) as connection:
            connection.execute(
                "UPDATE order_intents SET client_order_id='other' WHERE intent_id=?",
                (record.entry_intent.intent_id,),
            )
    with database.transaction() as session:
        if change == "late-forecast":
            session.execute(
                update(signals)
                .where(signals.c.signal_id.startswith("learner-forecast"))
                .values(occurred_at=(NOW + timedelta(minutes=1)).isoformat())
            )
        if change == "intent-quantity":
            values = {"quantity": "2"}
            session.execute(
                update(order_intents)
                .where(order_intents.c.intent_id == record.entry_intent.intent_id)
                .values(**values)
            )
        if change == "pre-intent-fill":
            session.execute(
                update(fills)
                .where(fills.c.side == "BUY")
                .values(occurred_at=(NOW - timedelta(seconds=1)).isoformat())
            )
    with pytest.raises(ValueError):
        read_trade_episodes(database.path, now=NOW + timedelta(hours=1))


@pytest.mark.parametrize("change", ["sell-before-buy", "close-before-fill", "ambiguous-same-time"])
def test_balanced_totals_with_invalid_chronology_are_not_profit(store, change):
    database, _, _ = store
    episode(store)
    with database.transaction() as session:
        if change == "sell-before-buy":
            session.execute(
                update(fills)
                .where(fills.c.side == "BUY")
                .values(occurred_at=(NOW + timedelta(minutes=31)).isoformat())
            )
        elif change == "close-before-fill":
            session.execute(
                update(order_events)
                .where(order_events.c.event_type == CLOSED_EVENT)
                .values(occurred_at=(NOW + timedelta(minutes=29)).isoformat())
            )
        else:
            session.execute(
                update(fills).where(fills.c.side == "SELL").values(occurred_at=NOW.isoformat())
            )
    (result,) = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    assert result.status == "INCOMPLETE_FILL_ATTRIBUTION"
    assert result.realized_net_pnl is None


def test_immutable_journal_records_execution_costs_and_deduplicates_reads(store, tmp_path):
    database, _, _ = store
    episode(store)
    first = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    root = tmp_path / "isolated-journal"
    assert journal_trade_episodes(root, first) == 1
    later = read_trade_episodes(database.path, now=NOW + timedelta(hours=2))
    assert journal_trade_episodes(root, later) == 0
    from quantbot.learner.forward import _read

    (path,) = root.glob("*.json")
    document = _read(path)
    assert document["evidence"] == "ACTUAL_PAPER_BROKER_FILLS"
    assert document["entry_notional"] == "100"
    assert document["exit_notional"] == "102"
    assert document["fees"] == "0.3"
    assert document["realized_net_pnl"] == "1.7"
    assert document["order_authority"] is False


def test_open_journal_does_not_invent_realized_learning_target(store, tmp_path):
    database, _, _ = store
    episode(store, closed=False)
    rows = read_trade_episodes(database.path, now=NOW + timedelta(hours=1))
    assert journal_trade_episodes(tmp_path / "journal", rows) == 1
    from quantbot.learner.forward import _read

    document = _read(next((tmp_path / "journal").glob("*.json")))
    assert document["realized_net_pnl"] is None
    assert document["usable_executed_outcome"] is False
