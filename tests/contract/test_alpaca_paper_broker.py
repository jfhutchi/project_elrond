from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import SecretStr

from quantbot.brokers import (
    AlpacaPaperBrokerAdapter,
    BrokerProtocolError,
    BrokerRequestError,
    BrokerTransportError,
    BrokerTransportResponse,
    SubmissionUncertainError,
)
from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce

NOW = datetime(2026, 8, 31, 20, tzinfo=UTC)


def response(
    status: int,
    payload: object | None = None,
    *,
    headers: Mapping[str, str] | None = None,
) -> BrokerTransportResponse:
    return BrokerTransportResponse(
        status_code=status,
        headers=dict(headers or {}),
        content=b"" if payload is None else json.dumps(payload).encode("ascii"),
    )


class ScriptedBrokerTransport:
    def __init__(self, scripted: list[BrokerTransportResponse | Exception]) -> None:
        self.scripted = list(scripted)
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        json_body: Mapping[str, object] | None,
        timeout_seconds: Decimal,
    ) -> BrokerTransportResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "params": dict(params),
                "json_body": None if json_body is None else dict(json_body),
                "timeout_seconds": timeout_seconds,
            }
        )
        item = self.scripted.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def account_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "paper-account-id",
        "account_number": "PA123456",
        "status": "ACTIVE",
        "currency": "USD",
        "cash": "100000.12",
        "buying_power": "200000.24",
        "equity": "101234.56",
        "trading_blocked": False,
        "transfers_blocked": False,
        "account_blocked": False,
        "trade_suspended_by_user": False,
    }
    payload.update(updates)
    return payload


def intent(
    *,
    order_type: OrderType = OrderType.MARKET,
    limit_price: str | None = None,
    stop_price: str | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id="intent-1",
        client_order_id="qb-client-1",
        strategy_id="full-v1:hash",
        symbol="SPY",
        signal_date=date(2026, 8, 31),
        side=OrderSide.BUY,
        order_type=order_type,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("2"),
        limit_price=limit_price,
        stop_price=stop_price,
        extended_hours=False,
        created_at=NOW,
    )


def order_payload(order: OrderIntent, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "broker-order-id",
        "client_order_id": order.client_order_id,
        "symbol": order.symbol,
        "side": order.side.value.lower(),
        "type": order.order_type.value.lower(),
        "time_in_force": order.time_in_force.value.lower(),
        "qty": str(order.quantity),
        "filled_qty": "0",
        "status": "accepted",
        "submitted_at": "2026-08-31T20:00:00Z",
        "filled_avg_price": None,
        "limit_price": None if order.limit_price is None else str(order.limit_price),
        "stop_price": None if order.stop_price is None else str(order.stop_price),
        "extended_hours": order.extended_hours,
    }
    payload.update(updates)
    return payload


def adapter(
    transport: ScriptedBrokerTransport,
    *,
    sleeper: Any | None = None,
    max_retries: int = 2,
    activity_page_size: int = 100,
    order_page_size: int = 500,
) -> AlpacaPaperBrokerAdapter:
    return AlpacaPaperBrokerAdapter(
        api_key=SecretStr("paper-key"),
        api_secret=SecretStr("paper-secret"),
        transport=transport,
        sleeper=sleeper,
        max_retries=max_retries,
        activity_page_size=activity_page_size,
        order_page_size=order_page_size,
    )


