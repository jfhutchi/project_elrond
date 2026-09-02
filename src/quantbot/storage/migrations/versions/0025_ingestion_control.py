"""Add the ingestion control plane: sources, cursors, runs, events and heartbeats.

Revision ID: 0025
Revises: 0024

Purely additive.  No existing row is read or rewritten, so market observations, hypothesis
registrations and research records keep their content hashes and their historical meaning.

Every column type used here is deliberately boring -- ``String``, ``Integer``, ``Boolean``,
``Text`` -- and every constraint is a plain ``CHECK`` or foreign key.  #197 has to prove
SQLite/PostgreSQL parity for this schema, and a partial index or a dialect-specific type added
now would be a portability problem discovered later rather than avoided now.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    update,
)

revision: str = "0025"
down_revision: str | Sequence[str] | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _schema_version() -> Table:
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v25_tables() -> tuple[Table, ...]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    sources = Table(
        "ingestion_sources",
        metadata,
        Column("source_id", String(128), primary_key=True),
        Column("provider", String(128), nullable=False),
        Column("source_kind", String(64), nullable=False),
        Column("configuration_hash", String(64), nullable=False),
        Column("enabled", Boolean, nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("updated_at", String(40), nullable=False),
    )
    Index("ix_ingestion_sources_enabled", sources.c.enabled)

    cursors = Table(
        "ingestion_cursors",
        metadata,
        Column(
            "source_id",
            String(128),
            ForeignKey("ingestion_sources.source_id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        Column("stream_key", String(128), primary_key=True),
        Column("cursor_value", Text),
        Column("last_observation_at", String(40)),
        Column("last_success_at", String(40)),
        Column("updated_at", String(40), nullable=False),
        Column("version", Integer, nullable=False),
        CheckConstraint("version >= 1", name="cursor_versions_start_at_one"),
    )

    runs = Table(
        "ingestion_runs",
        metadata,
        Column("ingestion_run_id", String(128), primary_key=True),
        Column(
            "source_id",
            String(128),
            ForeignKey("ingestion_sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("mode", String(16), nullable=False),
        Column("started_at", String(40), nullable=False),
        Column("finished_at", String(40)),
        Column("status", String(16), nullable=False),
        Column("requested_start", Text),
        Column("requested_end", Text),
        Column("records_seen", Integer, nullable=False),
        Column("records_inserted", Integer, nullable=False),
        Column("records_duplicate", Integer, nullable=False),
        Column("records_rejected", Integer, nullable=False),
        Column("error_code", String(64)),
        Column("detail_json", Text, nullable=False),
        CheckConstraint("mode IN ('LIVE', 'BACKFILL')", name="run_mode_is_known"),
        CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'ABANDONED')",
            name="run_status_is_known",
        ),
        CheckConstraint(
            "(status = 'RUNNING') = (finished_at IS NULL)", name="finished_runs_have_an_end"
        ),
        CheckConstraint(
            "error_code IS NULL OR status IN ('FAILED', 'ABANDONED')",
            name="only_unsuccessful_runs_carry_an_error",
        ),
        CheckConstraint(
            "records_seen >= 0 AND records_inserted >= 0 AND records_duplicate >= 0"
            " AND records_rejected >= 0",
            name="record_counts_are_nonnegative",
        ),
        CheckConstraint(
            "records_inserted + records_duplicate + records_rejected <= records_seen",
            name="record_outcomes_fit_inside_what_was_seen",
        ),
    )
    Index("ix_ingestion_runs_source_started", runs.c.source_id, runs.c.started_at)
    Index("ix_ingestion_runs_status", runs.c.status, runs.c.started_at)

    events = Table(
        "ingestion_events",
        metadata,
        Column("event_id", String(128), primary_key=True),
        Column(
            "source_id",
            String(128),
            ForeignKey("ingestion_sources.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("stream_key", String(128)),
        Column("event_kind", String(32), nullable=False),
        Column("severity", String(16), nullable=False),
        Column("occurred_at", String(40), nullable=False),
        Column("resolved_at", String(40)),
        Column("detail_json", Text, nullable=False),
        CheckConstraint(
            "severity IN ('INFO', 'WARNING', 'CRITICAL')", name="event_severity_is_known"
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at >= occurred_at",
            name="events_resolve_after_they_open",
        ),
    )
    Index(
        "ix_ingestion_events_open",
        events.c.source_id,
        events.c.event_kind,
        events.c.resolved_at,
    )
    Index("ix_ingestion_events_occurred", events.c.occurred_at)

    heartbeats = Table(
        "component_heartbeats",
        metadata,
        Column("heartbeat_id", String(128), primary_key=True),
        Column("component_id", String(128), nullable=False),
        Column("instance_id", String(128), nullable=False),
        Column("observed_at", String(40), nullable=False),
        Column("status", String(16), nullable=False),
        Column("last_success_at", String(40)),
        Column("detail_json", Text, nullable=False),
        CheckConstraint(
            "status IN ('STARTING', 'HEALTHY', 'DEGRADED', 'STOPPING')",
            name="heartbeat_status_is_known",
        ),
    )
    Index(
        "ix_component_heartbeats_component",
        heartbeats.c.component_id,
        heartbeats.c.observed_at,
    )
    return sources, cursors, runs, events, heartbeats


def upgrade() -> None:
    connection = op.get_bind()
    for table in v25_tables():
        # Matches revisions 0021-0024: creation is idempotent, while the final metadata
        # comparison still fails closed if an existing table has the wrong shape.
        table.create(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=25))


def downgrade() -> None:
    connection = op.get_bind()
    # Reversed, so the children go before `ingestion_sources` they reference.
    for table in reversed(v25_tables()):
        table.drop(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=24))
