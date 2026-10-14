"""Retain scoped health and relative-clock attempts without rewriting history."""

from alembic import op
from sqlalchemy import MetaData, text

from quantbot.storage.core_health_schema import health_tables

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    naming = {"pk": "pk_%(table_name)s"}
    for table in health_tables(MetaData(naming_convention=naming)):
        table.create(op.get_bind(), checkfirst=True)
    op.execute(text("UPDATE schema_version SET version=32 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v32 is evidence-preserving; use compatible code or repair forward")
