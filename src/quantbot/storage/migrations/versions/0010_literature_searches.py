"""Add durable literature searches, so an outage cannot be forgotten as an absence.

Revision ID: 0010
Revises: 0009

`ArxivScout` has distinguished "we looked and found nothing" from "we could not look" since it
was written, and `LiteratureSearch` carries the query and timestamp of a zero-result search on
purpose. None of it survived the process. So the distinction the connector was careful to make
existed for the length of one run and then vanished, which meant a later reader could not tell a
gap in the evidence from a gap in the search -- the exact confusion #4 opened to prevent.

Three outcomes rather than a nullable result set, because a null is a shape and these are facts.
An outage is constrained to a match count of zero: recording one against a request that never
completed would be inventing a number nobody received.

Purely additive: one new table, no existing row read or rewritten.

Declared literally rather than imported from `schema.py`, for the reason recorded in `REFUTED.md`
and enforced by `test_no_migration_imports_the_live_schema_module`.
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
    text,
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


revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _literature_searches() -> Table:
    """The table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "literature_searches",
        metadata,
        Column("search_id", String(128), primary_key=True),
        Column("connector", String(64), nullable=False),
        Column("query", Text, nullable=False),
        Column("outcome", String(16), nullable=False),
        Column("attempted_at", String(40), nullable=False),
        Column("total_matched", Integer, nullable=False),
        Column("results_json", Text, nullable=False, server_default=text("'[]'")),
        Column("detail", Text, nullable=False, server_default=text("''")),
        CheckConstraint(
            "outcome IN ('RESULTS', 'NO_RESULTS', 'OUTAGE')", name="known_search_outcome"
        ),
        CheckConstraint("total_matched >= 0", name="matched_not_negative"),
        CheckConstraint(
            "outcome != 'OUTAGE' OR total_matched = 0", name="an_outage_matched_nothing"
        ),
    )
    Index("ix_literature_searches_connector", table.c.connector)
    Index("ix_literature_searches_attempted", table.c.attempted_at)
    return table


def upgrade() -> None:
    """Create the literature-search log and mark the schema version."""
    connection = op.get_bind()
    _literature_searches().create(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=10))


def downgrade() -> None:
    """Drop the literature-search log."""
    connection = op.get_bind()
    _literature_searches().drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=9))
