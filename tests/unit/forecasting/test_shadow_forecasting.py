from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from quantbot.domain import Bar
from quantbot.forecasting.database import ForecastDatabase
from quantbot.forecasting.evaluation import score_forecast
from quantbot.forecasting.ledger import ForecastLedger
from quantbot.forecasting.models import (
    BaselineScore,
    ForecastCandle,
    ForecastDirection,
    ForecastFeatures,
    ForecastRecord,
    ForecastRequest,
    ForecastStatus,
    KronosConfig,
    WorkerForecast,
    calculate_evaluation_id,
)
from quantbot.forecasting.provider import ForecastProviderError, KronosSignalProvider
from quantbot.forecasting.service import ShadowForecastService
from quantbot.forecasting.snapshots import build_forecast_request
from quantbot.storage import StateConflictError

AS_OF = datetime(2026, 8, 14, 20, tzinfo=UTC)
SOURCE_PROVIDER = "alpaca:iex:all:1day"
SOURCE_METADATA = {
    "adjustment": "all",
    "feed": "iex",
    "provider": "alpaca",
    "timeframe": "1Day",
}


def make_bars(*, count: int = 41, future: int = 0) -> list[Bar]:
    bars: list[Bar] = []
    for offset in range(-(count - 1), future + 1):
        price = Decimal(99 + offset)
        bars.append(
            Bar(
                symbol="AAPL",
                timestamp=AS_OF + timedelta(days=offset),
                open=price,
                high=price + 2,
                low=price - 2,
                close=price + 1,
                volume="1000",
                adjustment="1",
            )
        )
    return bars


def make_request(*, attempt_number: int = 0, vintage_id: str = "source-vintage-1"):
    return build_forecast_request(
        bars=make_bars(count=40),
        symbol="AAPL",
        as_of=AS_OF,
        provider=SOURCE_PROVIDER,
        vintage_id=vintage_id,
        available_at=AS_OF,
        adjustment_metadata=SOURCE_METADATA,
        lookback=40,
        horizon=2,
        config=KronosConfig(sample_count=2),
        attempt_number=attempt_number,
    )


def make_worker_forecast(request_id: str, *, terminal: str = "110") -> WorkerForecast:
    first = ForecastCandle(open="101", high="106", low="99", close="105", volume="1000")
    second = ForecastCandle(open="105", high="112", low="103", close=terminal, volume="1000")
    return WorkerForecast(
        request_id=request_id,
        sample_paths=((first, second), (first, second)),
        model_revision=KronosConfig().model_revision,
        tokenizer_revision=KronosConfig().tokenizer_revision,
        kronos_code_revision=KronosConfig().kronos_code_revision,
        generated_at=AS_OF + timedelta(minutes=2),
        runtime_environment_hash="env-sha256",
        device="cpu",
        model_initialization_seconds=Decimal("1.25"),
        inference_seconds=Decimal("0.75"),
        peak_process_memory_bytes=1234,
    )


def make_success_record() -> ForecastRecord:
    request = make_request()
    return ForecastRecord.succeeded(request, make_worker_forecast(request.request_id))


def test_point_in_time_request_excludes_future_bars_and_has_stable_identity() -> None:
    common = {
        "symbol": "AAPL",
        "as_of": AS_OF,
        "provider": SOURCE_PROVIDER,
        "vintage_id": "source-vintage-1",
        "available_at": AS_OF,
        "adjustment_metadata": SOURCE_METADATA,
        "lookback": 40,
        "horizon": 2,
        "config": KronosConfig(sample_count=2),
    }
    all_bars = make_bars(count=41, future=4)
    with_future = build_forecast_request(bars=all_bars, **common)
    without_future = build_forecast_request(
        bars=[bar for bar in all_bars if bar.timestamp <= AS_OF], **common
    )

    assert len(with_future.snapshot.bars) == 40
    assert all(bar.timestamp <= AS_OF for bar in with_future.snapshot.bars)
    assert with_future.snapshot.latest_source_bar_at == AS_OF
    assert with_future.snapshot.source_data_hash == without_future.snapshot.source_data_hash
    assert with_future.request_id == without_future.request_id


def test_snapshot_identity_includes_vintage_and_attempt() -> None:
    first = make_request()
    assert make_request(vintage_id="source-vintage-2").request_id != first.request_id
    assert make_request(attempt_number=1).request_id != first.request_id


