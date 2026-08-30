"""Persist what woke the research system up, before anything enriches it.

Revision ID: 0014
Revises: 0013

#56's engine spends expensive news/model research only on symbols showing genuinely unusual
activity. That premise is only testable if the events are durable *before* enrichment runs --
otherwise a downstream failure erases what was actually observed, and the routing-value
experiment has nothing to compare a naive scan against.

**Two tables, and the split is load-bearing.** `market_anomaly_events` is the raw observation:
a symbol, a session, an expected against an observed, under a named detector and feed. It is
fixed the moment it is measured. `anomaly_dependence_contexts` is what one frozen correlation
matrix said about that observation -- which other symbols were moving with it. Re-run the matrix
over a different window and the grouping legitimately changes while the observation does not.

Folding the second into the first broke content-addressing before it reached storage: the
grouped copy carried a different `group_key` under the same `event_id`, so keying this table on
it would have deduplicated two materially different payloads into whichever arrived first. The
unique constraint on `(anomaly_event_id, dependence_snapshot_identity)` is the durable form of
"one matrix has one opinion per observation".

`event_id` excludes the wall clock, so replaying a session is idempotent rather than a second
sample -- the inflation #56 warns about, and the seam #57 deduplicates on.

No enrichment, no catalyst, no score. An anomaly is a routing signal for research and holds no
broker authority, so nothing here references an order, an intent or a side.

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
    UniqueConstraint,
    update,
)

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V14_TABLES = ("market_anomaly_events", "anomaly_dependence_contexts")


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v14_tables() -> MetaData:
    """The discovery tables exactly as this revision creates them."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    events = Table(
        "market_anomaly_events",
        metadata,
        Column("event_id", String(128), primary_key=True),
        Column("symbol", String(32), nullable=False),
        Column("session_date", String(10), nullable=False),
        Column("segment", String(24), nullable=False),
        Column("observed_at", String(40), nullable=False),
        Column("detector_identity", String(128), nullable=False),
        Column("feed", String(24), nullable=False),
        Column("volume_coverage", String(32), nullable=False),
        Column("cadence", String(24), nullable=False),
        Column("baseline_first_session", String(10), nullable=False),
        Column("baseline_last_session", String(10), nullable=False),
        Column("baseline_session_count", Integer, nullable=False),
        Column("observed_volume", Text, nullable=False),
        Column("expected_volume", Text),
        Column("volume_robust_z", Text),
        Column("session_return", Text, nullable=False),
        Column("trailing_sigma", Text),
        Column("triggers", String(128), nullable=False),
        Column("family_quality", String(255), nullable=False),
    )
    Index("ix_market_anomaly_events_session", events.c.session_date, events.c.symbol)
    Index("ix_market_anomaly_events_detector", events.c.detector_identity)

    contexts = Table(
        "anomaly_dependence_contexts",
        metadata,
        Column("context_id", String(128), primary_key=True),
        Column("anomaly_event_id", String(128), nullable=False),
        Column("dependence_snapshot_identity", String(128), nullable=False),
        Column("group_key", String(128)),
        Column("method_version", Integer, nullable=False),
        ForeignKeyConstraint(
            ["anomaly_event_id"],
            ["market_anomaly_events.event_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "anomaly_event_id",
            "dependence_snapshot_identity",
            name="one_context_per_event_and_snapshot",
        ),
    )
    Index(
        "ix_anomaly_dependence_contexts_group",
        contexts.c.dependence_snapshot_identity,
        contexts.c.group_key,
    )
    return metadata


def upgrade() -> None:
    """Create the discovery tables and bump the version marker."""
    connection = op.get_bind()
    metadata = v14_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V14_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=14))


def downgrade() -> None:
    """Drop them. Every observation of what the market did goes too, so this is not routine."""
    connection = op.get_bind()
    metadata = v14_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V14_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=13))
