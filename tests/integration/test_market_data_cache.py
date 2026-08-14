from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from quantbot.domain import Bar
from quantbot.market_data import (
    AdjustmentMode,
    BarValidationReport,
    MarketDataCache,
    MarketDataFeed,
)
from quantbot.storage import Database, StorageRepository


def bar(symbol: str, day: int, close: str) -> Bar:
    value = Decimal(close)
    return Bar(
        symbol=symbol,
        timestamp=datetime(2026, 8, day, 4, tzinfo=UTC),
        open=value,
        high=value + 1,
        low=value - 1,
        close=value,
        volume=1000,
        adjustment=1,
    )


def report() -> BarValidationReport:
    observations = (bar("QQQ", 28, "570"), bar("SPY", 28, "650"))
    return BarValidationReport(
        provider="alpaca",
        feed=MarketDataFeed.IEX,
        adjustment=AdjustmentMode.ALL,
        timeframe="1Day",
        requested_symbols=("QQQ", "SPY"),
        valid_bars=observations,
        gaps=(),
        eligible_symbols=("QQQ", "SPY"),
        ineligible_symbols=(),
        stale_symbols=(),
        completed_through=datetime(2026, 8, 28, 20, tzinfo=UTC),
    )


def test_validated_cache_is_durable_idempotent_and_provenance_bound(tmp_path: Path) -> None:
    path = tmp_path / "market-data.db"
    database = Database(path)
    with database.transaction() as session:
        repository = StorageRepository(session)
        cache = MarketDataCache(repository)
        assert cache.store(report()) == 2
        assert cache.store(report()) == 0
    database.close()

    reopened = Database(path)
    try:
        with reopened.transaction() as session:
            cache = MarketDataCache(StorageRepository(session))
            restored = cache.load(
                symbol="SPY",
                provider="alpaca",
                feed=MarketDataFeed.IEX,
                adjustment=AdjustmentMode.ALL,
                timeframe="1Day",
            )
            assert restored == [bar("SPY", 28, "650")]
            assert (
                cache.provider_key("alpaca", MarketDataFeed.IEX, AdjustmentMode.ALL, "1Day")
                == "alpaca:iex:all:1day"
            )
    finally:
        reopened.close()
