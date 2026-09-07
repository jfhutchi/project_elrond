"""Alembic runtime configuration for QuantBot storage migrations."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection, engine_from_config, pool
from sqlalchemy.engine import make_url

from quantbot.storage.migrations.metadata import metadata_for_dialect
from quantbot.storage.schema import metadata

config = context.config
target_metadata = metadata

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def configured_url() -> str:
    """A migration requires explicit target selection; runtime environment is not consulted."""
    url = context.get_x_argument(as_dictionary=True).get("database_url") or config.get_main_option(
        "sqlalchemy.url"
    )
    if not url or not url.strip():
        raise RuntimeError(
            "Alembic requires an explicit connection or database URL; "
            "supply -x database_url=<URL> or configure sqlalchemy.url"
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations using only the configured database URL."""
    url = configured_url()
    context.configure(
        url=url,
        target_metadata=metadata_for_dialect(make_url(url).get_dialect()()),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on a supplied connection or a configured engine."""
    supplied_connection = config.attributes.get("connection")
    if supplied_connection is not None:
        run_with_connection(supplied_connection)
        return

    options = config.get_section(config.config_ini_section, {})
    options["sqlalchemy.url"] = configured_url()
    connectable = engine_from_config(
        options,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        with connectable.connect() as connection:
            run_with_connection(connection)
    finally:
        connectable.dispose()


def run_with_connection(connection: Connection) -> None:
    """Apply revisions within the caller's connection boundary."""
    context.configure(
        connection=connection, target_metadata=metadata_for_dialect(connection.dialect)
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