def test_default_daily_targets_keep_source_session_clock_not_retrieval_clock() -> None:
    retrieval_time = AS_OF + timedelta(minutes=10)
    request = build_forecast_request(
        bars=make_bars(count=40),
        symbol="AAPL",
        as_of=retrieval_time,
        provider=SOURCE_PROVIDER,
        vintage_id="source-vintage-1",
        available_at=retrieval_time,
        adjustment_metadata=SOURCE_METADATA,
        lookback=40,
        horizon=2,
        config=KronosConfig(sample_count=2),
    )

    assert request.future_timestamps == (
        datetime(2026, 8, 17, 20, tzinfo=UTC),
        datetime(2026, 8, 18, 20, tzinfo=UTC),
    )


def test_snapshot_rejects_duplicate_wrong_symbol_and_impossible_availability() -> None:
    bars = make_bars(count=40)
    common = {
        "symbol": "AAPL",
        "as_of": AS_OF,
        "provider": SOURCE_PROVIDER,
        "vintage_id": "source-vintage-1",
        "available_at": AS_OF,
        "adjustment_metadata": SOURCE_METADATA,
        "lookback": 40,
        "horizon": 2,
    }
    with pytest.raises(ValueError, match="duplicate"):
        build_forecast_request(bars=[*bars, bars[-1]], **common)
    with pytest.raises(ValueError, match="symbol"):
        build_forecast_request(
            bars=[bars[0].model_copy(update={"symbol": "MSFT"}), *bars[1:]], **common
        )
    with pytest.raises(ValueError, match="available before"):
        build_forecast_request(
            bars=bars,
            **{**common, "available_at": AS_OF - timedelta(minutes=1)},
        )


def test_insufficient_lookback_is_deterministic_and_never_calls_provider() -> None:
    class ProviderThatMustNotRun:
        def forecast(self, _request: object) -> WorkerForecast:
            raise AssertionError("provider must not run")

    service = ShadowForecastService(ProviderThatMustNotRun(), clock=lambda: AS_OF)
    kwargs = {
        "bars": make_bars(count=10),
        "symbol": "AAPL",
        "as_of": AS_OF,
        "provider": SOURCE_PROVIDER,
        "vintage_id": "source-vintage-1",
        "available_at": AS_OF,
        "adjustment_metadata": SOURCE_METADATA,
        "lookback": 40,
        "horizon": 2,
        "config": KronosConfig(sample_count=2),
    }
    first = service.run(**kwargs)
    second = service.run(**kwargs)

    assert first.status is ForecastStatus.INSUFFICIENT_DATA
    assert first.forecast_id == second.forecast_id
    assert first.error_code == "INSUFFICIENT_LOOKBACK"


def test_success_derives_descriptive_features_without_confidence_language() -> None:
    record = make_success_record()

    assert record.status is ForecastStatus.SUCCESS
    assert record.features is not None
    assert record.features.terminal_return_median == Decimal("0.1")
    assert record.features.high_excursion == Decimal("0.12")
    assert record.features.low_excursion == Decimal("-0.01")
    assert record.features.direction is ForecastDirection.UP
    assert record.features.sample_terminal_return_dispersion == Decimal("0")
    assert "confidence" not in ForecastFeatures.model_fields
    assert "risk" not in ForecastFeatures.model_fields


def test_provider_failure_becomes_safe_failed_shadow_record() -> None:
    class FailedProvider:
        def forecast(self, _request: object) -> WorkerForecast:
            raise ForecastProviderError("credential=do-not-persist")

    record = ShadowForecastService(FailedProvider(), clock=lambda: AS_OF).run(
        bars=make_bars(count=40),
        symbol="AAPL",
        as_of=AS_OF,
        provider=SOURCE_PROVIDER,
        vintage_id="source-vintage-1",
        available_at=AS_OF,
        adjustment_metadata=SOURCE_METADATA,
        lookback=40,
        horizon=2,
        config=KronosConfig(sample_count=2),
    )

    assert record.status is ForecastStatus.FAILED
    assert record.error_code == "PROVIDER_ERROR"
    assert "credential" not in json.dumps(record.model_dump(mode="json"))


def test_worker_contract_mismatch_becomes_failed_artifact() -> None:
    class WrongSampleCountProvider:
        def forecast(self, request: ForecastRequest) -> WorkerForecast:
            worker = make_worker_forecast(request.request_id)
            return worker.model_copy(update={"sample_paths": (worker.sample_paths[0],)})

    record = ShadowForecastService(WrongSampleCountProvider(), clock=lambda: AS_OF).run(
        bars=make_bars(count=40),
        symbol="AAPL",
        as_of=AS_OF,
        provider=SOURCE_PROVIDER,
        vintage_id="source-vintage-1",
        available_at=AS_OF,
        adjustment_metadata=SOURCE_METADATA,
        lookback=40,
        horizon=2,
        config=KronosConfig(sample_count=2),
    )

    assert record.status is ForecastStatus.FAILED
    assert record.error_code == "WORKER_CONTRACT_MISMATCH"


