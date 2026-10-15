"""Add PAPER core fixture contracts and separate raw protection snapshots. Revision 0030."""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Integer, MetaData, String, Table, update

from quantbot.storage.core_schema import core_tables

revision: str = "0030"
down_revision: str | Sequence[str] | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    Table("order_intents", metadata, Column("intent_id", String(128), primary_key=True))
    Table("account_snapshots", metadata, Column("snapshot_id", String(128), primary_key=True))
    for table in core_tables(metadata):
        table.create(op.get_bind(), checkfirst=True)
    schema = Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )
    op.get_bind().execute(update(schema).where(schema.c.id == 1).values(version=30))


def downgrade() -> None:
    raise RuntimeError(
        "v30 is additive and evidence-preserving; use compatible code or repair forward"
    )
