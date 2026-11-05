"""Add explicit five-minute economic evidence without adopting old positions or resuming."""

from alembic import op
from sqlalchemy import MetaData, text

from quantbot.storage.core_throughput_schema import throughput_tables

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    for target in throughput_tables(metadata):
        target.create(op.get_bind(), checkfirst=True)
    op.execute(text("UPDATE schema_version SET version=37 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v37 is evidence-preserving; repair forward")
