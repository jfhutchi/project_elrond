"""Add explicit runtime commissioning and independent role evidence."""

from alembic import op
from sqlalchemy import MetaData, text

from quantbot.storage.core_runtime_schema import runtime_tables

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData(naming_convention={"pk": "pk_%(table_name)s"})
    for table in runtime_tables(metadata):
        table.create(op.get_bind(), checkfirst=True)
    op.execute(text("UPDATE schema_version SET version=35 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v35 is evidence-preserving; keep recovery roles and repair forward")
