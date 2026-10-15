"""Add immutable shadow learning; historical core payloads remain untouched."""

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, text

from quantbot.storage.core_learning_schema import learning_tables

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData(
        naming_convention={"pk": "pk_%(table_name)s", "uq": "uq_%(table_name)s_%(column_0_name)s"}
    )
    Table("search_runs", metadata, Column("search_id", String(128), primary_key=True))
    for table in learning_tables(metadata):
        table.create(op.get_bind(), checkfirst=True)
    op.execute(text("UPDATE schema_version SET version=33 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v33 is evidence-preserving; use compatible code or repair forward")
