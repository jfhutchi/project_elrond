"""Durable independent position-protection evaluations (#157).

Revision ID: 0022
Revises: 0021

The tables are declared literally rather than importing the live schema. This preserves the
historical migration boundary and lets metadata comparison prove that the revision reproduces
the current schema exactly.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
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

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V22_TABLES = (
    "position_protection_cycles",
    "position_protection_evaluations",
)


def _schema_version() -> Table:
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v22_tables() -> MetaData:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Reference-only stub: the table already exists from 0001 and is deliberately excluded from
    # V22_TABLES, but SQLAlchemy must know its composite key to resolve the new foreign keys.
    Table(
        "strategy_deployments",
        metadata,
        Column("strategy_id", String(128), primary_key=True),
        Column("version", String(64), primary_key=True),
    )
    cycles = Table(
        "position_protection_cycles",
        metadata,
        Column("cycle_id", String(128), primary_key=True),
        Column("observed_at", String(40), nullable=False),
        Column("completed_at", String(40), nullable=False),
        Column("strategy_id", String(128), nullable=False),
        Column("strategy_version", String(64), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("account_id", String(128), nullable=True),
        Column("policy_version", String(64), nullable=False),
        Column("policy_hash", String(128), nullable=False),
        Column("outcome", String(32), nullable=False),
        Column("reason", String(128), nullable=False),
        Column("position_count", Integer, nullable=True),
        Column("evaluated_count", Integer, nullable=False),
        Column("unprotected_count", Integer, nullable=False),
        Column("exit_required_count", Integer, nullable=False),
        Column("detail", Text, nullable=False),
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_deployments.strategy_id", "strategy_deployments.version"],
        ),
        CheckConstraint(
            "outcome IN ('HEALTHY', 'ACTION_REQUIRED', 'DEGRADED', 'INCIDENT')",
            name="outcome_is_known",
        ),
        CheckConstraint(
            "(outcome = 'HEALTHY') = (reason = '')",
            name="healthy_cycle_has_no_reason",
        ),
        CheckConstraint(
            "position_count IS NULL OR position_count >= 0",
            name="position_count_is_nonnegative",
        ),
        CheckConstraint("evaluated_count >= 0", name="evaluated_count_is_nonnegative"),
        CheckConstraint("unprotected_count >= 0", name="unprotected_count_is_nonnegative"),
        CheckConstraint(
            "exit_required_count >= 0",
            name="exit_required_count_is_nonnegative",
        ),
        CheckConstraint(
            "position_count IS NULL OR evaluated_count <= position_count",
            name="evaluations_do_not_exceed_positions",
        ),
        CheckConstraint(
            "unprotected_count <= evaluated_count",
            name="unprotected_does_not_exceed_evaluated",
        ),
        CheckConstraint(
            "exit_required_count <= evaluated_count",
            name="exits_do_not_exceed_evaluated",
        ),
    )
    Index("ix_position_protection_cycles_observed_at", cycles.c.observed_at)
    Index("ix_position_protection_cycles_outcome", cycles.c.outcome)

    evaluations = Table(
        "position_protection_evaluations",
        metadata,
        Column("evaluation_id", String(128), primary_key=True),
        Column(
            "cycle_id",
            String(128),
            ForeignKey("position_protection_cycles.cycle_id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("account_id", String(128), nullable=False),
        Column("symbol", String(32), nullable=False),
        Column("quantity", Text, nullable=False),
        Column("average_entry_price", Text, nullable=False),
        Column("price", Text, nullable=True),
        Column("price_as_of", String(40), nullable=True),
        Column("price_source", String(64), nullable=False),
        Column("strategy_id", String(128), nullable=False),
        Column("strategy_version", String(64), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("policy_version", String(64), nullable=False),
        Column("policy_hash", String(128), nullable=False),
        Column("reconciled", Integer, nullable=False),
        Column("outcome", String(32), nullable=False),
        Column("rule", String(32), nullable=False),
        Column("reason", Text, nullable=False),
        Column("move_bps", Text, nullable=True),
        Column("protected", Integer, nullable=False),
        Column("evaluated_at", String(40), nullable=False),
        Column("detail", Text, nullable=False),
        ForeignKeyConstraint(
            ["strategy_id", "strategy_version"],
            ["strategy_deployments.strategy_id", "strategy_deployments.version"],
        ),
        UniqueConstraint("cycle_id", "symbol", name="one_evaluation_per_position"),
        CheckConstraint("reconciled IN (0, 1)", name="reconciled_is_a_flag"),
        CheckConstraint("protected IN (0, 1)", name="protected_is_a_flag"),
        CheckConstraint(
            "outcome IN ('NO_ACTION', 'EXIT_REQUIRED', 'BLOCKED_STATE', 'INCIDENT')",
            name="outcome_is_known",
        ),
        CheckConstraint(
            "rule IN ('HARD_STOP', 'ACCOUNT_DRAWDOWN', 'NONE')",
            name="rule_is_known",
        ),
    )
    Index("ix_position_protection_evaluations_cycle", evaluations.c.cycle_id)
    Index(
        "ix_position_protection_evaluations_symbol_time",
        evaluations.c.symbol,
        evaluations.c.evaluated_at,
    )
    return metadata


def upgrade() -> None:
    connection = op.get_bind()
    metadata = v22_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V22_TABLES])
    connection.execute(
        update(_schema_version()).where(_schema_version().c.id == 1).values(version=22)
    )


def downgrade() -> None:
    connection = op.get_bind()
    metadata = v22_tables()
    metadata.drop_all(
        bind=connection,
        tables=[metadata.tables[name] for name in reversed(V22_TABLES)],
    )
    connection.execute(
        update(_schema_version()).where(_schema_version().c.id == 1).values(version=21)
    )
