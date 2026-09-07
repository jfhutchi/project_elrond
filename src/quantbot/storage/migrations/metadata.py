"""Dialect-rendered names for Alembic comparison, without mutating canonical metadata."""

from sqlalchemy import MetaData
from sqlalchemy.engine import Dialect
from sqlalchemy.sql.elements import conv

from quantbot.storage.schema import metadata


def metadata_for_dialect(dialect: Dialect) -> MetaData:
    """Compare against the names SQLAlchemy actually emits on the selected backend.

    PostgreSQL shortens generated names to its identifier limit. Alembic's named CHECK
    comparison otherwise treats those as dropped constraints and the long names as additions.
    Keep SQLAlchemy's own deterministic truncation; never rename an existing SQLite constraint.
    """
    rendered = MetaData(naming_convention=metadata.naming_convention)
    for table in metadata.sorted_tables:
        table.to_metadata(rendered)
    for table in rendered.tables.values():
        for constraint in table.constraints:
            if isinstance(constraint.name, str):
                constraint.name = conv(
                    dialect.identifier_preparer.truncate_and_render_constraint_name(
                        constraint.name, _alembic_quote=False
                    )
                )
    return rendered