@pytest.mark.asyncio
async def test_account_health_uses_paper_host_auth_and_exact_decimals() -> None:
    transport = ScriptedBrokerTransport([response(200, account_payload())])
    broker = adapter(transport)

    snapshot = await broker.get_account()

    assert snapshot.account.account_id == "paper-account-id"
    assert snapshot.account_number == "PA123456"
    assert snapshot.account.cash == Decimal("100000.12")
    assert snapshot.account.buying_power == Decimal("200000.24")
    assert snapshot.account.equity == Decimal("101234.56")
    assert snapshot.account.currency == "USD"
    assert snapshot.status == "ACTIVE"
    assert snapshot.order_entry_allowed is True
    assert transport.calls == [
        {
            "method": "GET",
            "url": "https://paper-api.alpaca.markets/v2/account",
            "headers": {
                "APCA-API-KEY-ID": "paper-key",
                "APCA-API-SECRET-KEY": "paper-secret",
                "Accept": "application/json",
            },
            "params": {},
            "json_body": None,
            "timeout_seconds": Decimal("10"),
        }
    ]
    assert "paper-key" not in repr(broker)
    assert "paper-secret" not in repr(broker)


@pytest.mark.asyncio
async def test_health_check_fails_closed_for_broker_restrictions() -> None:
    transport = ScriptedBrokerTransport(
        [response(200, account_payload(trading_blocked=True, status="ACCOUNT_UPDATED"))]
    )

    health = await adapter(transport).health_check()

    assert health.healthy is False
    assert health.provider == "alpaca"
    assert health.environment == "PAPER"
    assert health.account_id == "paper-account-id"
    assert health.reasons == ("ACCOUNT_STATUS_ACCOUNT_UPDATED", "TRADING_BLOCKED")


@pytest.mark.asyncio
async def test_connect_and_balance_accessors_use_authoritative_account() -> None:
    transport = ScriptedBrokerTransport(
        [
            response(200, account_payload()),
            response(200, account_payload()),
            response(200, account_payload()),
        ]
    )
    broker = adapter(transport)

    assert (await broker.connect()).healthy is True
    assert await broker.get_cash() == Decimal("100000.12")
    assert await broker.get_buying_power() == Decimal("200000.24")


@pytest.mark.asyncio
async def test_positions_are_broker_authoritative_and_short_sign_is_preserved() -> None:
    transport = ScriptedBrokerTransport(
        [
            response(
                200,
                [
                    {
                        "symbol": "SPY",
                        "qty": "2",
                        "side": "long",
                        "current_price": "650.25",
                        "market_value": "1300.50",
                        "avg_entry_price": "625.00",
                    },
                    {
                        "symbol": "QQQ",
                        "qty": "1.5",
                        "side": "short",
                        "current_price": "570.00",
                        "market_value": "-855.00",
                        "avg_entry_price": "560.00",
                    },
                ],
            ),
            response(404, {"message": "position does not exist"}),
        ]
    )
    broker = adapter(transport)

    positions = await broker.get_positions()
    missing = await broker.get_position("IWM")

    assert tuple((item.symbol, item.quantity) for item in positions) == (
        ("QQQ", Decimal("-1.5")),
        ("SPY", Decimal("2")),
    )
    assert missing is None
    assert transport.calls[1]["url"] == "https://paper-api.alpaca.markets/v2/positions/IWM"


@pytest.mark.asyncio
async def test_clock_uses_broker_calendar_values_without_local_assumptions() -> None:
    transport = ScriptedBrokerTransport(
        [
            response(
                200,
                {
                    "timestamp": "2026-11-27T12:00:00-05:00",
                    "is_open": False,
                    "next_open": "2026-11-27T09:30:00-05:00",
                    "next_close": "2026-11-27T13:00:00-05:00",
                },
            )
        ]
    )

    clock = await adapter(transport).get_market_clock()

    assert clock.timestamp == datetime(2026, 11, 27, 17, tzinfo=UTC)
    assert clock.next_open == datetime(2026, 11, 27, 14, 30, tzinfo=UTC)
    assert clock.next_close == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert transport.calls[0]["url"] == "https://paper-api.alpaca.markets/v2/clock"


