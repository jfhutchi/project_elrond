"""Add durable search records, so a trial count can be measured rather than declared.

Revision ID: 0009
Revises: 0008

`SearchOrigin.MEASURED` has been refused since 0007 because nothing emitted search telemetry:
accepting it on trust would have reopened the laundering path one enum value across from where
#23 closed it. `SubprocessWorker` now reports a disclosed cardinality, so a producer exists and
the origin can be derived from a record instead of asserted by the registrant.

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


revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _search_runs() -> Table:
    """The table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "search_runs",
        metadata,
        Column("search_id", String(128), primary_key=True),
        Column("actor", String(128), nullable=False),
        Column("actor_version", String(64), nullable=False),
        Column("candidates_evaluated", Integer, nullable=False),
        Column("dataset", String(128), nullable=False),
        Column("start_date", String(10), nullable=False),
        Column("end_date", String(10), nullable=False),
        Column("data_role", String(32), nullable=False),
        Column("started_at", String(40), nullable=False),
        Column("finished_at", String(40), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
        CheckConstraint("candidates_evaluated >= 1", name="searched_something"),
        CheckConstraint("end_date >= start_date", name="search_range_ordered"),
    )
    Index("ix_search_runs_dataset", table.c.dataset, table.c.start_date)
    Index("ix_search_runs_actor", table.c.actor)
    return table


def upgrade() -> None:
    """Create the search-run log and mark the schema version."""
    connection = op.get_bind()
    _search_runs().create(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=9))


def downgrade() -> None:
    """Drop the search-run log."""
    connection = op.get_bind()
    _search_runs().drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=8))
