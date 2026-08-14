"""Broker-neutral contracts for authoritative paper and live state."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quantbot.domain import Account, BrokerOrder, Fill, MarketClock, OrderIntent, Position


class BrokerError(RuntimeError):
    """Base error for fail-closed broker operations."""


class BrokerRequestError(BrokerError):
    """Raised when an authoritative broker request fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class BrokerProtocolError(BrokerError):
    """Raised when a broker response violates its expected schema."""


class BrokerTransportError(BrokerError):
    """Raised by a transport for an ambiguous connectivity failure."""


class SubmissionUncertainError(BrokerError):
    """Raised when a submission outcome cannot be proven safely."""


class BrokerModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="after")
    @classmethod
    def normalize_datetime(cls, value: object) -> object:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(UTC)


class BrokerAccountSnapshot(BrokerModel):
    account: Account
    account_number: str
    status: str
    order_entry_allowed: bool
    restrictions: tuple[str, ...]


class BrokerHealth(BrokerModel):
    healthy: bool
    provider: str
    environment: str
    account_id: str | None
    reasons: tuple[str, ...]


class BrokerTransportResponse(BrokerModel):
    status_code: int = Field(ge=100, le=599)
    headers: Mapping[str, str]
    content: bytes


class BrokerTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: Decimal,
    ) -> BrokerTransportResponse: ...


BrokerSleeper = Callable[[Decimal], Awaitable[None]]


class BrokerAdapter(Protocol):
    provider: str
    environment: str

    async def connect(self) -> BrokerHealth: ...

    async def health_check(self) -> BrokerHealth: ...

    async def get_account(self) -> BrokerAccountSnapshot: ...

    async def get_cash(self) -> Decimal: ...

    async def get_buying_power(self) -> Decimal: ...

    async def get_positions(self) -> Sequence[Position]: ...

    async def get_position(self, symbol: str) -> Position | None: ...

    async def get_open_orders(self) -> Sequence[BrokerOrder]: ...

    async def get_order(self, order_id: str) -> BrokerOrder: ...

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
    ) -> BrokerOrder | None: ...

    async def submit_order(self, order: OrderIntent) -> BrokerOrder: ...

    async def cancel_order(self, order_id: str) -> bool: ...

    async def cancel_all_orders(self) -> Sequence[str]: ...

    async def get_fills(self, *, after: datetime | None = None) -> Sequence[Fill]: ...

    async def get_market_clock(self) -> MarketClock: ...
