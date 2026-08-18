"""Add the frozen hypothesis registry.

Revision ID: 0002
Revises: 0001

Purely additive: two new tables and the schema-version marker. No existing row is read or
rewritten, so a paper database carries its ledger across unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import update

from quantbot.storage.schema import (
    hypotheses,
    hypothesis_data_windows,
    schema_version,
)

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NEW_TABLES = (hypotheses, hypothesis_data_windows)


def upgrade() -> None:
    """Create the registry tables and advance the schema marker to V2."""
    connection = op.get_bind()
    for table in NEW_TABLES:
        table.create(bind=connection)
    connection.execute(update(schema_version).where(schema_version.c.id == 1).values(version=2))


def downgrade() -> None:
    """Drop the registry tables and return the marker to V1."""
    connection = op.get_bind()
    for table in reversed(NEW_TABLES):
        table.drop(bind=connection)
    connection.execute(update(schema_version).where(schema_version.c.id == 1).values(version=1))