@pytest.mark.parametrize(
    ("order", "expected_body"),
    [
        (
            intent(),
            {
                "symbol": "SPY",
                "qty": "2",
                "side": "buy",
                "type": "market",
                "time_in_force": "day",
                "client_order_id": "qb-client-1",
                "extended_hours": False,
            },
        ),
        (
            intent(order_type=OrderType.LIMIT, limit_price="645.25"),
            {
                "symbol": "SPY",
                "qty": "2",
                "side": "buy",
                "type": "limit",
                "time_in_force": "day",
                "client_order_id": "qb-client-1",
                "extended_hours": False,
                "limit_price": "645.25",
            },
        ),
        (
            intent(order_type=OrderType.STOP, stop_price="640.25"),
            {
                "symbol": "SPY",
                "qty": "2",
                "side": "buy",
                "type": "stop",
                "time_in_force": "day",
                "client_order_id": "qb-client-1",
                "extended_hours": False,
                "stop_price": "640.25",
            },
        ),
        (
            intent(
                order_type=OrderType.STOP_LIMIT,
                limit_price="645.25",
                stop_price="640.25",
            ),
            {
                "symbol": "SPY",
                "qty": "2",
                "side": "buy",
                "type": "stop_limit",
                "time_in_force": "day",
                "client_order_id": "qb-client-1",
                "extended_hours": False,
                "limit_price": "645.25",
                "stop_price": "640.25",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_submit_order_maps_supported_types_without_silent_conversion(
    order: OrderIntent,
    expected_body: dict[str, object],
) -> None:
    transport = ScriptedBrokerTransport([response(200, order_payload(order))])

    accepted = await adapter(transport).submit_order(order)

    assert accepted.client_order_id == order.client_order_id
    assert accepted.order_type is order.order_type
    assert accepted.quantity == order.quantity
    assert accepted.filled_quantity == Decimal("0")
    assert accepted.status == "accepted"
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"] == "https://paper-api.alpaca.markets/v2/orders"
    assert transport.calls[0]["json_body"] == expected_body


@pytest.mark.asyncio
async def test_submit_response_must_match_original_intent_exactly() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [response(200, order_payload(order, symbol="QQQ", qty="3"))]
    )

    with pytest.raises(BrokerProtocolError, match="does not match"):
        await adapter(transport).submit_order(order)


@pytest.mark.asyncio
async def test_submission_timeout_recovers_by_client_id_without_duplicate_post() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [
            BrokerTransportError("timeout"),
            response(200, order_payload(order)),
        ]
    )

    recovered = await adapter(transport).submit_order(order)

    assert recovered.client_order_id == order.client_order_id
    assert tuple(call["method"] for call in transport.calls) == ("POST", "GET")
    assert str(transport.calls[1]["url"]).endswith("/v2/orders:by_client_order_id")
    assert transport.calls[1]["params"] == {"client_order_id": "qb-client-1"}


@pytest.mark.asyncio
async def test_submission_timeout_with_no_broker_order_is_explicitly_uncertain() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [BrokerTransportError("timeout"), response(404, {"message": "not found"})]
    )

    with pytest.raises(SubmissionUncertainError, match="not accepted") as captured:
        await adapter(transport).submit_order(order)

    assert len(transport.calls) == 2
    assert "paper-key" not in str(captured.value)
    assert "paper-secret" not in str(captured.value)


@pytest.mark.asyncio
async def test_submission_timeout_with_unavailable_lookup_is_explicitly_uncertain() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [BrokerTransportError("timeout"), BrokerTransportError("lookup timeout")]
    )

    with pytest.raises(SubmissionUncertainError, match="could not be determined"):
        await adapter(transport).submit_order(order)

    assert len(transport.calls) == 2


@pytest.mark.asyncio
async def test_submission_server_error_recovers_by_client_id_without_duplicate_post() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [response(503, {"message": "upstream failed"}), response(200, order_payload(order))]
    )

    recovered = await adapter(transport).submit_order(order)

    assert recovered.client_order_id == order.client_order_id
    assert tuple(call["method"] for call in transport.calls) == ("POST", "GET")


