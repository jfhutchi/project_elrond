"""Add atomic activation without modifying any earlier immutable payload."""

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, text

from quantbot.storage.core_activation_schema import activation_tables

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    for name in ("core_models", "core_learning_runs"):
        Table(name, metadata, Column("id", String(64), primary_key=True))
    for table in activation_tables(metadata):
        table.create(op.get_bind(), checkfirst=True)
    op.execute(text("UPDATE schema_version SET version=34 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v34 is evidence-preserving; select compatible code or repair forward")
