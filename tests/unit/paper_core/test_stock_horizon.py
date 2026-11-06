"""Calendar horizon exclusions are conclusive; missing data is still retryable."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.unit.paper_core.test_crypto_broker_data import broker_fixture

from quantbot.paper_core import roles
from quantbot.paper_core.contracts import ObservationCohort

REASON = "STOCK_SIGNAL_HORIZON_EXCEEDS_SESSION"


@pytest.mark.parametrize(
    "hour,minute,close,excluded",
    [(20, 30, "16:00", False), (20, 35, "16:00", True), (17, 35, "13:00", True)],
)
async def test_actual_calendar_horizon_excludes_before_bar_or_asset_reads(
    hour, minute, close, excluded
):
    now = datetime(2026, 11, 6, hour, minute, 3, tzinfo=UTC)
    broker, provider = broker_fixture(now)
    provider.market_open = True
    request = provider.request

    async def calendar_response(method, url, **kwargs):
        response = await request(method, url, **kwargs)
        if url.endswith("/calendar"):
            payload = json.loads(response.content)
            for session in payload:
                session["close"] = close
            return response.model_copy(update={"content": json.dumps(payload).encode()})
        return response

    provider.request = calendar_response
    captured = await broker._acquire_cohort(("XLE",), verify_assets=True)
    if excluded:
        assert captured.unavailable == (("XLE", REASON),)
        assert not captured.observations
        assert not any(url.endswith("/bars") or "/assets/" in url for _, url, _ in provider.calls)
    else:
        assert len(captured.observations) == 1
        assert not captured.unavailable


@pytest.mark.parametrize(
    "unavailable,crypto,gaps,conclusive",
    [
        ((("XLE", REASON),), (), (), True),
        ((), (), (), False),
        ((("XLE", "FRESH_COMPLETE_BAR_UNAVAILABLE"),), (), (), False),
        (
            (("XLE", REASON), ("BTC/USD", "CRYPTO_CONTIGUOUS_HISTORY_UNAVAILABLE")),
            ("BTC/USD",),
            (),
            False,
        ),
        ((("XLE", REASON),), ("BTC/USD",), ("CRYPTO:TimeoutError",), False),
    ],
)
def test_only_conclusive_horizon_empty_cohort_consumes_boundary(
    unavailable, crypto, gaps, conclusive
):
    now = datetime(2026, 11, 6, 20, 35, tzinfo=UTC)
    cohort = ObservationCohort(
        source_end=now,
        session_open=now - timedelta(hours=6),
        session_close=now + timedelta(minutes=25),
        observations=(),
        unavailable=unavailable,
        crypto_symbols=crypto,
        market_gaps=gaps,
    )
    assert roles._horizon_only_empty_cohort(cohort) is conclusive
