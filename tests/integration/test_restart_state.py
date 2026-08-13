from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from quantbot.domain import IntentState, OrderIntent, OrderSide, OrderType, TimeInForce
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 13, 14, 30, tzinfo=UTC)


def make_unresolved(intent_id: str, state: IntentState) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        client_order_id=f"client-{intent_id}",
        strategy_id="mean-reversion",
        symbol="AAPL",
        signal_date=date(2026, 8, 13),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity="5.2500",
        limit_price="100.25",
        state=state,
        created_at=NOW,
    )


def test_restart_restores_unresolved_intents_and_safety_state(tmp_path: Path) -> None:
    database_path = tmp_path / "restart.db"
    first = Database(database_path)
    with first.transaction() as session:
        repository = StorageRepository(session)
        repository.create_order_intent(make_unresolved("submitting", IntentState.SUBMITTING))
        repository.create_order_intent(
            make_unresolved("submission-unknown", IntentState.SUBMISSION_UNKNOWN)
        )
        repository.set_kill_switch(True, reason="uncertain submission", updated_at=NOW)
    first.close()

    reopened = Database(database_path)
    try:
        with reopened.transaction() as session:
            repository = StorageRepository(session)
            restored = repository.list_unresolved_order_intents()
            assert {intent.state for intent in restored} == {
                IntentState.SUBMITTING,
                IntentState.SUBMISSION_UNKNOWN,
            }
            assert {intent.intent_id for intent in restored} == {
                "submitting",
                "submission-unknown",
            }
            assert all(intent.quantity == Decimal("5.25") for intent in restored)
            assert all(intent.created_at.tzinfo is UTC for intent in restored)
            kill_switch = repository.get_kill_switch_state()
            assert kill_switch.engaged is True
            assert kill_switch.reason == "uncertain submission"

        with reopened.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() == "ok"
            assert (
                connection.exec_driver_sql("SELECT version FROM schema_version").scalar_one() == 1
            )
    finally:
        reopened.close()
