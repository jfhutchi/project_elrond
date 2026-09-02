"""Add the append-only deployed-state record.

Revision ID: 0026
Revises: 0025

Purely additive.  Nothing existing is read or rewritten.

The #190 inventory established which commit was checked out, whether anything was running and
which database was authoritative by reading the filesystem, the process table, the registry and
the git reflog.  Those answers were only unambiguous because nothing was running; with a live
process on an older checkout the same archaeology would not have settled it.  This table lets
the runtime state the facts itself.
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

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
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


def v26_tables() -> tuple[Table, ...]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    deployments = Table(
        "runtime_deployments",
        metadata,
        Column("observation_id", String(200), primary_key=True),
        Column("host_id", String(128), nullable=False),
        Column("instance_id", String(128), nullable=False),
        Column("role", String(32), nullable=False),
        Column("git_commit", String(64), nullable=False),
        Column("package_version", String(64), nullable=False),
        Column("python_version", String(32), nullable=False),
        Column("host_platform", String(160), nullable=False),
        Column("trading_mode", String(16), nullable=False),
        Column("database_fingerprint", String(64), nullable=False),
        Column("schema_version", Integer, nullable=False),
        Column("strategy_id", String(128)),
        Column("strategy_version", String(64)),
        Column("configuration_hash", String(128)),
        Column("process_started_at", String(40), nullable=False),
        Column("observed_at", String(40), nullable=False),
        Column("detail_json", Text, nullable=False, server_default=text("'{}'")),
        CheckConstraint(
            "role IN ('COORDINATOR', 'WORKER', 'CONSOLE', 'TOOL')", name="runtime_role_is_known"
        ),
        CheckConstraint("trading_mode IN ('PAPER', 'LIVE')", name="runtime_mode_is_known"),
        CheckConstraint("schema_version >= 1", name="runtime_schema_version_is_positive"),
        CheckConstraint(
            "observed_at >= process_started_at",
            name="an_observation_follows_its_process_start",
        ),
    )
    Index(
        "ix_runtime_deployments_role_observed",
        deployments.c.role,
        deployments.c.observed_at,
    )
    Index("ix_runtime_deployments_commit", deployments.c.git_commit)
    return (deployments,)


def upgrade() -> None:
    connection = op.get_bind()
    for table in v26_tables():
        table.create(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=26))


def downgrade() -> None:
    connection = op.get_bind()
    for table in reversed(v26_tables()):
        table.drop(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=25))