def test_success_record_rejects_timestamp_that_disagrees_with_worker() -> None:
    record = make_success_record()
    payload = record.model_dump(mode="python")
    payload["generated_at"] = record.generated_at + timedelta(seconds=1)

    with pytest.raises(ValueError, match="timestamp must match"):
        ForecastRecord.model_validate(payload)


def test_subprocess_provider_stages_worker_and_scrubs_application_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    request = make_request()
    expected = make_worker_forecast(request.request_id)
    captured: dict[str, object] = {}

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = args[0]
        captured.update(kwargs)
        return subprocess.CompletedProcess(
            args=args, returncode=0, stdout=expected.model_dump_json(), stderr=""
        )

    for name in (
        "ALPACA_API_KEY",
        "ALPACA_API_SECRET",
        "ALPACA_PAPER_API_KEY",
        "ALPACA_LIVE_API_SECRET",
        "QUANTBOT_DB_PATH",
        "QUANTBOT_LIVE_ACK",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "must-not-cross-boundary")
    monkeypatch.setattr(subprocess, "run", fake_run)
    cache = tmp_path / "hf-cache"
    worker_python = tmp_path / "worker-python.exe"
    worker_python.touch()
    provider = KronosSignalProvider(
        python_executable=worker_python,
        kronos_repository=tmp_path,
        timeout_seconds=30,
        cache_directory=cache,
    )

    result = provider.forecast(request)

    environment = captured["env"]
    assert isinstance(environment, dict)
    assert not any(str(key).startswith(("ALPACA_", "QUANTBOT_")) for key in environment)
    assert "PYTHONPATH" not in environment
    assert environment["HF_HUB_OFFLINE"] == "1"
    assert captured["shell"] is False
    command = captured["command"]
    assert isinstance(command, list)
    assert command[1] == "-I"
    assert command[2] == "-B"
    assert Path(command[3]).parent == cache.resolve()
    assert Path(command[3]).name.startswith("kronos-worker-")
    assert captured["cwd"] == cache.resolve()
    assert json.loads(str(captured["input"]))["request_id"] == request.request_id
    assert result == expected


def test_forecast_ledger_is_immutable_idempotent_and_has_no_active_tables(
    tmp_path: Path,
) -> None:
    record = make_success_record()
    database = ForecastDatabase(tmp_path / "forecast.db")
    try:
        with database.transaction() as session:
            ledger = ForecastLedger(session)
            assert ledger.save_forecast(record) is True
            assert ledger.save_forecast(record) is False
            assert ledger.get_forecast(record.forecast_id) == record

            conflicting = record.model_copy(
                update={
                    "features": ForecastFeatures(
                        terminal_return_mean="9",
                        terminal_return_median="9",
                        high_excursion="9",
                        low_excursion="-9",
                        direction=ForecastDirection.UP,
                        sample_terminal_return_dispersion="9",
                    )
                }
            )
            with pytest.raises(ValidationError, match="features"):
                ledger.save_forecast(conflicting)

        table_names = set(inspect(database.engine).get_table_names())
        assert "model_forecasts" in table_names
        assert not {"qualification_days", "order_intents", "signals"} & table_names
    finally:
        database.close()


def _outcome_bar(offset: int) -> Bar:
    price = Decimal(100 + offset)
    return Bar(
        symbol="AAPL",
        timestamp=AS_OF + timedelta(days=offset),
        open=price,
        high=price + 3,
        low=price - 1,
        close=price + 1,
        volume="1000",
        adjustment="1",
    )


