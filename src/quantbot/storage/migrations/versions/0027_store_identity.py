"""Add the durable store-lineage identity.

Revision ID: 0027
Revises: 0026

The identity lives in the existing singleton ``schema_version`` row. It is generated only by
explicit creation or migration, not by an ordinary database open.

Downgrade is intentionally refused. Revision 0027 replaces the legacy path hashes in every
runtime observation with the new lineage UUID, so the original values no longer exist. Renaming
the UUID column back would make revision-0026 code interpret lineage UUIDs as path hashes and draw
false operational conclusions.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

from alembic import op
from sqlalchemy import Column, Integer, MetaData, String, Table, text, update

revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _schema_version() -> Table:
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
        Column("store_identity", String(36), nullable=True),
    )


def upgrade() -> None:
    op.add_column("schema_version", Column("store_identity", String(36), nullable=True))
    connection = op.get_bind()
    schema = _schema_version()
    identity = str(uuid4())
    connection.execute(update(schema).where(schema.c.id == 1).values(store_identity=identity))
    with op.batch_alter_table("schema_version") as batch:
        batch.alter_column(
            "store_identity",
            existing_type=String(36),
            existing_nullable=True,
            nullable=False,
        )
    connection.execute(
        text("UPDATE runtime_deployments SET database_fingerprint = :store_identity"),
        {"store_identity": identity},
    )
    with op.batch_alter_table("runtime_deployments") as batch:
        batch.alter_column(
            "database_fingerprint",
            existing_type=String(64),
            existing_nullable=False,
            new_column_name="store_identity",
            type_=String(36),
        )
    connection.execute(update(schema).where(schema.c.id == 1).values(version=27))


def downgrade() -> None:
    raise RuntimeError(
        "downgrade from 0027 to 0026 is unsafe: 0027 replaced the legacy SHA-256 path "
        "fingerprints with a persisted store-lineage UUID, and the original fingerprints "
        "cannot be reconstructed. Restore a pre-0027 backup instead"
    )
