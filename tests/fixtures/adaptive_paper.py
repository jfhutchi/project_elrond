"""Synthetic regular-session market receipts; never prospective broker evidence."""

from datetime import UTC, datetime, timedelta

from tests.unit.learner.test_experiment_contract import manifest_data

from quantbot.domain import Bar
from quantbot.learner.data import DailyInput
from quantbot.learner.experiment_contract import ExperimentManifest
from quantbot.learner.forward_source import Receipt, Snapshot
from quantbot.market_data.base import AdjustmentMode, BarQuery, MarketDataFeed
from quantbot.strategy import XNYSSession, XNYSSessionSequence

OPEN = datetime(2026, 10, 6, 13, 30, tzinfo=UTC)


def manifest():
    return ExperimentManifest(**manifest_data())


def snapshot(minute=35, *, missing=(), session_minutes=390, opened=OPEN):
    received = opened + timedelta(minutes=minute, seconds=1)
    symbols = tuple(row.symbol for row in manifest().roster) + ("SPY",)
    bars = tuple(
        DailyInput(
            bar=Bar(symbol=symbol, timestamp=opened + timedelta(minutes=index * 5),
                    open=str(100 + index), high=str(100 + index), low=str(100 + index),
                    close=str(100 + index), volume="1", adjustment="1"),
            available_at=received,
        )
        for symbol in symbols for index in range(minute // 5)
        if (symbol, index) not in missing
    )
    return Snapshot(
        source_id="alpaca:iex:raw:5min",
        sessions=XNYSSessionSequence(calendar="XNYS", sessions=tuple(XNYSSession(
            session_date=(opened + timedelta(days=day)).date(),
            open_at=opened + timedelta(days=day),
            close_at=opened + timedelta(days=day, minutes=session_minutes),
        ) for day in range(2))),
        inputs=bars, received_at=received,
        query=BarQuery(symbols=symbols, start=opened, end=received, timeframe="5Min",
                       feed=MarketDataFeed.IEX, adjustment=AdjustmentMode.RAW, limit=10000),
        receipts=tuple(Receipt(
            url=url, query_hash="a" * 64, response_hash="b" * 64,
            received_at=received, status_code=200, response_bytes=100,
        ) for url in ("https://paper-api.alpaca.markets/v2/calendar",
                      "https://data.alpaca.markets/v2/stocks/bars")),
    )
