from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal

import pytest
from pydantic import SecretStr

from quantbot.brokers import (
    AlpacaPaperTradeStream,
    TradeStreamDisconnected,
    TradeStreamIncident,
    TradeStreamProtocolError,
    TradeStreamStaleError,
    TradeUpdateEvent,
)

AUTHORIZED = json.dumps(
    {
        "stream": "authorization",
        "data": {"status": "authorized", "action": "authenticate"},
    }
)
LISTENING = json.dumps({"stream": "listening", "data": {"streams": ["trade_updates"]}})
HANG = object()


def order_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "broker-order-1",
        "client_order_id": "qb-client-1",
        "symbol": "SPY",
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
        "qty": "10",
        "filled_qty": "4",
        "status": "partially_filled",
        "submitted_at": "2026-08-31T19:59:00Z",
        "filled_avg_price": "100.0000000000000001",
    }
    payload.update(updates)
    return payload


def trade_update(
    event: str = "partial_fill",
    *,
    execution_id: str = "execution-1",
    order: Mapping[str, object] | None = None,
    execution_quantity: str = "4",
    position_quantity: str = "4",
) -> dict[str, object]:
    data: dict[str, object] = {
        "event": event,
        "order": dict(order or order_payload()),
        "timestamp": "2026-08-31T20:00:01Z",
    }
    if event in {"fill", "partial_fill"}:
        data.update(
            {
                "execution_id": execution_id,
                "price": "100.0000000000000001",
                "qty": execution_quantity,
                "position_qty": position_quantity,
            }
        )
    return {"stream": "trade_updates", "data": data}


class ScriptedSocket:
    def __init__(self, incoming: list[str | bytes | Exception | object]) -> None:
        self.incoming = list(incoming)
        self.sent: list[Mapping[str, object]] = []
        self.closed = False

    async def receive(self) -> str | bytes:
        item = self.incoming.pop(0)
        if item is HANG:
            await asyncio.sleep(1)
            raise AssertionError("stale receive should be cancelled")
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, (str, bytes))
        return item

    async def send(self, message: str) -> None:
        decoded = json.loads(message)
        assert isinstance(decoded, dict)
        self.sent.append(decoded)

    async def close(self) -> None:
        self.closed = True


class ScriptedConnector:
    def __init__(self, sockets: list[ScriptedSocket | Exception]) -> None:
        self.sockets = list(sockets)
        self.urls: list[str] = []

    async def open(self, url: str) -> ScriptedSocket:
        self.urls.append(url)
        item = self.sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class ReconnectRecorder:
    def __init__(self) -> None:
        self.sleeps: list[Decimal] = []
        self.reconciliations: list[int] = []

    async def sleep(self, delay: Decimal) -> None:
        self.sleeps.append(delay)

    async def reconcile(self, connection_generation: int) -> None:
        self.reconciliations.append(connection_generation)


class IncidentRecorder:
    def __init__(self) -> None:
        self.incidents: list[TradeStreamIncident] = []

    async def record(self, incident: TradeStreamIncident) -> None:
        self.incidents.append(incident)


def stream(
    connector: ScriptedConnector,
    *,
    stale_after_seconds: Decimal = Decimal("1"),
    max_reconnects: int = 1,
    recorder: ReconnectRecorder | None = None,
    incident_recorder: IncidentRecorder | None = None,
) -> AlpacaPaperTradeStream:
    effective_recorder = recorder or ReconnectRecorder()
    effective_incident_recorder = incident_recorder or IncidentRecorder()
    return AlpacaPaperTradeStream(
        api_key=SecretStr("paper-key"),
        api_secret=SecretStr("paper-secret"),
        connector=connector,
        reconcile_after_reconnect=effective_recorder.reconcile,
        record_incident=effective_incident_recorder.record,
        stale_after_seconds=stale_after_seconds,
        max_reconnects=max_reconnects,
        reconnect_backoff_seconds=Decimal("0.25"),
        reconnect_sleeper=effective_recorder.sleep,
    )


@pytest.mark.asyncio
async def test_trade_stream_authenticates_listens_and_parses_binary_partial_fill() -> None:
    frame = json.dumps(trade_update()).encode("utf-8")
    socket = ScriptedSocket([AUTHORIZED, LISTENING, frame])
    connector = ScriptedConnector([socket])
    generator = stream(connector).updates()

    update = await anext(generator)
    await generator.aclose()

    assert connector.urls == ["wss://paper-api.alpaca.markets/stream"]
    assert socket.sent == [
        {"action": "auth", "key": "paper-key", "secret": "paper-secret"},
        {"action": "listen", "data": {"streams": ["trade_updates"]}},
    ]
    assert update.event is TradeUpdateEvent.PARTIAL_FILL
    assert update.event_id == "execution-1"
    assert update.order.client_order_id == "qb-client-1"
    assert update.order.filled_quantity == Decimal("4")
    assert update.fill is not None
    assert update.fill.fill_id == "execution-1"
    assert update.fill.quantity == Decimal("4")
    assert update.fill.price == Decimal("100.0000000000000001")
    assert update.position_quantity == Decimal("4")
    assert update.connection_generation == 0
    assert socket.closed is True


@pytest.mark.asyncio
async def test_execution_quantity_is_not_confused_with_cumulative_filled_quantity() -> None:
    payload = trade_update(
        event="fill",
        order=order_payload(status="filled", filled_qty="10"),
        execution_quantity="6",
        position_quantity="10",
    )
    generator = stream(
        ScriptedConnector([ScriptedSocket([AUTHORIZED, LISTENING, json.dumps(payload)])])
    ).updates()

    update = await anext(generator)
    await generator.aclose()

    assert update.order.filled_quantity == Decimal("10")
    assert update.fill is not None
    assert update.fill.quantity == Decimal("6")
    assert update.position_quantity == Decimal("10")


