"""Add research task orchestration.

Revision ID: 0006
Revises: 0005

Purely additive, and declaring its own tables for the reason recorded in `REFUTED.md`.
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

from quantbot.storage.schema import schema_version

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def v6_tables() -> tuple[Table, Table]:
    """The orchestration tables exactly as this revision creates them."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    tasks = Table(
        "research_tasks",
        metadata,
        Column("task_id", String(128), primary_key=True),
        Column("state", String(32), nullable=False),
        Column("hypothesis_id", String(128)),
        Column("hypothesis_version", Integer),
        Column("parent_task_id", String(128)),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
        Column("document_json", Text, nullable=False),
        CheckConstraint(
            "(hypothesis_id IS NULL) = (hypothesis_version IS NULL)",
            name="hypothesis_reference_is_whole",
        ),
    )
    Index("ix_research_tasks_state", tasks.c.state, tasks.c.updated_at)

    events = Table(
        "research_task_events",
        metadata,
        Column("event_id", Integer, primary_key=True, autoincrement=True),
        Column("task_id", String(128), ForeignKey("research_tasks.task_id"), nullable=False),
        Column("from_state", String(32)),
        Column("to_state", String(32), nullable=False),
        Column("actor", String(128), nullable=False),
        Column("reason", Text, nullable=False),
        Column("occurred_at", String(40), nullable=False),
        Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
    )
    Index("ix_research_task_events_task_id", events.c.task_id)
    return tasks, events


def upgrade() -> None:
    """Create the orchestration tables and advance the schema marker to V6."""
    connection = op.get_bind()
    for table in v6_tables():
        table.create(bind=connection)
    connection.execute(update(schema_version).where(schema_version.c.id == 1).values(version=6))


def downgrade() -> None:
    """Drop the orchestration tables and return the marker to V5."""
    connection = op.get_bind()
    for table in reversed(v6_tables()):
        table.drop(bind=connection)
    connection.execute(update(schema_version).where(schema_version.c.id == 1).values(version=5))
