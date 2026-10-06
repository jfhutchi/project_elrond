"""Read-only market clock and session-calendar comparison for the console.

No ledger writes, heartbeat synthesis, or execution authority. The calendar endpoint supplies
expected session bounds independently of the clock's reported is_open flag. Both are needed
before a normal closure can suppress market-hours liveness warnings.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict

from quantbot.domain.models import MarketClock
from quantbot.strategy import XNYSSession

EASTERN = ZoneInfo("America/New_York")
# Console-only project defaults; these do not change scheduler or risk policy.
REFRESH_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 5
CLOCK_SKEW_SECONDS = 2


class MarketStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    checked_at: AwareDatetime | None = None
    broker_at: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    reported_open: bool | None = None
    expected_open: bool | None = None
    session_open: AwareDatetime | None = None
    session_close: AwareDatetime | None = None
    next_open: AwareDatetime | None = None
    clock_status: Literal['UNVERIFIED', 'HEALTHY', 'CLOCK_SYNC_REQUIRED', 'STALE_SAMPLE'] = (
        'UNVERIFIED'
    )
    clock_offset_seconds: float | None = None
    # Informational closure evidence only; a skewed clock never gains market authority.
    closed_session_consistent: bool = False

    @property
    def label(self) -> str:
        if self.reported_open is None:
            return "Market status unavailable"
        return "Market open" if self.reported_open else "Market closed"

    @property
    def warning(self) -> str | None:
        if self.clock_status == 'CLOCK_SYNC_REQUIRED':
            return ('local clock synchronization requires operator review; '
                    'market authority is unverified')
        if self.reported_open is None:
            return (
                "market status could not be verified; the clock and session calendar need checking"
            )
        if self.expected_open and not self.reported_open:
            return "the market is scheduled to be open, but the broker reports it closed"
        if self.reported_open and self.expected_open is False:
            return "the broker reports the market open outside the scheduled session"
        return None

    @property
    def checked_eastern(self) -> str | None:
        return (
            self.checked_at.astimezone(EASTERN).strftime("%H:%M:%S %Z") if self.checked_at else None
        )

    def current(self, now: datetime) -> MarketStatus:
        if self.checked_at is not None and self.checked_at <= now and self.valid_until is not None:
            if now < self.valid_until:
                return self
        return MarketStatus()

    def loop_overdue(self, last_at: datetime | None, now: datetime, minutes: int) -> bool:
        """Only count silence within the current scheduled open session.

        A verified closure is normal. An unavailable clock must not hide a known stale loop.
        Existing protection failures and never-evaluated positions are handled separately.
        """
        current = self.current(now)
        if current.closed_session_consistent or (
            current.expected_open is False and current.reported_open is False
        ):
            return False
        if current.expected_open and current.session_open is not None:
            baseline = max(last_at or current.session_open, current.session_open)
            return now - baseline >= timedelta(minutes=minutes)
        return last_at is not None and now - last_at >= timedelta(minutes=minutes)


def assess_market(
    clock: MarketClock, sessions: tuple[XNYSSession, ...], *, now: datetime
) -> MarketStatus:
    """Compare a fresh clock against a successfully fetched calendar for today's ET date."""
    if clock.timestamp.tzinfo is None or clock.timestamp.utcoffset() is None:
        return MarketStatus()
    age = (now - clock.timestamp).total_seconds()
    day = now.astimezone(EASTERN).date()
    diagnostic = MarketStatus(
        checked_at=now, broker_at=clock.timestamp,
        valid_until=now + timedelta(seconds=REFRESH_SECONDS),
        clock_status='CLOCK_SYNC_REQUIRED' if age < -CLOCK_SKEW_SECONDS else 'STALE_SAMPLE',
        clock_offset_seconds=-age,
    )
    if not -CLOCK_SKEW_SECONDS <= age < REFRESH_SECONDS:
        # A future provider timestamp diagnoses clock skew. An old timestamp cannot
        # distinguish local skew from stale provider data and remains unverified.
        if age >= REFRESH_SECONDS or abs(age) >= REFRESH_SECONDS:
            return diagnostic
    if clock.timestamp.astimezone(EASTERN).date() != day:
        return diagnostic if age < -CLOCK_SKEW_SECONDS else MarketStatus()
    if any(session.session_date != day for session in sessions) or len(sessions) > 1:
        return diagnostic if age < -CLOCK_SKEW_SECONDS else MarketStatus()
    today = sessions[0] if sessions else None
    def phase(at: datetime) -> str:
        if today is None:
            return 'NO_SESSION'
        if at < today.open_at:
            return 'PRE_OPEN'
        return 'OPEN' if at < today.close_at else 'AFTER_CLOSE'

    closed_consistent = (
        not clock.is_open and phase(clock.timestamp) == phase(now) != 'OPEN'
    )
    expected = today is not None and today.open_at <= clock.timestamp < today.close_at
    if expected != (today is not None and today.open_at <= now < today.close_at):
        return diagnostic if age < -CLOCK_SKEW_SECONDS else MarketStatus()
    # A measurement made before an open/close must never be carried across that boundary.
    midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=EASTERN)
    boundaries = [min(clock.timestamp, now) + timedelta(seconds=REFRESH_SECONDS), midnight]
    if today is not None:
        boundaries.extend(at for at in (today.open_at, today.close_at) if at > clock.timestamp)
    if age < -CLOCK_SKEW_SECONDS:
        if today is not None:
            # Diagnostic waits expire when either advancing clock reaches a boundary.
            boundaries.extend(at + timedelta(seconds=age)
                              for at in (today.open_at, today.close_at)
                              if at > clock.timestamp)
        return diagnostic.model_copy(update={
            'valid_until': min(boundaries),
            'closed_session_consistent': closed_consistent,
        }).current(now)
    status = MarketStatus(
        checked_at=now,
        broker_at=clock.timestamp,
        valid_until=min(boundaries),
        reported_open=clock.is_open,
        expected_open=expected,
        session_open=today.open_at if today else None,
        session_close=today.close_at if today else None,
        next_open=clock.next_open,
        clock_status='HEALTHY',
        clock_offset_seconds=-age,
        closed_session_consistent=closed_consistent,
    )
    return status.current(now)


