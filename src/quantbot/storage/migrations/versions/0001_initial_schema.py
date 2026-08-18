"""Create the initial durable audit schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op

from quantbot.storage.schema import (
    kill_switch_state,
    metadata,
    schema_version,
)

#: The tables this revision creates, named rather than taken from live metadata. A migration
#: that reaches for `metadata` as it stands today stops describing the schema it actually
#: produced as soon as a later revision adds a table.
V1_TABLES = (
    "schema_version",
    "strategy_deployments",
    "runs",
    "bars",
    "signals",
    "account_snapshots",
    "equity_snapshots",
    "positions",
    "order_intents",
    "broker_orders",
    "fills",
    "order_events",
    "reconciliation_runs",
    "reconciliation_diffs",
    "incidents",
    "qualification_days",
    "kill_switch_state",
)

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all V1 tables, constraints, indices, and singleton state."""
    connection = op.get_bind()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V1_TABLES])
    connection.execute(schema_version.insert().values(id=1, version=1))
    connection.execute(
        kill_switch_state.insert().values(
            id=1,
            engaged=True,
            reason="default fail-closed state",
            updated_at=datetime.now(UTC).isoformat().removesuffix("+00:00") + "Z",
        )
    )


def downgrade() -> None:
    """Drop the V1 domain schema while Alembic retains its version table."""
    metadata.drop_all(
        bind=op.get_bind(), tables=[metadata.tables[name] for name in reversed(V1_TABLES)]
    )
