"""Create the initial durable audit schema.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from alembic import op

from quantbot.storage.schema import (
    SCHEMA_VERSION,
    kill_switch_state,
    metadata,
    schema_version,
)

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create all V1 tables, constraints, indices, and singleton state."""
    connection = op.get_bind()
    metadata.create_all(bind=connection)
    connection.execute(schema_version.insert().values(id=1, version=SCHEMA_VERSION))
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
    metadata.drop_all(bind=op.get_bind())
