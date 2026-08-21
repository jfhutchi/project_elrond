"""Persist the promotion ladder and its history.

Revision ID: 0008
Revises: 0007

`promotion.py` had `Stage`, `promote()`, `demote()` and forward-day counting, and nowhere to put
the answer. Promotion state lived only as long as the process that computed it (#16).

Two tables, and the second is the point. `promotion_state` is where a strategy is now;
`promotion_events` is every move it ever made. A ladder that stores only the current stage can be
walked backwards and rewritten with no trace, which for a mechanism whose entire purpose is
"this earned its way here" would make the record worthless. Events are append-only: nothing in
`StorageRepository` updates or deletes one.

Purely additive. No existing row is read or rewritten, so a paper database carries its ledger
across untouched.

Note on numbering: `codex/kronos-shadow-provider` is unpushed at the time of writing and may also
add an 0008. If both land, one rebases onto the other -- alembic will otherwise see two heads.

The tables are declared literally rather than imported from `schema.py`, for the reason recorded
in `REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    update,
)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V8_TABLES = ("promotion_state", "promotion_events")


def v8_tables() -> MetaData:
    """The ladder exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "promotion_state",
        metadata,
        Column("strategy_id", String(128), primary_key=True),
        Column("stage", String(32), nullable=False),
        Column("strategy_version", String(64), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("reason", Text, nullable=False),
        Column("actor", String(128), nullable=False),
        Column("updated_at", String(40), nullable=False),
    )
    events = Table(
        "promotion_events",
        metadata,
        Column("event_id", Integer, primary_key=True, autoincrement=True),
        Column("strategy_id", String(128), nullable=False),
        # Null on the first event, which is the strategy entering the ladder.
        Column("from_stage", String(32)),
        Column("to_stage", String(32), nullable=False),
        # Stored rather than derived from the stage ranks, because the ranks are code and this
        # row has to stay readable after the ladder is reshaped.
        Column("direction", String(16), nullable=False),
        Column("strategy_version", String(64), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("reason", Text, nullable=False),
        Column("actor", String(128), nullable=False),
        Column("occurred_at", String(40), nullable=False),
        CheckConstraint("direction IN ('PROMOTION', 'DEMOTION')", name="direction_known"),
    )
    Index("ix_promotion_events_strategy", events.c.strategy_id, events.c.occurred_at)
    return metadata


def upgrade() -> None:
    """Create the ladder tables and bump the version marker."""
    connection = op.get_bind()
    metadata = v8_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V8_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=8))


def downgrade() -> None:
    """Drop the ladder tables. The history goes with them, which is why this is not routine."""
    connection = op.get_bind()
    metadata = v8_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V8_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=7))
