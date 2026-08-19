"""Add the budget ledger.

Revision ID: 0005
Revises: 0004

Purely additive. The table is declared literally rather than imported from `schema.py`, for the
reason recorded in `REFUTED.md`: three earlier revisions built from live metadata and each
stopped describing what it produced as soon as the next one landed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    text,
    update,
)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported.

    A migration that reaches into `quantbot.storage.schema` stops describing the schema it
    actually operated on the moment a later revision reshapes that table.
    """
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def v5_table() -> Table:
    """The budget ledger exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    spend = Table(
        "budget_spend",
        metadata,
        Column("spend_id", Integer, primary_key=True, autoincrement=True),
        Column("budget_key", String(256), nullable=False),
        Column("category", String(32), nullable=False),
        Column("amount", Text, nullable=False),
        Column("task", Text, nullable=False),
        Column("recorded_at", String(40), nullable=False),
        Column("override_by", String(128)),
        Column("note", Text, nullable=False, server_default=text("''")),
    )
    Index("ix_budget_spend_budget_key", spend.c.budget_key, spend.c.category)
    Index("ix_budget_spend_override_by", spend.c.override_by)
    return spend


def upgrade() -> None:
    """Create the budget ledger and advance the schema marker to V5."""
    connection = op.get_bind()
    v5_table().create(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=5))


def downgrade() -> None:
    """Drop the budget ledger and return the marker to V4."""
    connection = op.get_bind()
    v5_table().drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=4))
