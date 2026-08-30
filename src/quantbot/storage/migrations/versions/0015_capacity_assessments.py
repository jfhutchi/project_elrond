"""Persist how much capital the edge survives, so the milestone gate can read it.

Revision ID: 0015
Revises: 0014

#51's capacity ladder has been correct and unreachable since #62/#67. Nothing produced a
turnover, an allocation or a per-instrument volume, so `assess_milestone` received `None` and
reported the capacity question as permanently unassessed. This is the artifact it reads instead.

The distinction the milestone gate already documents matters here: `UNASSESSED` must mean *the
production artifact is absent or its inputs were incomplete*, never *issue #51 is open*. A gate
that reads an issue number can be opened by closing an issue, which is precisely the failure #52
was written to remove.

`binding_symbol` and `binding_constraint` are stored because "the strategy exhausts at $2m" tells
an operator nothing they can act on, while "XLU participation reaches 4% of a session at $2m"
tells them to change the roster.

`modelled_inputs` records which inputs rested on a versioned project default rather than a
measurement. An empty value is a strong claim -- it asserts every input was observed -- so it is
stored explicitly rather than inferred from absence.

Purely additive. No existing row is read or rewritten.

The table is declared literally rather than imported from `schema.py`, for the reason recorded in
`REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Index, Integer, MetaData, String, Table, Text, update

revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V15_TABLES = ("capacity_assessments",)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v15_tables() -> MetaData:
    """The capacity table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    assessments = Table(
        "capacity_assessments",
        metadata,
        Column("assessment_id", String(128), primary_key=True),
        Column("strategy_id", String(128), nullable=False),
        Column("as_of", String(10), nullable=False),
        Column("inputs_identity", String(128)),
        Column("objective_identity", String(128), nullable=False),
        Column("cost_model_identity", String(128), nullable=False),
        Column("verdict", String(32), nullable=False),
        Column("breakeven_usd", Text),
        Column("first_failing_rung_usd", Text),
        Column("binding_symbol", String(32)),
        Column("binding_constraint", Text),
        Column("modelled_inputs", Text, nullable=False),
        Column("blocking", Text, nullable=False),
        Column("rung_count", Integer, nullable=False),
    )
    Index(
        "ix_capacity_assessments_strategy",
        assessments.c.strategy_id,
        assessments.c.as_of,
    )
    return metadata


def upgrade() -> None:
    """Create the capacity table and bump the version marker."""
    connection = op.get_bind()
    metadata = v15_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V15_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=15))


def downgrade() -> None:
    """Drop it. Every recorded capacity verdict goes with it, so this is not routine."""
    connection = op.get_bind()
    metadata = v15_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V15_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=14))