@pytest.mark.asyncio
async def test_malformed_submission_success_recovers_by_client_id() -> None:
    order = intent()
    transport = ScriptedBrokerTransport(
        [
            BrokerTransportResponse(status_code=200, headers={}, content=b"not-json"),
            response(200, order_payload(order)),
        ]
    )

    recovered = await adapter(transport).submit_order(order)

    assert recovered.client_order_id == order.client_order_id
    assert tuple(call["method"] for call in transport.calls) == ("POST", "GET")


@pytest.mark.asyncio
async def test_existing_client_order_is_checked_for_exact_intent_match() -> None:
    order = intent(order_type=OrderType.LIMIT, limit_price="645.25")
    transport = ScriptedBrokerTransport([response(200, order_payload(order, limit_price="644.00"))])

    with pytest.raises(BrokerProtocolError, match="does not match"):
        await adapter(transport).get_order_by_client_id(order.client_order_id, expected=order)


@pytest.mark.asyncio
async def test_extended_hours_order_is_rejected_before_transport() -> None:
    order = intent().model_copy(update={"extended_hours": True})
    transport = ScriptedBrokerTransport([])

    with pytest.raises(ValueError, match="extended-hours"):
        await adapter(transport).submit_order(order)

    assert transport.calls == []


@pytest.mark.asyncio
async def test_reads_retry_rate_limits_and_server_errors_but_not_client_errors() -> None:
    sleeps: list[Decimal] = []

    async def sleeper(delay: Decimal) -> None:
        sleeps.append(delay)

    transport = ScriptedBrokerTransport(
        [
            response(429, {"message": "slow"}, headers={"Retry-After": "2.5"}),
            response(503, {"message": "down"}),
            response(200, account_payload()),
            response(401, {"message": "bad credentials"}),
        ]
    )
    broker = adapter(transport, sleeper=sleeper, max_retries=2)

    await broker.get_account()
    with pytest.raises(BrokerRequestError, match="401"):
        await broker.get_account()

    assert sleeps == [Decimal("2.5"), Decimal("2")]
    assert len(transport.calls) == 4


@pytest.mark.asyncio
async def test_orders_fills_and_pagination_are_broker_authoritative() -> None:
    order = intent()
    open_payload = order_payload(order, status="partially_filled", filled_qty="1")
    transport = ScriptedBrokerTransport(
        [
            response(200, [open_payload]),
            response(200, open_payload),
            response(
                200,
                [
                    {
                        "activity_type": "FILL",
                        "id": "fill-1",
                        "order_id": "broker-order-id",
                        "symbol": "SPY",
                        "side": "buy",
                        "qty": "1",
                        "price": "650.25",
                        "transaction_time": "2026-08-31T20:00:01Z",
                    },
                    {
                        "activity_type": "FILL",
                        "id": "fill-2",
                        "order_id": "broker-order-id",
                        "symbol": "SPY",
                        "side": "buy",
                        "qty": "1",
                        "price": "650.50",
                        "transaction_time": "2026-08-31T20:00:02Z",
                    },
                ],
            ),
            response(200, []),
        ]
    )
    broker = adapter(transport, activity_page_size=2)

    orders = await broker.get_open_orders()
    restored = await broker.get_order("broker-order-id")
    fills = await broker.get_fills(after=datetime(2026, 8, 31, 19, tzinfo=UTC))

    assert orders[0].filled_quantity == Decimal("1")
    assert restored == orders[0]
    assert tuple((fill.fill_id, fill.quantity, fill.price) for fill in fills) == (
        ("fill-1", Decimal("1"), Decimal("650.25")),
        ("fill-2", Decimal("1"), Decimal("650.50")),
    )
    assert transport.calls[0]["params"] == {
        "status": "open",
        "direction": "asc",
        "limit": "500",
    }
    assert transport.calls[2]["params"] == {
        "activity_types": "FILL",
        "direction": "asc",
        "page_size": "2",
        "after": "2026-08-31T19:00:00Z",
    }
    second_page_params = transport.calls[2]["params"]
    assert isinstance(second_page_params, dict)
    assert transport.calls[3]["params"] == {
        **second_page_params,
        "page_token": "fill-2",
    }


