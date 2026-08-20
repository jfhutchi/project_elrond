"""Add research memory.

Revision ID: 0004
Revises: 0003

Purely additive: two tables, and the schema-version marker. The tables are declared literally
rather than imported from `schema.py` -- three earlier revisions built from live metadata and
each stopped describing what it produced as soon as the next landed. That defect is recorded in
`REFUTED.md`; this revision does not repeat it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
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


revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def v4_tables() -> tuple[Table, Table]:
    """The memory tables exactly as this revision creates them."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    records = Table(
        "research_records",
        metadata,
        Column("record_id", String(128), primary_key=True),
        Column("kind", String(32), nullable=False),
        Column("verdict", String(32)),
        Column("subject", Text, nullable=False),
        Column("statement", Text, nullable=False),
        Column("hypothesis_id", String(128)),
        Column("hypothesis_version", Integer),
        Column("evidence_hash", String(64)),
        Column("source", Text, nullable=False),
        Column("curated", Boolean, nullable=False),
        Column("recorded_at", String(40), nullable=False),
        Column("document_json", Text, nullable=False),
        CheckConstraint(
            "(hypothesis_id IS NULL) = (hypothesis_version IS NULL)",
            name="hypothesis_reference_is_whole",
        ),
    )
    Index("ix_research_records_kind", records.c.kind, records.c.recorded_at)
    Index("ix_research_records_hypothesis_id", records.c.hypothesis_id)

    relationships = Table(
        "research_relationships",
        metadata,
        Column("source_id", String(128), primary_key=True),
        Column("relation", String(32), primary_key=True),
        Column("target_id", String(128), primary_key=True),
        Column("recorded_at", String(40), nullable=False),
        Column("note", Text, nullable=False, server_default=text("''")),
    )
    Index("ix_research_relationships_target_id", relationships.c.target_id)
    return records, relationships


def upgrade() -> None:
    """Create the memory tables and advance the schema marker to V4."""
    connection = op.get_bind()
    for table in v4_tables():
        table.create(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=4))


def downgrade() -> None:
    """Drop the memory tables and return the marker to V3."""
    connection = op.get_bind()
    for table in reversed(v4_tables()):
        table.drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=3))
