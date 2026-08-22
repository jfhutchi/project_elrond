from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.forecasting.cli import ShadowCLIContext, build_parser, main
from quantbot.forecasting.database import ForecastDatabase
from quantbot.forecasting.ledger import ForecastLedger
from quantbot.forecasting.models import (
    ForecastCandle,
    ForecastRequest,
    ForecastStatus,
    WorkerForecast,
)
from quantbot.storage import Database, StorageRepository

AS_OF = datetime(2026, 8, 14, 20, tzinfo=UTC)
SOURCE_PROVIDER = "alpaca:iex:all:1day"
SOURCE_METADATA = {
    "adjustment": "all",
    "feed": "iex",
    "provider": "alpaca",
    "timeframe": "1Day",
}


def source_contract_arguments(cache: str | Path = "external-cache") -> list[str]:
    return [
        "--market-data-provider",
        "alpaca",
        "--market-data-feed",
        "iex",
        "--market-data-adjustment",
        "all",
        "--timeframe",
        "1Day",
        "--source-vintage-id",
        "test-vintage-v1",
        "--source-available-at",
        AS_OF.isoformat(),
        "--python-executable",
        "worker-python",
        "--cache-directory",
        str(cache),
    ]


class FakeProvider:
    def forecast(self, request: ForecastRequest) -> WorkerForecast:
        request_id = request.request_id
        config = request.config
        horizon = request.horizon
        candle = ForecastCandle(open="100", high="102", low="99", close="101", volume="1")
        return WorkerForecast(
            request_id=request_id,
            sample_paths=tuple(
                tuple(candle for _ in range(horizon)) for _ in range(config.sample_count)
            ),
            model_revision=config.model_revision,
            tokenizer_revision=config.tokenizer_revision,
            kronos_code_revision=config.kronos_code_revision,
            generated_at=AS_OF,
            runtime_environment_hash="test-env",
            device="cpu",
            model_initialization_seconds="0",
            inference_seconds="0",
        )


class CountingProvider(FakeProvider):
    def __init__(self) -> None:
        self.calls = 0

    def forecast(self, request: ForecastRequest) -> WorkerForecast:
        self.calls += 1
        return super().forecast(request)


def seed_bars(path: Path) -> None:
    database = Database(path)
    try:
        bars = [
            Bar(
                symbol="AAPL",
                timestamp=AS_OF + timedelta(days=offset),
                open=str(99 + offset),
                high=str(101 + offset),
                low=str(98 + offset),
                close=str(100 + offset),
                volume="1000",
                adjustment="1",
            )
            for offset in range(-39, 5)
        ]
        with database.transaction() as session:
            StorageRepository(session).save_bars(
                bars,
                provider=SOURCE_PROVIDER,
                adjustment_metadata=SOURCE_METADATA,
            )
    finally:
        database.close()


def test_parser_registers_independent_shadow_commands_and_safe_defaults() -> None:
    args = build_parser().parse_args(
        [
            "shadow-run",
            "--source-database",
            "source.db",
            "--forecast-database",
            "shadow.db",
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--kronos-repository",
            "Kronos",
            *source_contract_arguments(),
        ]
    )

    assert args.lookback == 40
    assert args.horizon == 12
    assert args.temperature == "0.6"
    assert args.top_p == "0.9"
    assert args.sample_count == 10
    assert args.command == "shadow-run"


def test_cli_rejects_forecast_database_that_is_the_active_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active = tmp_path / "active.db"
    output = io.StringIO()
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(active))

    exit_code = main(
        [
            "shadow-run",
            "--source-database",
            str(active),
            "--forecast-database",
            str(active),
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--kronos-repository",
            str(tmp_path),
            *source_contract_arguments(tmp_path / "cache"),
        ],
        context=ShadowCLIContext(provider_factory=lambda _args: FakeProvider()),
        output=output,
    )

    assert exit_code != 0
    assert json.loads(output.getvalue())["error"] == "FORECAST_DATABASE_NOT_ISOLATED"


def test_cli_rejects_default_active_database_even_without_environment_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("QUANTBOT_DB_PATH", raising=False)
    output = io.StringIO()
    exit_code = main(
        [
            "shadow-run",
            "--source-database",
            str(tmp_path / "other.db"),
            "--forecast-database",
            str(Path("quantbot.db").resolve()),
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--kronos-repository",
            str(tmp_path),
            *source_contract_arguments(tmp_path / "cache"),
        ],
        context=ShadowCLIContext(provider_factory=lambda _args: FakeProvider()),
        output=output,
    )

    assert exit_code == 2
    assert json.loads(output.getvalue())["error"] == "FORECAST_DATABASE_NOT_ISOLATED"