def test_scoring_appends_exact_target_outcome_without_rewriting_forecast(
    tmp_path: Path,
) -> None:
    record = make_success_record()
    available = AS_OF + timedelta(days=4, minutes=1)
    evaluation = score_forecast(
        record,
        outcome_bars=[_outcome_bar(offset) for offset in (1, 2, 3, 4)],
        outcome_provider=SOURCE_PROVIDER,
        outcome_vintage_id="outcome-vintage-1",
        outcome_available_at=available,
        outcome_adjustment_metadata=SOURCE_METADATA,
        evaluated_at=available + timedelta(hours=1),
    )

    assert evaluation is not None
    assert evaluation.realized_terminal_return == Decimal("0.05")
    assert evaluation.realized_through == AS_OF + timedelta(days=4)
    assert evaluation.evaluated_at == available
    assert evaluation.directionally_correct is True
    assert {score.name for score in evaluation.baselines} == {
        "momentum",
        "persistence",
        "short_reversal",
    }
    assert all(isinstance(score, BaselineScore) for score in evaluation.baselines)
    outcome = json.loads(evaluation.outcome_json)
    assert [bar["timestamp"] for bar in outcome["bars"]] == [
        "2026-08-17T20:00:00Z",
        "2026-08-18T20:00:00Z",
    ]

    database = ForecastDatabase(tmp_path / "evaluation.db")
    try:
        with database.transaction() as session:
            ledger = ForecastLedger(session)
            ledger.save_forecast(record)
            before = ledger.get_forecast(record.forecast_id)
            assert ledger.save_evaluation(evaluation) is True
            assert ledger.save_evaluation(evaluation) is False
            assert ledger.get_forecast(record.forecast_id) == before
    finally:
        database.close()


def test_ledger_rejects_outcome_not_derived_from_linked_forecast(tmp_path: Path) -> None:
    record = make_success_record()
    available = AS_OF + timedelta(days=4, minutes=1)
    evaluation = score_forecast(
        record,
        outcome_bars=[_outcome_bar(3), _outcome_bar(4)],
        outcome_provider=SOURCE_PROVIDER,
        outcome_vintage_id="outcome-vintage-1",
        outcome_available_at=available,
        outcome_adjustment_metadata=SOURCE_METADATA,
        evaluated_at=available,
    )
    assert evaluation is not None
    outcome = json.loads(evaluation.outcome_json)
    outcome["bars"][0]["symbol"] = "MSFT"
    outcome_json = json.dumps(outcome, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    outcome_source_hash = hashlib.sha256(outcome_json.encode("utf-8")).hexdigest()
    forged = evaluation.model_copy(
        update={
            "evaluation_id": calculate_evaluation_id(
                forecast_id=record.forecast_id,
                outcome_source_hash=outcome_source_hash,
                scoring_version=evaluation.scoring_version,
            ),
            "outcome_json": outcome_json,
            "outcome_source_hash": outcome_source_hash,
        }
    )

    database = ForecastDatabase(tmp_path / "forged-evaluation.db")
    try:
        with database.transaction() as session:
            ledger = ForecastLedger(session)
            ledger.save_forecast(record)
            with pytest.raises(StateConflictError, match="canonical score"):
                ledger.save_evaluation(forged)
    finally:
        database.close()


def test_scoring_waits_for_exact_targets_and_point_in_time_availability() -> None:
    record = make_success_record()
    common = {
        "outcome_provider": SOURCE_PROVIDER,
        "outcome_vintage_id": "outcome-vintage-1",
        "outcome_adjustment_metadata": SOURCE_METADATA,
    }
    cutoff = AS_OF + timedelta(days=4, hours=1)

    assert (
        score_forecast(
            record,
            outcome_bars=[_outcome_bar(1), _outcome_bar(2)],
            outcome_available_at=cutoff,
            evaluated_at=cutoff,
            **common,
        )
        is None
    )
    assert (
        score_forecast(
            record,
            outcome_bars=[_outcome_bar(3), _outcome_bar(4)],
            outcome_available_at=cutoff + timedelta(days=1),
            evaluated_at=cutoff,
            **common,
        )
        is None
    )
    with pytest.raises(ValueError, match="before the final target"):
        score_forecast(
            record,
            outcome_bars=[_outcome_bar(3), _outcome_bar(4)],
            outcome_available_at=AS_OF + timedelta(days=3),
            evaluated_at=cutoff,
            **common,
        )


def test_evaluation_baselines_are_deeply_immutable() -> None:
    available = AS_OF + timedelta(days=4, minutes=1)
    evaluation = score_forecast(
        make_success_record(),
        outcome_bars=[_outcome_bar(3), _outcome_bar(4)],
        outcome_provider=SOURCE_PROVIDER,
        outcome_vintage_id="outcome-vintage-1",
        outcome_available_at=available,
        outcome_adjustment_metadata=SOURCE_METADATA,
        evaluated_at=available,
    )
    assert evaluation is not None
    with pytest.raises(ValidationError):
        evaluation.baselines[0].name = "changed"
