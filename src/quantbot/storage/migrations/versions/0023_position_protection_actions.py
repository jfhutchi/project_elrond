"""Durable protective-exit action outbox (#157).

Revision ID: 0023
Revises: 0022

An evaluation says an exit is required.  These tables preserve the separate fact that one
specific broker intent was created for that decision, and keep later evaluations linked to the
same active action rather than minting duplicate exits.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
    update,
)

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V23_TABLES = (
    "position_protection_actions",
    "position_protection_action_evaluations",
)


def _schema_version() -> Table:
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v23_tables() -> MetaData:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "order_intents",
        metadata,
        Column("intent_id", String(128), primary_key=True),
    )
    Table(
        "position_protection_evaluations",
        metadata,
        Column("evaluation_id", String(128), primary_key=True),
    )
    Table(
        "account_snapshots",
        metadata,
        Column("snapshot_id", String(128), primary_key=True),
    )
    actions = Table(
        "position_protection_actions",
        metadata,
        Column("action_id", String(128), primary_key=True),
        Column(
            "trigger_evaluation_id",
            String(128),
            ForeignKey("position_protection_evaluations.evaluation_id"),
            nullable=False,
            unique=True,
        ),
        Column("account_id", String(128), nullable=False),
        Column("symbol", String(32), nullable=False),
        Column("side", String(8), nullable=False),
        Column("quantity", Text, nullable=False),
        Column(
            "intent_id",
            String(128),
            ForeignKey("order_intents.intent_id"),
            nullable=False,
            unique=True,
        ),
        Column("client_order_id", String(128), nullable=False, unique=True),
        Column("status", String(32), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        Column("completed_at", String(40), nullable=True),
        Column(
            "flat_confirmation_snapshot_id",
            String(128),
            ForeignKey("account_snapshots.snapshot_id"),
            nullable=True,
        ),
        Column("flat_confirmed_at", String(40), nullable=True),
        Column("last_error", Text, nullable=False),
        Column("detail", Text, nullable=False),
        CheckConstraint("side IN ('BUY', 'SELL')", name="side_is_known"),
        CheckConstraint(
            "status IN ('PENDING', 'WORKING', 'UNCERTAIN', 'FILLED_UNVERIFIED', "
            "'COMPLETED', 'FAILED')",
            name="status_is_known",
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (completed_at IS NOT NULL)",
            name="completion_time_matches_status",
        ),
        CheckConstraint(
            "(flat_confirmation_snapshot_id IS NULL) = (flat_confirmed_at IS NULL)",
            name="flat_confirmation_fields_match",
        ),
        CheckConstraint(
            "(status = 'COMPLETED') = (flat_confirmation_snapshot_id IS NOT NULL)",
            name="completion_has_flat_confirmation",
        ),
    )
    Index(
        "uq_position_protection_actions_active_symbol",
        actions.c.account_id,
        actions.c.symbol,
        unique=True,
        sqlite_where=text(
            "status IN ('PENDING', 'WORKING', 'UNCERTAIN', 'FILLED_UNVERIFIED', 'FAILED')"
        ),
    )
    Index(
        "ix_position_protection_actions_status_time",
        actions.c.status,
        actions.c.updated_at,
    )

    links = Table(
        "position_protection_action_evaluations",
        metadata,
        Column(
            "action_id",
            String(128),
            ForeignKey("position_protection_actions.action_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column(
            "evaluation_id",
            String(128),
            ForeignKey("position_protection_evaluations.evaluation_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("linked_at", String(40), nullable=False),
    )
    Index(
        "ix_position_protection_action_evaluations_evaluation",
        links.c.evaluation_id,
    )
    return metadata


def upgrade() -> None:
    connection = op.get_bind()
    metadata = v23_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V23_TABLES])
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=23))


def downgrade() -> None:
    connection = op.get_bind()
    metadata = v23_tables()
    metadata.drop_all(
        bind=connection,
        tables=[metadata.tables[name] for name in reversed(V23_TABLES)],
    )
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=22))
