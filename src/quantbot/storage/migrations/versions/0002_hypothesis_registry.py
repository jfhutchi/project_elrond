"""Add the frozen hypothesis registry.

Revision ID: 0002
Revises: 0001

Purely additive: two new tables and the schema-version marker. No existing row is read or
rewritten, so a paper database carries its ledger across unchanged.

The tables are declared literally rather than imported from `schema.py`. A migration that
builds from live metadata stops describing what it actually created the moment a later revision
reshapes those tables -- and this one was caught doing exactly that: it produced the V3 column
names on a fresh database, which then failed 0003's rename with "no such column
hypotheses.expected_sharpe". Revision 0001 carried the same defect and was pinned for the same
reason.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
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


revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def v2_tables() -> tuple[MetaData, Table, Table]:
    """The registry exactly as this revision creates it. Also imported by 0003's rebuild."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    hypotheses = Table(
        "hypotheses",
        metadata,
        Column("hypothesis_id", String(128), primary_key=True),
        Column("version", Integer, primary_key=True),
        Column("family_id", String(128), nullable=False),
        Column("parent_hypothesis_id", String(128)),
        Column("parent_version", Integer),
        Column("registered_at", String(40), nullable=False),
        Column("content_hash", String(64), nullable=False, unique=True),
        Column("search_cardinality", Integer, nullable=False),
        Column("cumulative_trials", Integer, nullable=False),
        Column("luck_threshold_z", Text, nullable=False),
        Column("expected_sharpe", Text, nullable=False),
        Column("required_sessions", Integer, nullable=False),
        Column("available_sessions", Integer, nullable=False),
        Column("document_json", Text, nullable=False),
        ForeignKeyConstraint(
            ["parent_hypothesis_id", "parent_version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(parent_hypothesis_id IS NULL) = (parent_version IS NULL)",
            name="parent_is_whole",
        ),
    )
    Index("ix_hypotheses_family_id", hypotheses.c.family_id)

    windows = Table(
        "hypothesis_data_windows",
        metadata,
        Column("hypothesis_id", String(128), primary_key=True),
        Column("version", Integer, primary_key=True),
        Column("dataset", String(128), primary_key=True),
        Column("role", String(32), primary_key=True),
        Column("start_date", String(10), nullable=False),
        Column("end_date", String(10), nullable=False),
        ForeignKeyConstraint(
            ["hypothesis_id", "version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
            ondelete="CASCADE",
        ),
        CheckConstraint("end_date >= start_date", name="range_ordered"),
    )
    Index("ix_hypothesis_data_windows_dataset", windows.c.dataset, windows.c.start_date)
    return metadata, hypotheses, windows


def upgrade() -> None:
    """Create the registry tables and advance the schema marker to V2."""
    connection = op.get_bind()
    _, hypotheses, windows = v2_tables()
    hypotheses.create(bind=connection)
    windows.create(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=2))


def downgrade() -> None:
    """Drop the registry tables and return the marker to V1."""
    connection = op.get_bind()
    _, hypotheses, windows = v2_tables()
    windows.drop(bind=connection)
    hypotheses.drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=1))
