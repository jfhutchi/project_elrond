"""Fetch a long daily-bar history into a separate research database.

The operational cache deliberately holds only the strategy's warmup window, and its
contents feed the ``bar_set_hash`` recorded against live signals. Research history
therefore goes into its own database so live audit provenance is never disturbed.

Usage:
    uv run python scripts/fetch_research_bars.py [START_DATE] [DB_PATH]
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from quantbot.config import Settings
from quantbot.market_data import (
    AlpacaMarketCalendar,
    AlpacaMarketDataClient,
    BarQuery,
    HttpxMarketDataTransport,
    MarketDataCache,
    align_daily_bars_to_session_closes,
    latest_completed_session,
    session_closes,
    validate_bar_batch,
)
from quantbot.runtime import market_data_settings, paper_credentials, runtime_paths
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import load_strategy_config

DEFAULT_START = date(2015, 1, 1)
DEFAULT_DB = Path("research") / "bars.db"


async def main() -> int:
    start = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_START
    db_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DB
    db_path.parent.mkdir(parents=True, exist_ok=True)

    settings = Settings()
    key, secret = paper_credentials(settings)
    config = load_strategy_config(runtime_paths().config)
    feed = market_data_settings()
    transport = HttpxMarketDataTransport()
    database = Database(db_path)

    calendar = AlpacaMarketCalendar(api_key=key, api_secret=secret, transport=transport)
    client = AlpacaMarketDataClient(api_key=key, api_secret=secret, transport=transport)

    today = datetime.now(UTC).date()
    print(f"universe {len(config.universe)} symbols, {start} .. {today} -> {db_path}")
    sessions = await calendar.get_sessions(start, today)
    completed = latest_completed_session(sessions, datetime.now(UTC))
    if completed is None:
        print("no completed session in window")
        return 1
    closes = session_closes(sessions)
    print(f"calendar sessions: {len(sessions)}, last completed {completed.session_date}")

    stored_total = 0
    year = start.year
    while year <= today.year:
        window = tuple(
            session
            for session in sessions
            if session.session_date.year == year and session.close_at <= completed.close_at
        )
        if not window:
            year += 1
            continue
        query = BarQuery(
            symbols=config.universe,
            start=datetime.combine(window[0].session_date, time.min, tzinfo=UTC),
            end=window[-1].close_at,
            timeframe=feed.timeframe,
            feed=feed.feed,
            adjustment=feed.adjustment,
            limit=10000,
        )
        batch = await client.get_historical_bars(query)
        aligned = align_daily_bars_to_session_closes(batch, sessions=closes)
        expected = tuple(session.close_at for session in window)
        in_window = tuple(bar for bar in aligned.bars if expected[0] <= bar.timestamp <= expected[-1])
        report = validate_bar_batch(
            aligned.model_copy(update={"bars": in_window}),
            expected_timestamps=expected,
            completed_through=completed.close_at,
        )
        with database.transaction() as session:
            stored = MarketDataCache(StorageRepository(session)).store(report)
        stored_total += stored
        gaps = len(report.ineligible_symbols)
        print(
            f"  {year}: sessions={len(window):3} bars={len(in_window):5} stored={stored:5}"
            f" incomplete_symbols={gaps}"
        )
        year += 1

    provider_key = MarketDataCache.provider_key(
        feed.provider, feed.feed, feed.adjustment, feed.timeframe
    )
    print(f"\nstored {stored_total} new bars\ncoverage by symbol:")
    with database.transaction() as session:
        repository = StorageRepository(session)
        for symbol in config.universe:
            bars = repository.list_bars(symbol=symbol, provider=provider_key)
            if bars:
                span = f"{bars[0].timestamp.date()} .. {bars[-1].timestamp.date()}"
                print(f"  {symbol:5} {len(bars):5} bars  {span}")
            else:
                print(f"  {symbol:5}     0 bars  NO DATA")
    database.close()
    await transport.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
