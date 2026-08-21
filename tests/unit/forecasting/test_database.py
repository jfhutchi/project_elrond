from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from quantbot.forecasting.database import (
    ForecastDatabase,
    UnsupportedForecastSchemaVersionError,
)
from quantbot.forecasting.schema import FORECAST_SCHEMA_VERSION, metadata
from quantbot.storage import Database


def test_forecast_database_has_exact_minimal_schema_and_pragmas(tmp_path: Path) -> None:
    database = ForecastDatabase(tmp_path / "forecast.db")
    try:
        assert set(inspect(database.engine).get_table_names()) == {
            "alembic_version",
            "forecast_schema_version",
            "model_forecast_evaluations",
            "model_forecasts",
        }
        with database.engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
            assert (
                connection.execute(text("SELECT version FROM forecast_schema_version")).scalar_one()
                == FORECAST_SCHEMA_VERSION
            )
    finally:
        database.close()


def test_fresh_forecast_migration_replay_matches_head_metadata(tmp_path: Path) -> None:
    path = tmp_path / "migration.db"
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}")
    try:
        with engine.begin() as connection:
            config = ForecastDatabase._alembic_config(connection)
            command.upgrade(config, "head")
            context = MigrationContext.configure(
                connection,
                opts={"compare_type": True, "compare_server_default": True},
            )
            assert compare_metadata(context, metadata) == []

            command.downgrade(config, "base")
            remaining = set(inspect(connection).get_table_names())
            assert "model_forecasts" not in remaining
            assert "model_forecast_evaluations" not in remaining
            assert "forecast_schema_version" not in remaining
    finally:
        engine.dispose()


def test_forecast_database_rejects_active_or_tampered_schema(tmp_path: Path) -> None:
    active_path = tmp_path / "active.db"
    active = Database(active_path)
    active.close()
    with pytest.raises(UnsupportedForecastSchemaVersionError, match="incompatible"):
        ForecastDatabase(active_path)

    forecast_path = tmp_path / "forecast.db"
    forecast = ForecastDatabase(forecast_path)
    try:
        with forecast.engine.begin() as connection:
            connection.execute(text("UPDATE forecast_schema_version SET version = 99"))
    finally:
        forecast.close()
    with pytest.raises(UnsupportedForecastSchemaVersionError, match="99"):
        ForecastDatabase(forecast_path)


def test_forecast_migrations_do_not_import_live_schema() -> None:
    versions = (
        Path(__file__).parents[3] / "src" / "quantbot" / "forecasting" / "migrations" / "versions"
    )
    for migration in versions.glob("*.py"):
        tree = ast.parse(migration.read_text(encoding="utf-8"), filename=str(migration))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
        assert "quantbot.forecasting.schema" not in imported, migration
