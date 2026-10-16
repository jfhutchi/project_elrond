"""Entry data may arrive slowly; freshness and independent protection still bind."""

import argparse
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from quantbot.market_data.base import (
    AdjustmentMode,
    BarQuery,
    MarketDataFeed,
    MarketDataTransportError,
    TransportResponse,
)


@pytest.fixture
def commissioned_runtime(tmp_path):
    from tests.integration.paper_core.test_final_repair import commissioned

    db, _, release, *_ = commissioned(tmp_path, gross=True)
    db.close()
    return release


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["trader", "guardian", "outcomes"])
async def test_production_entry_data_can_exceed_two_seconds_without_slowing_protection(
    commissioned_runtime, monkeypatch, role
):
    from quantbot.paper_core import __main__ as cli
    from quantbot.paper_core import roles

    release = commissioned_runtime
    key = SecretStr("offline")
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda **_: SimpleNamespace(
            EXPECTED_ACCOUNT_ID="paper",
            TRADING_MODE=SimpleNamespace(value="PAPER"),
            ALPACA_PAPER_API_KEY=key,
            ALPACA_PAPER_API_SECRET=key,
        ),
    )
    monkeypatch.setattr(
        "quantbot.config.validate_trading_authority",
        lambda *_: SimpleNamespace(allowed=True),
    )

    class SlowMarket:
        async def request(self, method, url, **kwargs):
            assert method == "GET" and url.endswith("/stocks/bars")
            try:
                async with asyncio.timeout(float(kwargs["timeout_seconds"])):
                    await asyncio.sleep(2.1)
            except TimeoutError as error:
                raise MarketDataTransportError("slow response") from error
            return TransportResponse(
                status_code=200, headers={}, content=json.dumps({"bars": {}}).encode()
            )

        async def aclose(self):
            pass

    monkeypatch.setattr("quantbot.market_data.transports.HttpxMarketDataTransport", SlowMarket)

    async def check(store, broker, config, selected_role, **_):
        assert selected_role == role
        # Order POSTs, clock/calendar reads and guardian marks keep their short limits.
        assert broker.adapter._timeout_seconds == 2
        assert broker.calendar._timeout_seconds == 2
        if role != "trader":
            assert broker.market._timeout_seconds == 2
            return
        end = store.clocks.sample()[0]
        batch = await broker.market.get_historical_bars(
            BarQuery(
                symbols=("AAPL", "SPY"),
                start=end - timedelta(minutes=40),
                end=end,
                timeframe="5Min",
                feed=MarketDataFeed.IEX,
                adjustment=AdjustmentMode.ALL,
                limit=100,
            )
        )
        assert not batch.bars

    monkeypatch.setattr(roles, "run_role", check)
    await cli._run(
        argparse.Namespace(
            role=role,
            fixture_driver=None,
            submission_disabled=True,
            entry_disabled=True,
            cycles=1,
        ),
        release,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("delay", "stale", "should_decide"),
    [(5.1, False, True), (5.1, True, False), (10.1, False, False)],
)
async def test_role_allows_bounded_slow_cohort_but_never_stale_or_unbounded_data(
    tmp_path, delay, stale, should_decide
):
    from tests.integration.paper_core.test_daily_selection import NOW, cohort, prepare

    from quantbot.paper_core import roles

    db, store, broker, *_ = prepare(tmp_path)

    class Clock:
        process_id = "bounded-entry-data"

        def sample(self):
            return NOW, 100.0

    store.clocks = Clock()
    captured = cohort(at=NOW - timedelta(minutes=5) if stale else NOW)
    if stale:
        captured = captured.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(
                        update={
                            "recorded_at_utc": NOW,
                            "available_at": NOW,
                        }
                    )
                    for o in captured.observations
                ),
            }
        )

    async def acquire():
        await asyncio.sleep(delay)
        return captured

    broker.observation_cohort = acquire
    runtime = SimpleNamespace(
        entry_poll_seconds=15, guardian_poll_seconds=15, outcomes_poll_seconds=15
    )
    try:
        await roles.run_role(store, broker, runtime, "trader", cycles=1)
        decisions = store.documents("core_decisions")
        assert any(d["action"] == "BUY" for d in decisions) is should_decide
        # This role test has no independent guardian; even a fresh candidate cannot POST.
        assert not broker.posts
        if not should_decide:
            assert store.documents("core_role_heartbeats")[-1]["objections"]
    finally:
        db.close()