@pytest.mark.asyncio
async def test_open_orders_paginate_without_duplicates_or_omissions() -> None:
    first_order = intent()
    second_order = intent().model_copy(update={"client_order_id": "qb-client-2"})
    third_order = intent().model_copy(update={"client_order_id": "qb-client-3"})
    transport = ScriptedBrokerTransport(
        [
            response(
                200,
                [
                    order_payload(first_order, id="order-1"),
                    order_payload(second_order, id="order-2"),
                ],
            ),
            response(200, [order_payload(third_order, id="order-3")]),
        ]
    )

    orders = await adapter(transport, order_page_size=2).get_open_orders()

    assert tuple(order.broker_order_id for order in orders) == (
        "order-1",
        "order-2",
        "order-3",
    )
    assert transport.calls[1]["params"] == {
        "status": "open",
        "direction": "asc",
        "limit": "2",
        "after_order_id": "order-2",
    }


@pytest.mark.asyncio
async def test_duplicate_fill_across_pages_fails_closed() -> None:
    fill = {
        "activity_type": "FILL",
        "id": "fill-1",
        "order_id": "broker-order-id",
        "symbol": "SPY",
        "side": "buy",
        "qty": "1",
        "price": "650.25",
        "transaction_time": "2026-08-31T20:00:01Z",
    }
    transport = ScriptedBrokerTransport([response(200, [fill]), response(200, [fill])])

    with pytest.raises(BrokerProtocolError, match="duplicate fill"):
        await adapter(transport, activity_page_size=1).get_fills()


@pytest.mark.asyncio
async def test_cancel_order_and_cancel_all_require_broker_confirmation() -> None:
    transport = ScriptedBrokerTransport(
        [
            response(204),
            response(
                207,
                [
                    {"id": "order-1", "status": 204},
                    {"id": "order-2", "status": 204},
                ],
            ),
        ]
    )
    broker = adapter(transport)

    assert await broker.cancel_order("broker-order-id") is True
    cancelled = await broker.cancel_all_orders()

    assert cancelled == ("order-1", "order-2")
    assert transport.calls[0]["method"] == "DELETE"
    assert str(transport.calls[0]["url"]).endswith("/v2/orders/broker-order-id")
    assert str(transport.calls[1]["url"]).endswith("/v2/orders")


@pytest.mark.asyncio
async def test_cancel_all_partial_failure_fails_explicitly() -> None:
    transport = ScriptedBrokerTransport(
        [response(207, [{"id": "order-1", "status": 204}, {"id": "order-2", "status": 422}])]
    )

    with pytest.raises(BrokerRequestError, match="order-2"):
        await adapter(transport).cancel_all_orders()


@pytest.mark.asyncio
async def test_non_json_and_non_finite_numeric_responses_fail_protocol_validation() -> None:
    malformed = ScriptedBrokerTransport(
        [BrokerTransportResponse(status_code=200, headers={}, content=b"not-json")]
    )
    with pytest.raises(BrokerProtocolError, match="JSON"):
        await adapter(malformed).get_account()

    nonfinite = ScriptedBrokerTransport([response(200, account_payload(equity="NaN"))])
    with pytest.raises(BrokerProtocolError, match="equity"):
        await adapter(nonfinite).get_account()


def test_broker_adapter_is_irreversibly_paper_only() -> None:
    broker = adapter(ScriptedBrokerTransport([]))

    assert broker.provider == "alpaca"
    assert broker.environment == "PAPER"
    assert broker.base_url == "https://paper-api.alpaca.markets"


@pytest.mark.asyncio
async def test_get_position_validates_symbol_before_building_url() -> None:
    broker = adapter(ScriptedBrokerTransport([]))
    with pytest.raises(ValueError, match="uppercase"):
        await broker.get_position("../account")
