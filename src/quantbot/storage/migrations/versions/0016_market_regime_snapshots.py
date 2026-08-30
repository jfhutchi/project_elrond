"""Persist what kind of market a decision happened in, and bind it to observations.

Revision ID: 0016
Revises: 0015

#59's regime snapshot is context for conditional analysis: a refusal or an outcome can later be
grouped by the conditions it occurred under. It is never authority -- `quantbot.risk.regime` is
structurally unable to import a broker, a coordinator, an order or the veto engine, so no calm
market can relax a limit.

**Bound through a relation, not by copying the id onto the observation.** This is the rule #94
established for dependence and it applies identically here: a snapshot is one versioned
definition's reading of a session, and revising that definition must mint a new snapshot rather
than redefine the observation it described. `anomaly_regime_contexts` therefore keys on
`(anomaly_event_id, market_regime_snapshot_id)`, so a second regime method produces a second
relation row and the raw observation is untouched.

`rejected_candidates.market_regime_snapshot_id` already exists from 0012 and needs no change:
that binding is written once at decision time, when the regime is already known, rather than
attached afterwards.

`unavailable` is stored as text rather than left implicit. An axis reporting `UNAVAILABLE`
without a reason is indistinguishable from an ordinary reading once it is in a dataset, and a
study conditioning on regime would then find effects in its own gaps.

Purely additive. No existing row is read or rewritten.

The tables are declared literally rather than imported from `schema.py`, for the reason recorded
in `REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
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

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V16_TABLES = ("market_regime_snapshots", "anomaly_regime_contexts")


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v16_tables() -> MetaData:
    """The regime tables exactly as this revision creates them.

    `market_anomaly_events` is declared here as a primary-key-only stub. It is created by 0014
    and is **not** in `V16_TABLES`, so nothing here creates or alters it -- but the foreign key
    on `anomaly_regime_contexts` cannot resolve against a `MetaData` that has never heard of it.
    Declaring the real table again would duplicate a definition this revision does not own; a
    stub resolves the reference and owns nothing.
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "market_anomaly_events",
        metadata,
        Column("event_id", String(128), primary_key=True),
    )
    snapshots = Table(
        "market_regime_snapshots",
        metadata,
        Column("snapshot_id", String(128), primary_key=True),
        Column("as_of_session", String(10), nullable=False),
        Column("benchmark_symbol", String(32), nullable=False),
        Column("source", String(64), nullable=False),
        Column("method_version", Integer, nullable=False),
        Column("trend", String(16), nullable=False),
        Column("volatility", String(16), nullable=False),
        Column("breadth", String(16), nullable=False),
        Column("realised_volatility", Text),
        Column("participation", Text),
        Column("unavailable", Text, nullable=False),
        Column("source_snapshot_ids", Text, nullable=False),
    )
    Index(
        "ix_market_regime_snapshots_session",
        snapshots.c.as_of_session,
        snapshots.c.method_version,
    )
    Table(
        "anomaly_regime_contexts",
        metadata,
        Column("anomaly_event_id", String(128), primary_key=True),
        Column("market_regime_snapshot_id", String(128), primary_key=True),
        ForeignKeyConstraint(
            ["anomaly_event_id"],
            ["market_anomaly_events.event_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["market_regime_snapshot_id"],
            ["market_regime_snapshots.snapshot_id"],
            ondelete="CASCADE",
        ),
    )
    return metadata


def upgrade() -> None:
    """Create the regime tables and bump the version marker."""
    connection = op.get_bind()
    metadata = v16_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V16_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=16))


def downgrade() -> None:
    """Drop them. Every recorded market condition goes too, so this is not routine."""
    connection = op.get_bind()
    metadata = v16_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V16_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=15))
