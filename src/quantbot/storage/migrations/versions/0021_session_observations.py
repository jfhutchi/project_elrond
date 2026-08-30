"""A heartbeat that is durable even when nothing is wrong (#143).

Revision ID: 0021
Revises: 0020

The daemon wakes once per day, after the close. Between one close and the next, nothing looks at
reconciliation, the kill switch, or market-data freshness.

What made this urgent is in the operator's own store rather than in a design argument. A cycle
halted on 2026-08-19. The kill switch was cleared on 2026-08-20 with a recorded verification that
the condition had gone, and reconciliation then ran clean four times. No operational run has
happened since -- ten days, no blocking condition, nothing that noticed.

**The reason nothing noticed is the reason this table stores healthy rows too.** `incidents`
records problems. A system that has stopped produces no problems, so ten days of silence and ten
days of health are the same row count: zero. A heartbeat only durable when it fails is not a
heartbeat, and this table exists so that "nothing was wrong" and "nothing ran" stop looking
identical.

`outcome` separates `HALTED` from `DEGRADED` deliberately. The kill switch being engaged is a
deliberate stop, not a fault. Recording it as a fault is how somebody ends up clearing a kill
switch to make an alert go away, which is the one thing CLAUDE.md forbids outright.

The `reasons` constraint is the load-bearing one: a healthy observation names no reasons, and an
unhealthy one names at least one. Enforced here rather than only in the model that writes,
because the failure it prevents -- a row that says HEALTHY while carrying reasons, or DEGRADED
while carrying none -- would make every later reading of this table ambiguous.

`observation_id` is content-addressed from the instant, so a restart cannot mint a second id for
a pass that already happened.

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

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V21_TABLES = ("session_observations",)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v21_tables() -> MetaData:
    """The observation table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    observations = Table(
        "session_observations",
        metadata,
        # Content-addressed from the observed instant: a restart cannot mint a second id for a
        # pass that already happened.
        Column("observation_id", String(128), primary_key=True),
        Column("observed_at", String(40), nullable=False),
        Column("market_open", Integer, nullable=False),
        # HEALTHY, DEGRADED (a fault), HALTED (a deliberate stop -- not the same thing).
        Column("outcome", String(32), nullable=False),
        Column("reasons", Text, nullable=False),
        Column("market_data_age_seconds", String(40), nullable=True),
        Column("detail", Text, nullable=False),
        CheckConstraint(
            "outcome IN ('HEALTHY', 'DEGRADED', 'HALTED')",
            name="an_outcome_is_one_of_three",
        ),
        CheckConstraint("market_open IN (0, 1)", name="market_open_is_a_flag"),
        # The load-bearing one. A row claiming health while carrying reasons, or claiming a
        # fault while naming none, makes every later reading of this table ambiguous.
        CheckConstraint(
            "(outcome = 'HEALTHY') = (reasons = '')",
            name="health_and_reasons_agree",
        ),
    )
    Index("ix_session_observations_observed_at", observations.c.observed_at)
    Index("ix_session_observations_outcome", observations.c.outcome)
    return metadata


def upgrade() -> None:
    """Create the table and bump the version marker."""
    connection = op.get_bind()
    metadata = v21_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V21_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=21))


def downgrade() -> None:
    """Drop it. Every recorded heartbeat goes with it, healthy ones included."""
    connection = op.get_bind()
    metadata = v21_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V21_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=20))