def test_shadow_cli_runs_with_injected_provider_and_persists_only_shadow_artifact(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.db"
    forecast = tmp_path / "forecast.db"
    seed_bars(source)
    source_before = hashlib.sha256(source.read_bytes()).hexdigest()
    output = io.StringIO()

    exit_code = main(
        [
            "shadow-run",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--lookback",
            "40",
            "--horizon",
            "2",
            "--sample-count",
            "2",
            "--kronos-repository",
            str(tmp_path),
            *source_contract_arguments(tmp_path / "cache"),
        ],
        context=ShadowCLIContext(provider_factory=lambda _args: FakeProvider()),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert exit_code == 0
    assert payload["ok"] is True
    assert payload["results"][0]["status"] == "SUCCESS"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_before
    assert source.read_bytes() != forecast.read_bytes()


def test_shadow_cli_reuses_existing_identity_without_rerunning_model(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    forecast = tmp_path / "forecast.db"
    seed_bars(source)
    provider = CountingProvider()
    arguments = [
        "shadow-run",
        "--source-database",
        str(source),
        "--forecast-database",
        str(forecast),
        "--symbol",
        "AAPL",
        "--as-of",
        AS_OF.isoformat(),
        "--lookback",
        "40",
        "--horizon",
        "2",
        "--sample-count",
        "2",
        "--kronos-repository",
        str(tmp_path),
        *source_contract_arguments(tmp_path / "cache"),
    ]

    first = main(
        arguments,
        context=ShadowCLIContext(provider_factory=lambda _args: provider),
        output=io.StringIO(),
    )
    second = main(
        arguments,
        context=ShadowCLIContext(provider_factory=lambda _args: provider),
        output=io.StringIO(),
    )

    assert first == second == 0
    assert provider.calls == 1


def test_provider_setup_failure_is_a_durable_safe_shadow_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    forecast = tmp_path / "forecast.db"
    seed_bars(source)
    output = io.StringIO()

    def unavailable(_args: object) -> FakeProvider:
        raise ValueError("local path that must not reach output")

    exit_code = main(
        [
            "shadow-run",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--lookback",
            "40",
            "--horizon",
            "2",
            "--sample-count",
            "2",
            "--kronos-repository",
            str(tmp_path),
            *source_contract_arguments(tmp_path / "cache"),
        ],
        context=ShadowCLIContext(provider_factory=unavailable),
        output=output,
    )

    payload = json.loads(output.getvalue())
    assert exit_code == 1
    assert payload["results"][0]["status"] == "FAILED"
    assert payload["results"][0]["error_code"] == "PROVIDER_SETUP_FAILED"
    assert "local path" not in output.getvalue()

    database = ForecastDatabase(forecast)
    try:
        with database.transaction() as session:
            records = ForecastLedger(session).list_forecasts(status=ForecastStatus.FAILED)
        assert len(records) == 1
        assert records[0].error_code == "PROVIDER_SETUP_FAILED"
    finally:
        database.close()


def test_score_command_appends_only_mature_exact_target_outcome(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    forecast = tmp_path / "forecast.db"
    seed_bars(source)
    run_output = io.StringIO()
    run_exit = main(
        [
            "shadow-run",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--symbol",
            "AAPL",
            "--as-of",
            AS_OF.isoformat(),
            "--lookback",
            "40",
            "--horizon",
            "2",
            "--sample-count",
            "2",
            "--kronos-repository",
            str(tmp_path),
            *source_contract_arguments(tmp_path / "cache"),
        ],
        context=ShadowCLIContext(provider_factory=lambda _args: FakeProvider()),
        output=run_output,
    )
    assert run_exit == 0

    available = AS_OF + timedelta(days=4, minutes=10)
    future_output = io.StringIO()
    future_exit = main(
        [
            "score",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--as-of",
            available.isoformat(),
            "--symbol",
            "AAPL",
            "--outcome-vintage-id",
            "outcome-vintage-v1",
            "--outcome-available-at",
            available.isoformat(),
        ],
        context=ShadowCLIContext(
            provider_factory=lambda _args: FakeProvider(),
            clock=lambda: AS_OF,
        ),
        output=future_output,
    )
    assert future_exit == 2
    assert json.loads(future_output.getvalue())["error"] == "SHADOW_COMMAND_FAILED"

    missing_output = io.StringIO()
    missing_exit = main(
        [
            "score",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--as-of",
            available.isoformat(),
            "--symbol",
            "MISSING",
            "--outcome-vintage-id",
            "outcome-vintage-v1",
            "--outcome-available-at",
            available.isoformat(),
        ],
        output=missing_output,
    )
    assert missing_exit == 2
    assert json.loads(missing_output.getvalue())["error"] == "SHADOW_COMMAND_FAILED"

    score_output = io.StringIO()
    score_exit = main(
        [
            "score",
            "--source-database",
            str(source),
            "--forecast-database",
            str(forecast),
            "--as-of",
            available.isoformat(),
            "--symbol",
            " AAPL ",
            "--outcome-vintage-id",
            "outcome-vintage-v1",
            "--outcome-available-at",
            available.isoformat(),
        ],
        output=score_output,
    )

    payload = json.loads(score_output.getvalue())
    assert score_exit == 0
    assert len(payload["evaluations"]) == 1
    assert payload["evaluations"][0]["realized_terminal_return"] == "0.04"
