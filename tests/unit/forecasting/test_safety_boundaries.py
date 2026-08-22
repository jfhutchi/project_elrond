from __future__ import annotations

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError

from quantbot.domain import (
    Bar,
    IntentState,
    OrderIntent,
    OrderSide,
    OrderType,
    StrategyIdentity,
    TimeInForce,
)
from quantbot.forecasting.database import ForecastDatabase
from quantbot.forecasting.ledger import ForecastLedger
from quantbot.forecasting.models import (
    ForecastCandle,
    ForecastRecord,
    KronosConfig,
    WorkerForecast,
)
from quantbot.forecasting.schema import model_forecasts
from quantbot.forecasting.snapshots import build_forecast_request
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import order_intents, qualification_days, signals


def successful_forecast_fixture() -> ForecastRecord:
    as_of = datetime(2026, 8, 14, 20, tzinfo=UTC)
    request = build_forecast_request(
        bars=[
            Bar(
                symbol="AAPL",
                timestamp=as_of,
                open="99",
                high="101",
                low="98",
                close="100",
                volume="1000",
                adjustment="1",
            )
        ],
        symbol="AAPL",
        as_of=as_of,
        provider="alpaca:iex:all:1day",
        vintage_id="safety-source-v1",
        available_at=as_of,
        adjustment_metadata={
            "adjustment": "all",
            "feed": "iex",
            "provider": "alpaca",
            "timeframe": "1Day",
        },
        lookback=1,
        horizon=1,
        config=KronosConfig(sample_count=1),
    )
    worker = WorkerForecast(
        request_id=request.request_id,
        sample_paths=(
            (ForecastCandle(open="100", high="102", low="99", close="101", volume="1"),),
        ),
        model_revision=request.config.model_revision,
        tokenizer_revision=request.config.tokenizer_revision,
        kronos_code_revision=request.config.kronos_code_revision,
        generated_at=as_of + timedelta(minutes=1),
        runtime_environment_hash="test-env",
        device="cpu",
        model_initialization_seconds="0",
        inference_seconds="0",
    )
    return ForecastRecord.succeeded(request, worker)


def test_forecasting_package_has_no_import_path_to_trading_authority() -> None:
    package = Path(__file__).parents[3] / "src" / "quantbot" / "forecasting"
    forbidden_modules = (
        "quantbot.brokers",
        "quantbot.config",
        "quantbot.execution",
        "quantbot.operations",
        "quantbot.research.promotion",
        "quantbot.risk",
        "quantbot.runtime",
        "quantbot.strategy",
    )
    forbidden_calls = {
        "record_qualification_day",
        "record_signal",
        "submit_order",
        "create_order_intent",
        "clear_kill_switch",
        "count_forward_days",
        "promote",
        "cancel_order",
    }
    forbidden_names = {
        "ForwardObservation",
        "OrderIntent",
        "QualificationTracker",
        "SubmissionRequest",
    }

    for source_file in package.rglob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
        imported: list[str] = []
        calls: list[str] = []
        names: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.append(node.module)
            elif isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    calls.append(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    calls.append(node.func.attr)
            elif isinstance(node, ast.Name):
                names.append(node.id)

        assert not any(name.startswith(forbidden_modules) for name in imported), source_file
        assert forbidden_calls.isdisjoint(calls), source_file
        assert forbidden_names.isdisjoint(names), source_file


def test_trading_authority_has_no_reverse_dependency_on_forecasting() -> None:
    root = Path(__file__).parents[3] / "src" / "quantbot"
    targets = [
        root / "runtime.py",
        root / "brokers",
        root / "execution",
        root / "operations",
        root / "risk",
        root / "strategy",
    ]
    for target in targets:
        files = [target] if target.is_file() else list(target.rglob("*.py"))
        for source_file in files:
            tree = ast.parse(source_file.read_text(encoding="utf-8"), filename=str(source_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    assert all(
                        not alias.name.startswith("quantbot.forecasting") for alias in node.names
                    ), source_file
                elif isinstance(node, ast.ImportFrom) and node.module is not None:
                    assert not node.module.startswith("quantbot.forecasting"), source_file


def test_shadow_forecast_schema_cannot_masquerade_as_strategy_or_paper_evidence() -> None:
    columns = set(model_forecasts.c.keys())

    assert "evidence_role" in columns
    assert {"strategy_id", "run_id", "signal_id", "qualification_day"}.isdisjoint(columns)


def test_database_rejects_paper_evidence_role_for_forecast(tmp_path: Path) -> None:
    database = ForecastDatabase(tmp_path / "shadow.db")
    try:
        record = successful_forecast_fixture()
        with database.transaction() as session:
            ForecastLedger(session).save_forecast(record)
        with database.engine.begin() as connection, pytest.raises(IntegrityError):
            connection.execute(
                update(model_forecasts)
                .where(model_forecasts.c.forecast_id == record.forecast_id)
                .values(evidence_role="PAPER_OBSERVATION")
            )
    finally:
        database.close()


def test_forecast_and_active_storage_planes_have_disjoint_authority(tmp_path: Path) -> None:
    active = Database(tmp_path / "active.db")
    forecast = ForecastDatabase(tmp_path / "forecast.db")
    try:
        active_tables = set(inspect(active.engine).get_table_names())
        forecast_tables = set(inspect(forecast.engine).get_table_names())
        assert "model_forecasts" not in active_tables
        assert "model_forecasts" in forecast_tables
        assert not {"signals", "order_intents", "qualification_days"} & forecast_tables
    finally:
        forecast.close()
        active.close()


def test_forecast_write_leaves_seeded_active_evidence_unchanged(tmp_path: Path) -> None:
    active = Database(tmp_path / "active.db")
    forecast = ForecastDatabase(tmp_path / "forecast.db")
    as_of = datetime(2026, 8, 14, 20, tzinfo=UTC)
    identity = StrategyIdentity(
        strategy_id="protected-paper-strategy",
        version="1.0.0",
        git_commit="abc1234",
        configuration_hash="protected-hash",
        deployment_timestamp=as_of,
    )
    intent = OrderIntent(
        intent_id="protected-intent",
        client_order_id="protected-client-id",
        strategy_id=identity.strategy_id,
        symbol="AAPL",
        signal_date=date(2026, 8, 14),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.DAY,
        quantity="1",
        limit_price="100",
        created_at=as_of,
        state=IntentState.RISK_APPROVED,
    )
    try:
        with active.transaction() as session:
            repository = StorageRepository(session)
            repository.create_run("protected-run", identity, started_at=as_of)
            repository.record_signal(
                "protected-signal",
                run_id="protected-run",
                strategy_id=identity.strategy_id,
                symbol="AAPL",
                occurred_at=as_of,
                payload={"score": "0.5"},
            )
            repository.create_order_intent(intent)
            repository.record_qualification_day(
                identity.strategy_id,
                date(2026, 8, 14),
                qualified=True,
                detail={"proof": "protected"},
            )
        with active.transaction() as session:
            before = {
                table.name: tuple(session.execute(select(table)).mappings())
                for table in (signals, order_intents, qualification_days)
            }

        with forecast.transaction() as session:
            ForecastLedger(session).save_forecast(successful_forecast_fixture())

        with active.transaction() as session:
            after = {
                table.name: tuple(session.execute(select(table)).mappings())
                for table in (signals, order_intents, qualification_days)
            }
        assert after == before
    finally:
        forecast.close()
        active.close()
