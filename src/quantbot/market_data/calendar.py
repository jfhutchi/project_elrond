"""Authoritative XNYS session calendar sourced from the broker."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import cast
from zoneinfo import ZoneInfo

from pydantic import SecretStr, ValidationError

from quantbot.domain import TradingCalendar
from quantbot.market_data.base import (
    AsyncMarketDataTransport,
    MarketDataProtocolError,
    MarketDataRequestError,
    MarketDataTransportError,
    MarketSessionClose,
    Sleeper,
    TransportResponse,
)
from quantbot.strategy import XNYSSession, XNYSSessionSequence

ALPACA_PAPER_CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"
XNYS_TIMEZONE = ZoneInfo("America/New_York")


async def _default_sleeper(delay: Decimal) -> None:
    await asyncio.sleep(float(delay))


def _local_time(value: object, field: str) -> time:
    if not isinstance(value, str):
        raise MarketDataProtocolError(f"calendar {field} must be text")
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise MarketDataProtocolError(f"calendar {field} is not a valid time") from error


def _session_date(value: object) -> date:
    if not isinstance(value, str):
        raise MarketDataProtocolError("calendar date must be text")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise MarketDataProtocolError("calendar date is not a valid date") from error


def _session_from_payload(payload: Mapping[str, object]) -> XNYSSession:
    session_date = _session_date(payload.get("date"))
    open_local = _local_time(payload.get("open"), "open")
    close_local = _local_time(payload.get("close"), "close")
    try:
        return XNYSSession(
            session_date=session_date,
            open_at=datetime.combine(session_date, open_local, tzinfo=XNYS_TIMEZONE),
            close_at=datetime.combine(session_date, close_local, tzinfo=XNYS_TIMEZONE),
        )
    except ValidationError as error:
        raise MarketDataProtocolError("calendar session failed domain validation") from error


def session_closes(
    sessions: tuple[XNYSSession, ...], *, calendar: TradingCalendar = "XNYS"
) -> tuple[MarketSessionClose, ...]:
    """Project sessions onto the market-data alignment contract."""
    return tuple(
        MarketSessionClose(
            calendar=calendar,
            session_date=session.session_date,
            close_at=session.close_at,
        )
        for session in sessions
    )


def session_sequence(sessions: tuple[XNYSSession, ...]) -> XNYSSessionSequence:
    """Wrap sessions in the strictly ordered sequence the strategy requires."""
    try:
        return XNYSSessionSequence(calendar="XNYS", sessions=sessions)
    except ValidationError as error:
        raise MarketDataProtocolError("calendar sessions are not strictly increasing") from error


def latest_completed_session(
    sessions: tuple[XNYSSession, ...],
    now: datetime,
) -> XNYSSession | None:
    """Return the last session whose close has already passed."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    cutoff = now.astimezone(UTC)
    completed = [session for session in sessions if session.close_at <= cutoff]
    return completed[-1] if completed else None


class AlpacaMarketCalendar:
    """Read the broker's authoritative XNYS trading calendar."""

    calendar = "XNYS"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        transport: AsyncMarketDataTransport,
        url: str = ALPACA_PAPER_CALENDAR_URL,
        sleeper: Sleeper | None = None,
        max_retries: int = 2,
        timeout_seconds: Decimal = Decimal("10"),
    ) -> None:
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca credentials must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = transport
        self._url = url
        self._sleeper = sleeper or _default_sleeper
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds

    def __repr__(self) -> str:
        return (
            "AlpacaMarketCalendar(api_key=SecretStr('**********'), "
            "api_secret=SecretStr('**********'))"
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._api_key.get_secret_value(),
            "APCA-API-SECRET-KEY": self._api_secret.get_secret_value(),
            "Accept": "application/json",
        }

    @staticmethod
    def _retry_after(headers: Mapping[str, str]) -> Decimal:
        raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), "1")
        try:
            delay = Decimal(raw)
        except InvalidOperation as error:
            raise MarketDataProtocolError("Retry-After header is invalid") from error
        if not delay.is_finite() or delay < 0:
            raise MarketDataProtocolError("Retry-After header is invalid")
        return delay

    async def _request(self, params: Mapping[str, str]) -> TransportResponse:
        attempt = 0
        while True:
            try:
                response = await self._transport.request(
                    "GET",
                    self._url,
                    headers=self._headers,
                    params=params,
                    timeout_seconds=self._timeout_seconds,
                )
            except MarketDataTransportError as error:
                if attempt >= self._max_retries:
                    raise MarketDataRequestError(
                        "Alpaca calendar transport failed after bounded retries"
                    ) from error
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue
            retryable = response.status_code == 429 or 500 <= response.status_code < 600
            if retryable and attempt < self._max_retries:
                delay = (
                    self._retry_after(response.headers)
                    if response.status_code == 429
                    else Decimal(2) ** attempt
                )
                await self._sleeper(delay)
                attempt += 1
                continue
            if not 200 <= response.status_code < 300:
                raise MarketDataRequestError(
                    f"Alpaca calendar request failed with HTTP {response.status_code}"
                )
            return response

    async def get_sessions(self, start: date, end: date) -> tuple[XNYSSession, ...]:
        """Return every XNYS session in the inclusive date window, in order."""
        if end < start:
            raise ValueError("end must be on or after start")
        response = await self._request({"start": start.isoformat(), "end": end.isoformat()})
        try:
            loaded = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise MarketDataProtocolError("calendar response is not valid JSON") from error
        if not isinstance(loaded, list):
            raise MarketDataProtocolError("calendar response must be a JSON array")
        sessions: list[XNYSSession] = []
        for item in cast(list[object], loaded):
            if not isinstance(item, dict):
                raise MarketDataProtocolError("calendar entry must be an object")
            sessions.append(_session_from_payload(cast(Mapping[str, object], item)))
        ordered = tuple(sorted(sessions, key=lambda session: session.session_date))
        dates = [session.session_date for session in ordered]
        if len(dates) != len(set(dates)):
            raise MarketDataProtocolError("calendar contains duplicate session dates")
        return ordered


CRYPTO_DAY_CLOSE = time(23, 59, 59, 999999)


def crypto_sessions(start: date, end: date) -> tuple[XNYSSession, ...]:
    """Synthesise one session per UTC day for a market that never closes.

    A 24/7 market has no close, but the strategy is defined on completed sessions: it
    evaluates at a close using finished bars and acts at the next open. Treating each UTC
    day as a session preserves that contract exactly — the day's close is the last instant
    of the day, and the next day's open is the first — so the same deterministic machinery
    applies without a parallel code path. Consecutive sessions are one microsecond apart,
    which satisfies the strictly-increasing, non-overlapping requirement.

    Daily crypto bars are stamped 00:00:00Z for the day they cover, so a bar's date maps to
    that day's session.
    """
    if end < start:
        raise ValueError("end must be on or after start")
    sessions: list[XNYSSession] = []
    cursor = start
    while cursor <= end:
        sessions.append(
            XNYSSession(
                session_date=cursor,
                open_at=datetime.combine(cursor, time.min, tzinfo=UTC),
                close_at=datetime.combine(cursor, CRYPTO_DAY_CLOSE, tzinfo=UTC),
            )
        )
        cursor += timedelta(days=1)
    return tuple(sessions)


def crypto_session_sequence(sessions: tuple[XNYSSession, ...]) -> XNYSSessionSequence:
    """Wrap 24/7 sessions in the sequence the strategy requires."""
    try:
        return XNYSSessionSequence(calendar="CRYPTO24", sessions=sessions)
    except ValidationError as error:
        raise MarketDataProtocolError("crypto sessions are not strictly increasing") from error