@pytest.mark.parametrize("event", list(TradeUpdateEvent))
@pytest.mark.asyncio
async def test_every_documented_trade_update_event_is_accepted(event: TradeUpdateEvent) -> None:
    socket = ScriptedSocket([AUTHORIZED, LISTENING, json.dumps(trade_update(event=event.value))])
    generator = stream(ScriptedConnector([socket])).updates()

    update = await anext(generator)
    await generator.aclose()

    assert update.event is event


@pytest.mark.asyncio
async def test_disconnect_reconnects_resubscribes_reconciles_and_marks_generation() -> None:
    first = ScriptedSocket([AUTHORIZED, LISTENING, TradeStreamDisconnected("closed")])
    second = ScriptedSocket(
        [AUTHORIZED, LISTENING, json.dumps(trade_update(event="new", order=order_payload()))]
    )
    connector = ScriptedConnector([first, second])
    recorder = ReconnectRecorder()
    generator = stream(connector, recorder=recorder).updates()

    update = await anext(generator)
    await generator.aclose()

    assert update.event is TradeUpdateEvent.NEW
    assert update.fill is None
    assert update.connection_generation == 1
    assert len(connector.urls) == 2
    assert recorder.sleeps == [Decimal("0.25")]
    assert recorder.reconciliations == [1]
    assert first.sent[-1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
    assert second.sent[-1] == {"action": "listen", "data": {"streams": ["trade_updates"]}}
    assert first.closed is True
    assert second.closed is True


@pytest.mark.asyncio
async def test_connection_open_failure_reconnects_within_bound() -> None:
    socket = ScriptedSocket([AUTHORIZED, LISTENING, json.dumps(trade_update(event="accepted"))])
    connector = ScriptedConnector([TradeStreamDisconnected("open failed"), socket])
    recorder = ReconnectRecorder()
    generator = stream(connector, recorder=recorder).updates()

    update = await anext(generator)
    await generator.aclose()

    assert update.event is TradeUpdateEvent.ACCEPTED
    assert update.connection_generation == 1
    assert recorder.sleeps == [Decimal("0.25")]
    assert recorder.reconciliations == [1]
    assert socket.closed is True


@pytest.mark.asyncio
async def test_stale_stream_fails_closed_after_bounded_reconnects() -> None:
    socket = ScriptedSocket([AUTHORIZED, LISTENING, HANG])
    connector = ScriptedConnector([socket])
    generator = stream(
        connector,
        stale_after_seconds=Decimal("0.001"),
        max_reconnects=0,
    ).updates()

    with pytest.raises(TradeStreamStaleError, match="stale"):
        await anext(generator)

    assert socket.closed is True


@pytest.mark.asyncio
async def test_unauthorized_stream_is_not_retried_or_secret_bearing() -> None:
    unauthorized = json.dumps(
        {
            "stream": "authorization",
            "data": {"status": "unauthorized", "action": "authenticate"},
        }
    )
    socket = ScriptedSocket([unauthorized])
    connector = ScriptedConnector([socket])
    generator = stream(connector).updates()

    with pytest.raises(TradeStreamProtocolError, match="authorization") as captured:
        await anext(generator)

    assert len(connector.urls) == 1
    assert "paper-key" not in str(captured.value)
    assert "paper-secret" not in str(captured.value)
    assert socket.closed is True


@pytest.mark.asyncio
async def test_unknown_event_is_recorded_as_an_incident_before_failing_closed() -> None:
    socket = ScriptedSocket(
        [AUTHORIZED, LISTENING, json.dumps(trade_update(event="future_status"))]
    )
    recorder = IncidentRecorder()
    generator = stream(
        ScriptedConnector([socket]),
        incident_recorder=recorder,
    ).updates()

    with pytest.raises(TradeStreamProtocolError, match="unknown trade update"):
        await anext(generator)

    assert len(recorder.incidents) == 1
    incident = recorder.incidents[0]
    assert incident.kind == "UNKNOWN_BROKER_ORDER_EVENT"
    assert incident.detail == {
        "broker_order_id": "broker-order-1",
        "event": "future_status",
    }
    assert incident.occurred_at.isoformat() == "2026-08-31T20:00:01+00:00"
    assert socket.closed is True


@pytest.mark.parametrize(
    "frame",
    [
        b"not-json",
        b"\xff",
        json.dumps({"stream": "other", "data": {}}),
        json.dumps(trade_update(event="unknown_event")),
        json.dumps(
            trade_update(
                event="fill",
                order=order_payload(status="filled", filled_qty="10"),
            )
        ).replace('"execution_id": "execution-1",', ""),
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_unknown_trade_update_fails_closed(frame: str | bytes) -> None:
    socket = ScriptedSocket([AUTHORIZED, LISTENING, frame])
    generator = stream(ScriptedConnector([socket])).updates()

    with pytest.raises(TradeStreamProtocolError):
        await anext(generator)

    assert socket.closed is True


def test_trade_stream_is_irreversibly_paper_only_and_hides_secrets() -> None:
    instance = stream(ScriptedConnector([]))

    assert instance.environment == "PAPER"
    assert instance.url == "wss://paper-api.alpaca.markets/stream"
    assert "paper-key" not in repr(instance)
    assert "paper-secret" not in repr(instance)


def test_trade_stream_requires_reconciliation_and_incident_callbacks() -> None:
    with pytest.raises(TypeError, match="reconcile_after_reconnect") as captured:
        AlpacaPaperTradeStream(  # type: ignore[call-arg]
            api_key=SecretStr("paper-key"),
            api_secret=SecretStr("paper-secret"),
            connector=ScriptedConnector([]),
        )
    assert "record_incident" in str(captured.value)
