"""Alpaca Paper trade-update streaming with bounded recovery."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, cast

from pydantic import SecretStr, ValidationError, model_validator

from quantbot.brokers.alpaca import (
    AlpacaPaperBrokerAdapter,
    _decimal,
    _text,
    _timestamp,
)
from quantbot.brokers.base import BrokerModel, BrokerProtocolError
from quantbot.domain import BrokerOrder, Fill

ALPACA_PAPER_TRADE_STREAM_URL = "wss://paper-api.alpaca.markets/stream"


class TradeStreamError(RuntimeError):
    """Base error for Alpaca Paper trade streaming."""


class TradeStreamDisconnected(TradeStreamError):
    """Raised by a connector when the websocket disconnects."""


class TradeStreamStaleError(TradeStreamError):
    """Raised when no frame arrives within the configured health bound."""


class TradeStreamProtocolError(TradeStreamError):
    """Raised when authentication or update data violates the protocol."""


class TradeStreamIncident(BrokerModel):
    """Secret-safe incident payload emitted before an unknown event fails closed."""

    incident_id: str
    severity: str
    kind: str
    message: str
    occurred_at: datetime
    detail: Mapping[str, str]


class _UnknownTradeUpdateError(TradeStreamProtocolError):
    def __init__(self, incident: TradeStreamIncident) -> None:
        self.incident = incident
        super().__init__("unknown trade update event")


class TradeUpdateEvent(StrEnum):
    ACCEPTED = "accepted"
    PENDING_NEW = "pending_new"
    NEW = "new"
    FILL = "fill"
    PARTIAL_FILL = "partial_fill"
    CANCELED = "canceled"
    EXPIRED = "expired"
    DONE_FOR_DAY = "done_for_day"
    REPLACED = "replaced"
    REJECTED = "rejected"
    HELD = "held"
    STOPPED = "stopped"
    PENDING_CANCEL = "pending_cancel"
    PENDING_REPLACE = "pending_replace"
    CALCULATED = "calculated"
    SUSPENDED = "suspended"
    ORDER_REPLACE_REJECTED = "order_replace_rejected"
    ORDER_CANCEL_REJECTED = "order_cancel_rejected"
    TRADE_BUST = "trade_bust"
    TRADE_CORRECT = "trade_correct"


class TradeUpdate(BrokerModel):
    """Normalized broker update with execution-level fill data when applicable."""

    event: TradeUpdateEvent
    event_id: str
    order: BrokerOrder
    occurred_at: datetime
    fill: Fill | None
    position_quantity: Decimal | None
    connection_generation: int

    @model_validator(mode="after")
    def validate_fill_shape(self) -> TradeUpdate:
        fill_event = self.event in {TradeUpdateEvent.FILL, TradeUpdateEvent.PARTIAL_FILL}
        if fill_event and (self.fill is None or self.position_quantity is None):
            raise ValueError("fill updates require fill and position_quantity")
        if not fill_event and (self.fill is not None or self.position_quantity is not None):
            raise ValueError("non-fill updates cannot contain fill accounting data")
        if not self.event_id or self.event_id != self.event_id.strip():
            raise ValueError("event_id must be nonempty without surrounding whitespace")
        if self.position_quantity is not None and not self.position_quantity.is_finite():
            raise ValueError("position_quantity must be finite")
        if self.connection_generation < 0:
            raise ValueError("connection_generation must be nonnegative")
        return self


class TradeStreamConnection(Protocol):
    async def receive(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...

    async def close(self) -> None: ...


class TradeStreamConnector(Protocol):
    async def open(self, url: str) -> TradeStreamConnection: ...


TradeStreamSleeper = Callable[[Decimal], Awaitable[None]]
TradeStreamReconciler = Callable[[int], Awaitable[None]]
TradeStreamIncidentRecorder = Callable[[TradeStreamIncident], Awaitable[None]]


async def _default_sleeper(delay: Decimal) -> None:
    await asyncio.sleep(float(delay))


def _decode_frame(frame: str | bytes) -> Mapping[str, object]:
    if isinstance(frame, bytes):
        try:
            text = frame.decode("utf-8")
        except UnicodeDecodeError as error:
            raise TradeStreamProtocolError("trade stream frame is not UTF-8 JSON") from error
    else:
        text = frame
    try:
        loaded = json.loads(text, parse_float=Decimal, parse_int=Decimal)
    except json.JSONDecodeError as error:
        raise TradeStreamProtocolError("trade stream frame is not valid JSON") from error
    if not isinstance(loaded, dict):
        raise TradeStreamProtocolError("trade stream frame must be a JSON object")
    return cast(Mapping[str, object], loaded)


def _nested_object(payload: Mapping[str, object], field: str) -> Mapping[str, object]:
    value = payload.get(field)
    if not isinstance(value, dict):
        raise TradeStreamProtocolError(f"trade stream {field} must be an object")
    return cast(Mapping[str, object], value)


def _stable_event_id(data: Mapping[str, object]) -> str:
    canonical = json.dumps(
        data,
        default=str,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "alpaca-event-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]


def _expect_authorized(frame: str | bytes) -> None:
    payload = _decode_frame(frame)
    data = _nested_object(payload, "data")
    if (
        payload.get("stream") != "authorization"
        or data.get("action") != "authenticate"
        or data.get("status") != "authorized"
    ):
        raise TradeStreamProtocolError("trade stream authorization failed")


def _expect_listening(frame: str | bytes) -> None:
    payload = _decode_frame(frame)
    data = _nested_object(payload, "data")
    if payload.get("stream") != "listening" or data.get("streams") != ["trade_updates"]:
        raise TradeStreamProtocolError("trade stream listen acknowledgement failed")


def _parse_trade_update(frame: str | bytes, connection_generation: int) -> TradeUpdate:
    payload = _decode_frame(frame)
    if payload.get("stream") != "trade_updates":
        raise TradeStreamProtocolError("unexpected Alpaca trade stream message")
    data = _nested_object(payload, "data")
    try:
        event_text = _text(data, "event")
    except BrokerProtocolError as error:
        raise TradeStreamProtocolError("trade update event is invalid") from error
    try:
        event = TradeUpdateEvent(event_text)
    except ValueError as error:
        try:
            order_data = _nested_object(data, "order")
            broker_order_id = _text(order_data, "id")
            occurred_at = _timestamp(data, "timestamp")
        except BrokerProtocolError as protocol_error:
            raise TradeStreamProtocolError(
                "unknown trade update has invalid incident data"
            ) from protocol_error
        detail = {"broker_order_id": broker_order_id, "event": event_text}
        incident_seed = json.dumps(
            {"detail": detail, "occurred_at": occurred_at.isoformat()},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        incident = TradeStreamIncident(
            incident_id=(
                "alpaca-stream-incident-"
                + hashlib.sha256(incident_seed.encode("utf-8")).hexdigest()[:40]
            ),
            severity="ERROR",
            kind="UNKNOWN_BROKER_ORDER_EVENT",
            message="Alpaca reported an unknown trade update event",
            occurred_at=occurred_at,
            detail=detail,
        )
        raise _UnknownTradeUpdateError(incident) from error

    order_payload = _nested_object(data, "order")
    try:
        order = AlpacaPaperBrokerAdapter._broker_order_from_payload(order_payload)
        occurred_at = _timestamp(data, "timestamp")
        if event in {TradeUpdateEvent.FILL, TradeUpdateEvent.PARTIAL_FILL}:
            event_id = _text(data, "execution_id")
            price = _decimal(data, "price")
            quantity = _decimal(data, "qty")
            position_quantity = _decimal(data, "position_qty")
            if price <= 0 or quantity <= 0:
                raise BrokerProtocolError("trade execution price and quantity must be positive")
            fill = Fill(
                fill_id=event_id,
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                side=order.side,
                quantity=quantity,
                price=price,
                occurred_at=occurred_at,
            )
        else:
            event_id = _stable_event_id(data)
            fill = None
            position_quantity = None
        return TradeUpdate(
            event=event,
            event_id=event_id,
            order=order,
            occurred_at=occurred_at,
            fill=fill,
            position_quantity=position_quantity,
            connection_generation=connection_generation,
        )
    except (BrokerProtocolError, ValidationError, ValueError) as error:
        raise TradeStreamProtocolError("trade update failed protocol validation") from error


class AlpacaPaperTradeStream:
    """Authenticate, consume, and recover Alpaca Paper trade updates."""

    provider = "alpaca"
    environment = "PAPER"
    url = ALPACA_PAPER_TRADE_STREAM_URL

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        connector: TradeStreamConnector,
        reconcile_after_reconnect: TradeStreamReconciler,
        record_incident: TradeStreamIncidentRecorder,
        stale_after_seconds: Decimal = Decimal("30"),
        max_reconnects: int = 5,
        reconnect_backoff_seconds: Decimal = Decimal("1"),
        reconnect_sleeper: TradeStreamSleeper | None = None,
    ) -> None:
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca Paper credentials must not be empty")
        if not stale_after_seconds.is_finite() or stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive and finite")
        if max_reconnects < 0:
            raise ValueError("max_reconnects must be nonnegative")
        if not reconnect_backoff_seconds.is_finite() or reconnect_backoff_seconds < 0:
            raise ValueError("reconnect_backoff_seconds must be nonnegative and finite")
        self._api_key = api_key
        self._api_secret = api_secret
        self._connector = connector
        self._stale_after_seconds = stale_after_seconds
        self._max_reconnects = max_reconnects
        self._reconnect_backoff_seconds = reconnect_backoff_seconds
        self._reconnect_sleeper = reconnect_sleeper or _default_sleeper
        self._reconcile_after_reconnect = reconcile_after_reconnect
        self._record_incident = record_incident

    def __repr__(self) -> str:
        return (
            "AlpacaPaperTradeStream(environment='PAPER', "
            "api_key=SecretStr('**********'), api_secret=SecretStr('**********'))"
        )

    async def _receive(self, connection: TradeStreamConnection) -> str | bytes:
        try:
            return await asyncio.wait_for(
                connection.receive(),
                timeout=float(self._stale_after_seconds),
            )
        except TimeoutError as error:
            raise TradeStreamStaleError("Alpaca trade stream is stale") from error

    async def _authenticate(self, connection: TradeStreamConnection) -> None:
        await connection.send(
            json.dumps(
                {
                    "action": "auth",
                    "key": self._api_key.get_secret_value(),
                    "secret": self._api_secret.get_secret_value(),
                },
                separators=(",", ":"),
            )
        )
        _expect_authorized(await self._receive(connection))
        await connection.send(
            json.dumps(
                {"action": "listen", "data": {"streams": ["trade_updates"]}},
                separators=(",", ":"),
            )
        )
        _expect_listening(await self._receive(connection))

    async def updates(self) -> AsyncGenerator[TradeUpdate, None]:
        reconnects = 0
        while True:
            connection: TradeStreamConnection | None = None
            should_reconnect = False
            try:
                connection = await self._connector.open(self.url)
                await self._authenticate(connection)
                if reconnects:
                    await self._reconcile_after_reconnect(reconnects)
                while True:
                    frame = await self._receive(connection)
                    try:
                        update = _parse_trade_update(frame, reconnects)
                    except _UnknownTradeUpdateError as error:
                        await self._record_incident(error.incident)
                        raise
                    yield update
            except (TradeStreamDisconnected, TradeStreamStaleError):
                if reconnects >= self._max_reconnects:
                    raise
                reconnects += 1
                should_reconnect = True
            finally:
                if connection is not None:
                    await connection.close()
            if should_reconnect:
                delay = self._reconnect_backoff_seconds * Decimal(2 ** (reconnects - 1))
                await self._reconnect_sleeper(delay)
