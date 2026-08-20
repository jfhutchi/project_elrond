"""Create the initial durable audit schema.

Revision ID: 0001
Revises:

The tables are declared literally rather than imported from `schema.py`. Pinning only the table
*names* was not enough: `metadata.create_all(tables=[metadata.tables[n] for n in V1_TABLES])`
still takes each table's *shape* from the live schema module, so the day a later revision adds a
column, changes a type, or alters a constraint on a V1 table, this revision silently starts
creating the new shape. A fresh database replaying 0001 -> 0006 would then diverge from one that
actually lived through the chain, and the later migration would fail or double-apply.

Revision 0002 carried the same defect in its added form and was caught producing V3 column names
on a fresh database, which then failed 0003's rename with "no such column
hypotheses.expected_sharpe". This is that fix applied to the base revision.

Transcribed 2026-08-19 from the schema as it then stood, verified byte-identical against the live
metadata's DDL by `tests/unit/test_storage_migrations.py`. No later revision had altered a V1
table at that point -- only `hypotheses`, added in 0002 -- so today's shape was still the V1 shape
and freezing it here loses nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: The tables this revision creates, in dependency order.
V1_TABLES = (
    "schema_version",
    "strategy_deployments",
    "runs",
    "bars",
    "signals",
    "account_snapshots",
    "equity_snapshots",
    "positions",
    "order_intents",
    "broker_orders",
    "fills",
    "order_events",
    "reconciliation_runs",
    "reconciliation_diffs",
    "incidents",
    "qualification_days",
    "kill_switch_state",
)


def v1_tables() -> MetaData:
    """The schema exactly as this revision creates it, independent of `schema.py`."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "schema_version",
        metadata,
        Column("id", Integer(), primary_key=True),
        Column("version", Integer(), nullable=False),
        CheckConstraint("id = 1", name="singleton"),
    )
    Table(
        "strategy_deployments",
        metadata,
        Column("strategy_id", String(length=128), primary_key=True),
        Column("version", String(length=64), primary_key=True),
        Column("git_commit", String(length=128), nullable=False),
        Column("configuration_hash", String(length=128), nullable=False),
        Column("deployment_timestamp", String(length=40), nullable=False),
    )
    Table(
        "runs",
        metadata,
        Column("run_id", String(length=128), primary_key=True),
        Column("strategy_id", String(length=128), nullable=False),
        Column("strategy_version", String(length=64), nullable=False),
        Column("status", String(length=32), nullable=False),
        Column("started_at", String(length=40), nullable=False),
        Column("finished_at", String(length=40)),
        Column("detail_json", Text(), nullable=False, server_default=text("'{}'")),
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_deployments.strategy_id", "strategy_deployments.version"],
        ),
        Index("ix_runs_started_at", "started_at"),
    )
    Table(
        "bars",
        metadata,
        Column("bar_id", Integer(), primary_key=True, autoincrement=True),
        Column("symbol", String(length=32), nullable=False),
        Column("timestamp", String(length=40), nullable=False),
        Column("provider", String(length=64), nullable=False),
        Column("adjustment_metadata_json", Text(), nullable=False),
        Column("open", Text(), nullable=False),
        Column("high", Text(), nullable=False),
        Column("low", Text(), nullable=False),
        Column("close", Text(), nullable=False),
        Column("volume", Text(), nullable=False),
        Column("adjustment", Text(), nullable=False),
        UniqueConstraint(
            "symbol", "timestamp", "provider", "adjustment_metadata_json", name="observation"
        ),
        Index("ix_bars_symbol_timestamp", "symbol", "timestamp"),
    )
    Table(
        "signals",
        metadata,
        Column("signal_id", String(length=128), primary_key=True),
        Column("run_id", String(length=128), ForeignKey("runs.run_id"), nullable=False),
        Column("strategy_id", String(length=128), nullable=False),
        Column("symbol", String(length=32), nullable=False),
        Column("occurred_at", String(length=40), nullable=False),
        Column("payload_json", Text(), nullable=False),
        Index("ix_signals_run_symbol", "run_id", "symbol"),
    )
    Table(
        "account_snapshots",
        metadata,
        Column("snapshot_id", String(length=128), primary_key=True),
        Column("run_id", String(length=128), ForeignKey("runs.run_id")),
        Column("account_id", String(length=128), nullable=False),
        Column("captured_at", String(length=40), nullable=False),
        Column("cash", Text(), nullable=False),
        Column("buying_power", Text(), nullable=False),
        Column("equity", Text(), nullable=False),
        Column("currency", String(length=16), nullable=False),
        Index("ix_account_snapshots_account_time", "account_id", "captured_at"),
    )
    Table(
        "equity_snapshots",
        metadata,
        Column("snapshot_id", String(length=128), primary_key=True),
        Column(
            "account_snapshot_id",
            String(length=128),
            ForeignKey("account_snapshots.snapshot_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        Column("account_id", String(length=128), nullable=False),
        Column("captured_at", String(length=40), nullable=False),
        Column("equity", Text(), nullable=False),
        Column("cash", Text(), nullable=False),
        Index("ix_equity_snapshots_account_time", "account_id", "captured_at"),
    )
    Table(
        "positions",
        metadata,
        Column(
            "snapshot_id",
            String(length=128),
            ForeignKey("account_snapshots.snapshot_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("symbol", String(length=32), primary_key=True),
        Column("quantity", Text(), nullable=False),
        Column("market_price", Text(), nullable=False),
        Column("market_value", Text(), nullable=False),
        Column("average_entry_price", Text(), nullable=False),
    )
    Table(
        "order_intents",
        metadata,
        Column("intent_id", String(length=128), primary_key=True),
        Column("client_order_id", String(length=128), nullable=False, unique=True),
        Column("strategy_id", String(length=128), nullable=False),
        Column("symbol", String(length=32), nullable=False),
        Column("signal_date", String(length=10), nullable=False),
        Column("side", String(length=8), nullable=False),
        Column("order_type", String(length=16), nullable=False),
        Column("time_in_force", String(length=8), nullable=False),
        Column("quantity", Text(), nullable=False),
        Column("limit_price", Text()),
        Column("stop_price", Text()),
        Column("extended_hours", Boolean(), nullable=False),
        Column("state", String(length=32), nullable=False),
        Column("created_at", String(length=40), nullable=False),
        Index("ix_order_intents_state_created", "state", "created_at"),
    )
    Table(
        "broker_orders",
        metadata,
        Column("broker_order_id", String(length=128), primary_key=True),
        Column(
            "client_order_id",
            String(length=128),
            ForeignKey("order_intents.client_order_id"),
            nullable=False,
            unique=True,
        ),
        Column("symbol", String(length=32), nullable=False),
        Column("side", String(length=8), nullable=False),
        Column("order_type", String(length=16), nullable=False),
        Column("time_in_force", String(length=8), nullable=False),
        Column("quantity", Text(), nullable=False),
        Column("filled_quantity", Text(), nullable=False),
        Column("status", String(length=64), nullable=False),
        Column("submitted_at", String(length=40), nullable=False),
        Column("filled_average_price", Text()),
        Index("ix_broker_orders_status", "status"),
    )
    Table(
        "fills",
        metadata,
        Column("fill_id", String(length=128), primary_key=True),
        Column(
            "broker_order_id",
            String(length=128),
            ForeignKey("broker_orders.broker_order_id"),
            nullable=False,
        ),
        Column("symbol", String(length=32), nullable=False),
        Column("side", String(length=8), nullable=False),
        Column("quantity", Text(), nullable=False),
        Column("price", Text(), nullable=False),
        Column("occurred_at", String(length=40), nullable=False),
        Column("fee", Text(), nullable=False),
        Index("ix_fills_broker_time", "broker_order_id", "occurred_at"),
    )
    Table(
        "order_events",
        metadata,
        Column("event_id", String(length=128), primary_key=True),
        Column("intent_id", String(length=128), ForeignKey("order_intents.intent_id")),
        Column("broker_order_id", String(length=128), ForeignKey("broker_orders.broker_order_id")),
        Column("event_type", String(length=64), nullable=False),
        Column("occurred_at", String(length=40), nullable=False),
        Column("detail_json", Text(), nullable=False),
        Index("ix_order_events_intent_time", "intent_id", "occurred_at"),
    )
    Table(
        "reconciliation_runs",
        metadata,
        Column("reconciliation_id", String(length=128), primary_key=True),
        Column("run_id", String(length=128), ForeignKey("runs.run_id")),
        Column("status", String(length=32), nullable=False),
        Column("started_at", String(length=40), nullable=False),
        Column("completed_at", String(length=40), nullable=False),
        Column("summary_json", Text(), nullable=False),
        Index("ix_reconciliation_runs_completed", "completed_at"),
    )
    Table(
        "reconciliation_diffs",
        metadata,
        Column("diff_id", String(length=128), primary_key=True),
        Column(
            "reconciliation_id",
            String(length=128),
            ForeignKey("reconciliation_runs.reconciliation_id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("category", String(length=64), nullable=False),
        Column("record_key", String(length=256), nullable=False),
        Column("expected_json", Text(), nullable=False),
        Column("actual_json", Text(), nullable=False),
        Index("ix_reconciliation_diffs_run", "reconciliation_id"),
    )
    Table(
        "incidents",
        metadata,
        Column("incident_id", String(length=128), primary_key=True),
        Column("run_id", String(length=128), ForeignKey("runs.run_id")),
        Column("severity", String(length=32), nullable=False),
        Column("kind", String(length=64), nullable=False),
        Column("message", Text(), nullable=False),
        Column("occurred_at", String(length=40), nullable=False),
        Column("resolved_at", String(length=40)),
        Column("detail_json", Text(), nullable=False),
        Index("ix_incidents_occurred", "occurred_at"),
    )
    Table(
        "qualification_days",
        metadata,
        Column("strategy_id", String(length=128), primary_key=True),
        Column("trading_date", String(length=10), primary_key=True),
        Column("qualified", Boolean(), nullable=False),
        Column("detail_json", Text(), nullable=False),
    )
    Table(
        "kill_switch_state",
        metadata,
        Column("id", Integer(), primary_key=True),
        Column("engaged", Boolean(), nullable=False),
        Column("reason", Text(), nullable=False),
        Column("updated_at", String(length=40), nullable=False),
        CheckConstraint("id = 1", name="singleton"),
    )
    return metadata


def upgrade() -> None:
    """Create all V1 tables, constraints, indices, and singleton state."""
    connection = op.get_bind()
    metadata = v1_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V1_TABLES])
    connection.execute(metadata.tables["schema_version"].insert().values(id=1, version=1))
    connection.execute(
        metadata.tables["kill_switch_state"]
        .insert()
        .values(
            id=1,
            engaged=True,
            reason="default fail-closed state",
            updated_at=datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z",
        )
    )


def downgrade() -> None:
    """Drop the V1 domain schema while Alembic retains its version table."""
    metadata = v1_tables()
    metadata.drop_all(
        bind=op.get_bind(),
        tables=[metadata.tables[name] for name in reversed(V1_TABLES)],
    )
