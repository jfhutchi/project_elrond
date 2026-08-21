"""Independent composition root for shadow collection and outcome scoring."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TextIO

from sqlalchemy.exc import SQLAlchemyError

from quantbot.domain import Bar
from quantbot.forecasting.database import (
    ForecastDatabase,
    UnsupportedForecastSchemaVersionError,
)
from quantbot.forecasting.evaluation import score_forecast
from quantbot.forecasting.ledger import ForecastLedger
from quantbot.forecasting.models import (
    ForecastRecord,
    ForecastRequest,
    ForecastStatus,
    KronosConfig,
    WorkerForecast,
)
from quantbot.forecasting.provider import (
    ForecastProviderError,
    KronosSignalProvider,
    SignalProvider,
)
from quantbot.forecasting.service import ShadowForecastService
from quantbot.forecasting.snapshots import build_forecast_request
from quantbot.market_data.base import AdjustmentMode, MarketDataFeed
from quantbot.market_data.cache import MarketDataCache
from quantbot.storage import StateConflictError, decode_decimal, decode_utc


@dataclass(frozen=True, slots=True)
class ShadowCLIContext:
    provider_factory: Callable[[argparse.Namespace], SignalProvider]
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _UnavailableProvider:
    error_code: str

    def forecast(self, _request: ForecastRequest) -> WorkerForecast:
        raise ForecastProviderError("provider setup failed", code=self.error_code)


def _default_provider(args: argparse.Namespace) -> SignalProvider:
    return KronosSignalProvider(
        python_executable=args.python_executable,
        kronos_repository=args.kronos_repository,
        cache_directory=args.cache_directory,
        timeout_seconds=args.timeout_seconds,
    )


def _add_database_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source-database", required=True, type=Path)
    parser.add_argument("--forecast-database", default=Path("kronos-shadow.db"), type=Path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="quantbot-kronos",
        description="Shadow-only candle forecasts with no order path",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    shadow = commands.add_parser("shadow-run")
    _add_database_arguments(shadow)
    shadow.add_argument("--symbol", action="append", required=True)
    shadow.add_argument("--as-of", required=True)
    shadow.add_argument("--market-data-provider", required=True)
    shadow.add_argument(
        "--market-data-feed", required=True, choices=[value.value for value in MarketDataFeed]
    )
    shadow.add_argument(
        "--market-data-adjustment",
        required=True,
        choices=[value.value for value in AdjustmentMode],
    )
    shadow.add_argument("--timeframe", required=True, choices=("1Day",))
    shadow.add_argument("--source-vintage-id", required=True)
    shadow.add_argument("--source-available-at", required=True)
    shadow.add_argument("--lookback", type=int, default=40)
    shadow.add_argument("--horizon", type=int, default=12)
    shadow.add_argument("--temperature", default="0.6")
    shadow.add_argument("--top-p", default="0.9")
    shadow.add_argument("--top-k", type=int, default=0)
    shadow.add_argument("--sample-count", type=int, default=10)
    shadow.add_argument("--seed", type=int, default=0)
    shadow.add_argument("--device", default="cpu", choices=("cpu",))
    shadow.add_argument("--future-timestamp", action="append")
    shadow.add_argument("--attempt-number", type=int, default=0)
    shadow.add_argument("--kronos-repository", required=True, type=Path)
    shadow.add_argument("--python-executable", required=True)
    shadow.add_argument("--cache-directory", required=True, type=Path)
    shadow.add_argument("--timeout-seconds", type=int, default=300)
    shadow.add_argument("--dry-run", action="store_true")
    shadow.add_argument("--no-persist", action="store_true")

    score = commands.add_parser("score")
    _add_database_arguments(score)
    score.add_argument("--as-of", required=True)
    score.add_argument("--symbol", action="append")
    score.add_argument("--outcome-vintage-id", required=True)
    score.add_argument("--outcome-available-at", required=True)
    return parser


def _parse_timestamp(value: str) -> datetime:
    decoded = datetime.fromisoformat(
        value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else "")
    )
    if decoded.tzinfo is None or decoded.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return decoded.astimezone(UTC)


def _database_paths_are_isolated(source: Path, forecast: Path) -> bool:
    forecast_path = forecast.expanduser().resolve()
    protected = [source.expanduser().resolve(), Path("quantbot.db").resolve()]
    active_raw = os.environ.get("QUANTBOT_DB_PATH")
    if active_raw is not None:
        protected.append(Path(active_raw).expanduser().resolve())
    for protected_path in protected:
        if protected_path == forecast_path:
            return False
        if protected_path.exists() and forecast_path.exists():
            try:
                if os.path.samefile(protected_path, forecast_path):
                    return False
            except OSError:
                return False
    return True


def _read_source_bars(
    path: Path,
    *,
    symbol: str,
    provider: str,
    adjustment_metadata_json: str,
) -> list[Bar]:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError("source database does not exist")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT symbol, timestamp, open, high, low, close, volume, adjustment "
            "FROM bars WHERE symbol = ? AND provider = ? AND adjustment_metadata_json = ? "
            "ORDER BY timestamp",
            (symbol, provider, adjustment_metadata_json),
        ).fetchall()
    finally:
        connection.close()
    return [
        Bar(
            symbol=str(row["symbol"]),
            timestamp=decode_utc(str(row["timestamp"])),
            open=decode_decimal(str(row["open"])),
            high=decode_decimal(str(row["high"])),
            low=decode_decimal(str(row["low"])),
            close=decode_decimal(str(row["close"])),
            volume=decode_decimal(str(row["volume"])),
            adjustment=decode_decimal(str(row["adjustment"])),
        )
        for row in rows
    ]


def _config_from_args(args: argparse.Namespace) -> KronosConfig:
    return KronosConfig(
        temperature=Decimal(args.temperature),
        top_p=Decimal(args.top_p),
        top_k=args.top_k,
        sample_count=args.sample_count,
        seed=args.seed,
        device=args.device,
    )


def _source_identity(args: argparse.Namespace) -> tuple[str, dict[str, str], str]:
    feed = MarketDataFeed(args.market_data_feed)
    adjustment = AdjustmentMode(args.market_data_adjustment)
    provider = str(args.market_data_provider).strip().lower()
    key = MarketDataCache.provider_key(provider, feed, adjustment, args.timeframe)
    metadata = {
        "adjustment": adjustment.value,
        "feed": feed.value,
        "provider": provider,
        "timeframe": args.timeframe,
    }
    encoded = json.dumps(metadata, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return key, metadata, encoded


def _record_summary(record: ForecastRecord) -> dict[str, object]:
    summary: dict[str, object] = {
        "as_of": record.request.snapshot.as_of.isoformat(),
        "forecast_id": record.forecast_id,
        "source_bar_count": len(record.request.snapshot.bars),
        "source_data_hash": record.request.snapshot.source_data_hash,
        "status": record.status.value,
        "symbol": record.request.snapshot.symbol,
    }
    if record.features is not None:
        summary["direction"] = record.features.direction.value
        summary["predicted_terminal_return"] = str(record.features.terminal_return_median)
        summary["sample_terminal_return_dispersion"] = str(
            record.features.sample_terminal_return_dispersion
        )
    if record.error_code is not None:
        summary["error_code"] = record.error_code
    return summary


def _existing_forecast(path: Path, forecast_id: str) -> ForecastRecord | None:
    if not path.expanduser().resolve().is_file():
        return None
    database = ForecastDatabase(path)
    try:
        with database.transaction() as session:
            return ForecastLedger(session).get_forecast(forecast_id)
    finally:
        database.close()


def _run_shadow(
    args: argparse.Namespace,
    *,
    context: ShadowCLIContext,
) -> tuple[int, dict[str, object]]:
    as_of = _parse_timestamp(args.as_of)
    if as_of > context.clock():
        raise ValueError("as_of cannot be in the future")
    available_at = _parse_timestamp(args.source_available_at)
    config = _config_from_args(args)
    source_provider, adjustment_metadata, metadata_json = _source_identity(args)
    future_timestamps = (
        None
        if args.future_timestamp is None
        else tuple(_parse_timestamp(value) for value in args.future_timestamp)
    )
    symbols = tuple(dict.fromkeys(str(symbol).strip().upper() for symbol in args.symbol))
    if any(not symbol for symbol in symbols):
        raise ValueError("symbols must not be empty")
    provider: SignalProvider | None = None
    if not args.dry_run:
        try:
            provider = context.provider_factory(args)
        except ForecastProviderError as exc:
            provider = _UnavailableProvider(exc.code)
        except (OSError, ValueError):
            provider = _UnavailableProvider("PROVIDER_SETUP_FAILED")
    results: list[dict[str, object]] = []

    for symbol in symbols:
        bars = _read_source_bars(
            args.source_database,
            symbol=symbol,
            provider=source_provider,
            adjustment_metadata_json=metadata_json,
        )
        request = build_forecast_request(
            bars=bars,
            symbol=symbol,
            as_of=as_of,
            provider=source_provider,
            vintage_id=args.source_vintage_id,
            available_at=available_at,
            lookback=args.lookback,
            horizon=args.horizon,
            config=config,
            adjustment_metadata=adjustment_metadata,
            future_timestamps=future_timestamps,
            attempt_number=args.attempt_number,
        )
        if args.dry_run:
            results.append(
                {
                    "forecast_id": request.request_id,
                    "source_bar_count": len(request.snapshot.bars),
                    "source_data_hash": request.snapshot.source_data_hash,
                    "status": "DRY_RUN",
                    "symbol": symbol,
                }
            )
            continue

        if not args.no_persist:
            existing = _existing_forecast(args.forecast_database, request.request_id)
            if existing is not None:
                results.append(_record_summary(existing))
                continue

        if provider is None:
            raise RuntimeError("provider was not constructed")
        record = ShadowForecastService(provider).run(
            bars=bars,
            symbol=symbol,
            as_of=as_of,
            provider=source_provider,
            vintage_id=args.source_vintage_id,
            available_at=available_at,
            lookback=args.lookback,
            horizon=args.horizon,
            config=config,
            adjustment_metadata=adjustment_metadata,
            future_timestamps=future_timestamps,
            attempt_number=args.attempt_number,
            persist=False,
        )
        results.append(_record_summary(record))
        if not args.no_persist:
            database = ForecastDatabase(args.forecast_database)
            try:
                with database.transaction() as session:
                    ledger = ForecastLedger(session)
                    ledger.save_forecast(record)
            finally:
                database.close()
    ok = all(result["status"] in {ForecastStatus.SUCCESS.value, "DRY_RUN"} for result in results)
    return (0 if ok else 1), {"ok": ok, "results": results, "shadow_only": True}


def _score(args: argparse.Namespace, *, context: ShadowCLIContext) -> tuple[int, dict[str, object]]:
    evaluated_at = _parse_timestamp(args.as_of)
    outcome_available_at = _parse_timestamp(args.outcome_available_at)
    now = context.clock()
    if evaluated_at > now or outcome_available_at > now:
        raise ValueError("outcome scoring timestamps cannot be in the future")
    if not args.forecast_database.expanduser().resolve().is_file():
        raise FileNotFoundError("forecast database does not exist")
    database = ForecastDatabase(args.forecast_database)
    evaluations: list[dict[str, object]] = []
    try:
        with database.transaction() as session:
            ledger = ForecastLedger(session)
            forecasts = ledger.list_forecasts(status=ForecastStatus.SUCCESS)
            allowed: set[str] | None = None
            if args.symbol is not None:
                normalized = tuple(value.strip().upper() for value in args.symbol)
                if any(not value for value in normalized):
                    raise ValueError("symbols must not be empty")
                allowed = set(normalized)
                available = {forecast.request.snapshot.symbol for forecast in forecasts}
                if missing := allowed - available:
                    raise ValueError(
                        "no successful forecast exists for requested symbol: "
                        + ",".join(sorted(missing))
                    )
            for forecast in forecasts:
                if allowed is not None and forecast.request.snapshot.symbol not in allowed:
                    continue
                bars = _read_source_bars(
                    args.source_database,
                    symbol=forecast.request.snapshot.symbol,
                    provider=forecast.request.snapshot.provider,
                    adjustment_metadata_json=(forecast.request.snapshot.adjustment_metadata_json),
                )
                evaluation = score_forecast(
                    forecast,
                    outcome_bars=bars,
                    outcome_provider=forecast.request.snapshot.provider,
                    outcome_vintage_id=args.outcome_vintage_id,
                    outcome_available_at=outcome_available_at,
                    outcome_adjustment_metadata=json.loads(
                        forecast.request.snapshot.adjustment_metadata_json
                    ),
                    evaluated_at=evaluated_at,
                )
                if evaluation is None:
                    continue
                ledger.save_evaluation(evaluation)
                evaluations.append(
                    {
                        "directionally_correct": evaluation.directionally_correct,
                        "evaluation_id": evaluation.evaluation_id,
                        "forecast_id": evaluation.forecast_id,
                        "realized_terminal_return": str(evaluation.realized_terminal_return),
                    }
                )
    finally:
        database.close()
    return 0, {"evaluations": evaluations, "ok": True, "shadow_only": True}


def main(
    argv: Sequence[str] | None = None,
    *,
    context: ShadowCLIContext | None = None,
    output: TextIO | None = None,
) -> int:
    stream = output or sys.stdout
    args = build_parser().parse_args(argv)
    if not _database_paths_are_isolated(args.source_database, args.forecast_database):
        stream.write(_canonical_output({"error": "FORECAST_DATABASE_NOT_ISOLATED", "ok": False}))
        return 2
    active = context or ShadowCLIContext(provider_factory=_default_provider)
    try:
        exit_code, payload = (
            _run_shadow(args, context=active)
            if args.command == "shadow-run"
            else _score(args, context=active)
        )
    except (
        FileNotFoundError,
        OSError,
        SQLAlchemyError,
        StateConflictError,
        UnsupportedForecastSchemaVersionError,
        sqlite3.Error,
        ValueError,
    ):
        exit_code, payload = 2, {"error": "SHADOW_COMMAND_FAILED", "ok": False}
    stream.write(_canonical_output(payload))
    return exit_code


def _canonical_output(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
