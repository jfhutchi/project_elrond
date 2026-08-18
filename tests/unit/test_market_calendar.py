"""XNYS calendar parsing: local exchange times must become correct UTC instants."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from quantbot.market_data import (
    AlpacaMarketCalendar,
    MarketDataProtocolError,
    MarketDataRequestError,
    TransportResponse,
    latest_completed_session,
    session_closes,
    session_sequence,
)


class StubTransport:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code
        self.calls = 0

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> TransportResponse:
        self.calls += 1
        assert "APCA-API-KEY-ID" in headers
        return TransportResponse(
            status_code=self.status_code,
            headers={},
            content=json.dumps(self.payload).encode("utf-8"),
        )


def _calendar(payload: Any, status_code: int = 200) -> AlpacaMarketCalendar:
    return AlpacaMarketCalendar(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        transport=StubTransport(payload, status_code),
        max_retries=0,
    )


async def test_summer_winter_and_half_day_closes_convert_to_utc() -> None:
    calendar = _calendar(
        [
            {"date": "2026-07-01", "open": "09:30", "close": "16:00"},
            {"date": "2026-11-27", "open": "09:30", "close": "13:00"},
            {"date": "2026-12-01", "open": "09:30", "close": "16:00"},
        ]
    )

    sessions = await calendar.get_sessions(date(2026, 7, 1), date(2026, 12, 1))

    # EDT is UTC-4, EST is UTC-5, and the half day closes three hours early.
    assert sessions[0].close_at == datetime(2026, 7, 1, 20, 0, tzinfo=UTC)
    assert sessions[1].close_at == datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    assert sessions[2].close_at == datetime(2026, 12, 1, 21, 0, tzinfo=UTC)
    assert sessions[0].open_at == datetime(2026, 7, 1, 13, 30, tzinfo=UTC)


async def test_sessions_are_ordered_and_projected_for_both_consumers() -> None:
    calendar = _calendar(
        [
            {"date": "2026-08-14", "open": "09:30", "close": "16:00"},
            {"date": "2026-08-12", "open": "09:30", "close": "16:00"},
            {"date": "2026-08-13", "open": "09:30", "close": "16:00"},
        ]
    )

    sessions = await calendar.get_sessions(date(2026, 8, 12), date(2026, 8, 14))

    assert [item.session_date.day for item in sessions] == [12, 13, 14]
    assert session_sequence(sessions).sessions == sessions
    assert [item.close_at for item in session_closes(sessions)] == [
        item.close_at for item in sessions
    ]


async def test_duplicate_session_dates_are_rejected() -> None:
    calendar = _calendar(
        [
            {"date": "2026-08-14", "open": "09:30", "close": "16:00"},
            {"date": "2026-08-14", "open": "09:30", "close": "16:00"},
        ]
    )

    with pytest.raises(MarketDataProtocolError, match="duplicate session dates"):
        await calendar.get_sessions(date(2026, 8, 14), date(2026, 8, 14))


async def test_malformed_payloads_fail_closed() -> None:
    with pytest.raises(MarketDataProtocolError, match="JSON array"):
        await _calendar({"date": "2026-08-14"}).get_sessions(
            date(2026, 8, 14), date(2026, 8, 14)
        )

    bad_time = _calendar([{"date": "2026-08-14", "open": "09:30", "close": "not-a-time"}])
    with pytest.raises(MarketDataProtocolError, match="valid time"):
        await bad_time.get_sessions(date(2026, 8, 14), date(2026, 8, 14))

    with pytest.raises(MarketDataRequestError, match="HTTP 403"):
        await _calendar([], status_code=403).get_sessions(date(2026, 8, 14), date(2026, 8, 14))


async def test_latest_completed_session_ignores_sessions_still_open() -> None:
    calendar = _calendar(
        [
            {"date": "2026-08-13", "open": "09:30", "close": "16:00"},
            {"date": "2026-08-14", "open": "09:30", "close": "16:00"},
        ]
    )
    sessions = await calendar.get_sessions(date(2026, 8, 13), date(2026, 8, 14))

    midday = latest_completed_session(sessions, datetime(2026, 8, 14, 17, 0, tzinfo=UTC))
    after_close = latest_completed_session(sessions, datetime(2026, 8, 14, 20, 5, tzinfo=UTC))
    before_any = latest_completed_session(sessions, datetime(2026, 8, 13, 12, 0, tzinfo=UTC))

    assert midday is not None and midday.session_date == date(2026, 8, 13)
    assert after_close is not None and after_close.session_date == date(2026, 8, 14)
    assert before_any is None


async def test_inverted_date_window_is_rejected_before_any_request() -> None:
    transport = StubTransport([])
    calendar = AlpacaMarketCalendar(
        api_key=SecretStr("key"),
        api_secret=SecretStr("secret"),
        transport=transport,
    )

    with pytest.raises(ValueError, match="on or after start"):
        await calendar.get_sessions(date(2026, 8, 14), date(2026, 8, 13))

    assert transport.calls == 0


async def test_a_single_session_cannot_form_a_sequence() -> None:
    calendar = _calendar([{"date": "2026-08-14", "open": "09:30", "close": "16:00"}])
    sessions = await calendar.get_sessions(date(2026, 8, 14), date(2026, 8, 14))

    with pytest.raises(MarketDataProtocolError, match="strictly increasing"):
        session_sequence(sessions)