class MarketStatusCache:
    """One bounded probe per minute or session boundary, shared by concurrent page requests."""

    def __init__(
        self,
        probe: Callable[[], Awaitable[MarketStatus]] | None,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._probe = probe
        self._now = now
        self._status = MarketStatus()
        self._retry_at = datetime.min.replace(tzinfo=UTC)
        self._lock = asyncio.Lock()

    def current(self) -> MarketStatus:
        return self._status.current(self._now())

    async def refresh(self) -> MarketStatus:
        if self._probe is None:
            return self.current()
        async with self._lock:
            now = self._now()
            if now < self._retry_at:
                return self.current()
            try:
                async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                    self._status = (await self._probe()).current(self._now())
            except Exception:  # noqa: BLE001 -- never expose transport errors or credentials
                self._status = MarketStatus()
            self._retry_at = self._status.valid_until or (
                self._now() + timedelta(seconds=REFRESH_SECONDS)
            )
            return self.current()


def paper_market_probe() -> Callable[[], Awaitable[MarketStatus]]:
    """Build a PAPER-only GET capability; configuration errors are handled by the CLI."""
    import httpx

    from quantbot.brokers import AlpacaPaperBrokerAdapter
    from quantbot.brokers.transports import HttpxBrokerTransport
    from quantbot.config import Settings
    from quantbot.market_data.calendar import AlpacaMarketCalendar
    from quantbot.market_data.transports import HttpxMarketDataTransport
    from quantbot.runtime import paper_credentials, runtime_paths
    from quantbot.strategy.config import load_strategy_config

    settings = Settings()
    if settings.TRADING_MODE.value != "PAPER":
        raise ValueError("market status requires PAPER mode")
    key, secret = paper_credentials(settings)
    if load_strategy_config(runtime_paths().config).calendar != "XNYS":
        raise ValueError("market status requires the XNYS calendar")

    async def probe() -> MarketStatus:
        async with httpx.AsyncClient() as client:
            broker = AlpacaPaperBrokerAdapter(
                api_key=key,
                api_secret=secret,
                transport=HttpxBrokerTransport(client),
                max_retries=0,
            )
            calendar = AlpacaMarketCalendar(
                api_key=key,
                api_secret=secret,
                transport=HttpxMarketDataTransport(client),
                max_retries=0,
            )
            day = datetime.now(EASTERN).date()
            sessions = await calendar.get_sessions(day, day)
            clock = await broker.get_market_clock()
            now = datetime.now(UTC)
            if now.astimezone(EASTERN).date() != day:
                return MarketStatus()
            return assess_market(clock, sessions, now=now)

    return probe
