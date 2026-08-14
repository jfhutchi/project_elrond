"""Alpaca Paper Trading REST adapter."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import cast
from urllib.parse import quote

from pydantic import SecretStr, ValidationError

from quantbot.brokers.base import (
    BrokerAccountSnapshot,
    BrokerHealth,
    BrokerProtocolError,
    BrokerRequestError,
    BrokerSleeper,
    BrokerTransport,
    BrokerTransportError,
    BrokerTransportResponse,
    SubmissionUncertainError,
)
from quantbot.domain import (
    Account,
    BrokerOrder,
    Fill,
    MarketClock,
    OrderIntent,
    OrderSide,
    OrderType,
    Position,
    TimeInForce,
)

ALPACA_PAPER_BASE_URL = "https://paper-api.alpaca.markets"


async def _default_sleeper(delay: Decimal) -> None:
    await asyncio.sleep(float(delay))


def _decode_json(response: BrokerTransportResponse) -> object:
    try:
        return json.loads(
            response.content.decode("utf-8"),
            parse_float=Decimal,
            parse_int=Decimal,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrokerProtocolError("broker response is not valid JSON") from error


def _object_payload(response: BrokerTransportResponse) -> Mapping[str, object]:
    loaded = _decode_json(response)
    if not isinstance(loaded, dict):
        raise BrokerProtocolError("broker response must be a JSON object")
    return cast(Mapping[str, object], loaded)


def _array_payload(response: BrokerTransportResponse) -> list[object]:
    loaded = _decode_json(response)
    if not isinstance(loaded, list):
        raise BrokerProtocolError("broker response must be a JSON array")
    return cast(list[object], loaded)


def _text(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise BrokerProtocolError(f"broker {field} must be nonempty text")
    return value


def _decimal(payload: Mapping[str, object], field: str) -> Decimal:
    value = payload.get(field)
    if value is None or isinstance(value, bool):
        raise BrokerProtocolError(f"broker {field} must be numeric")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise BrokerProtocolError(f"broker {field} must be numeric") from error
    if not parsed.is_finite():
        raise BrokerProtocolError(f"broker {field} must be finite")
    return parsed


def _optional_decimal(payload: Mapping[str, object], field: str) -> Decimal | None:
    if payload.get(field) is None:
        return None
    return _decimal(payload, field)


def _boolean(payload: Mapping[str, object], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise BrokerProtocolError(f"broker {field} must be boolean")
    return value


def _timestamp(payload: Mapping[str, object], field: str) -> datetime:
    value = payload.get(field)
    if not isinstance(value, str):
        raise BrokerProtocolError(f"broker {field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BrokerProtocolError(f"broker {field} must be a valid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BrokerProtocolError(f"broker {field} must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc_parameter(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical_decimal(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("decimal must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


class AlpacaPaperBrokerAdapter:
    """Broker-neutral adapter permanently bound to Alpaca's paper endpoint."""

    provider = "alpaca"
    environment = "PAPER"
    base_url = ALPACA_PAPER_BASE_URL

    def __init__(
        self,
        *,
        api_key: SecretStr,
        api_secret: SecretStr,
        transport: BrokerTransport,
        sleeper: BrokerSleeper | None = None,
        max_retries: int = 2,
        timeout_seconds: Decimal = Decimal("10"),
        activity_page_size: int = 100,
        order_page_size: int = 500,
    ) -> None:
        if not api_key.get_secret_value() or not api_secret.get_secret_value():
            raise ValueError("Alpaca Paper credentials must not be empty")
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        if not 1 <= activity_page_size <= 100:
            raise ValueError("activity_page_size must be between 1 and 100")
        if not 1 <= order_page_size <= 500:
            raise ValueError("order_page_size must be between 1 and 500")
        self._api_key = api_key
        self._api_secret = api_secret
        self._transport = transport
        self._sleeper = sleeper or _default_sleeper
        self._max_retries = max_retries
        self._timeout_seconds = timeout_seconds
        self._activity_page_size = activity_page_size
        self._order_page_size = order_page_size

    def __repr__(self) -> str:
        return (
            "AlpacaPaperBrokerAdapter(environment='PAPER', "
            "api_key=SecretStr('**********'), api_secret=SecretStr('**********'))"
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
        raw = next(
            (value for key, value in headers.items() if key.lower() == "retry-after"),
            "1",
        )
        try:
            delay = Decimal(raw)
        except InvalidOperation as error:
            raise BrokerProtocolError("Retry-After header is invalid") from error
        if not delay.is_finite() or delay < 0:
            raise BrokerProtocolError("Retry-After header is invalid")
        return delay

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Mapping[str, object] | None = None,
        expected_statuses: frozenset[int] = frozenset({200}),
        retry_safe: bool = True,
    ) -> BrokerTransportResponse:
        attempt = 0
        while True:
            try:
                response = await self._transport.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=self._headers,
                    params=params or {},
                    json_body=json_body,
                    timeout_seconds=self._timeout_seconds,
                )
            except BrokerTransportError:
                if not retry_safe or attempt >= self._max_retries:
                    raise
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue

            if response.status_code == 429 and retry_safe and attempt < self._max_retries:
                await self._sleeper(self._retry_after(response.headers))
                attempt += 1
                continue
            if 500 <= response.status_code < 600 and retry_safe and attempt < self._max_retries:
                await self._sleeper(Decimal(2) ** attempt)
                attempt += 1
                continue
            if response.status_code not in expected_statuses:
                raise BrokerRequestError(
                    f"Alpaca Paper request failed with HTTP {response.status_code}",
                    status_code=response.status_code,
                )
            return response

    @staticmethod
    def _account_from_payload(payload: Mapping[str, object]) -> BrokerAccountSnapshot:
        account_id = _text(payload, "id")
        account_number = _text(payload, "account_number")
        status = _text(payload, "status")
        restrictions: list[str] = []
        if status != "ACTIVE":
            restrictions.append(f"ACCOUNT_STATUS_{status}")
        for field, reason in (
            ("trading_blocked", "TRADING_BLOCKED"),
            ("transfers_blocked", "TRANSFERS_BLOCKED"),
            ("account_blocked", "ACCOUNT_BLOCKED"),
            ("trade_suspended_by_user", "TRADE_SUSPENDED_BY_USER"),
        ):
            if _boolean(payload, field):
                restrictions.append(reason)
        try:
            account = Account(
                account_id=account_id,
                cash=_decimal(payload, "cash"),
                buying_power=_decimal(payload, "buying_power"),
                equity=_decimal(payload, "equity"),
                currency=_text(payload, "currency"),
            )
        except ValidationError as error:
            raise BrokerProtocolError("broker account failed domain validation") from error
        return BrokerAccountSnapshot(
            account=account,
            account_number=account_number,
            status=status,
            order_entry_allowed=not restrictions,
            restrictions=tuple(restrictions),
        )

    async def get_account(self) -> BrokerAccountSnapshot:
        response = await self._request("GET", "/v2/account")
        return self._account_from_payload(_object_payload(response))

    async def connect(self) -> BrokerHealth:
        return await self.health_check()

    async def get_cash(self) -> Decimal:
        return (await self.get_account()).account.cash

    async def get_buying_power(self) -> Decimal:
        return (await self.get_account()).account.buying_power

    async def health_check(self) -> BrokerHealth:
        try:
            snapshot = await self.get_account()
        except (BrokerRequestError, BrokerTransportError, BrokerProtocolError):
            return BrokerHealth(
                healthy=False,
                provider=self.provider,
                environment=self.environment,
                account_id=None,
                reasons=("BROKER_UNAVAILABLE",),
            )
        return BrokerHealth(
            healthy=snapshot.order_entry_allowed,
            provider=self.provider,
            environment=self.environment,
            account_id=snapshot.account.account_id,
            reasons=snapshot.restrictions,
        )

    @staticmethod
    def _position_from_payload(payload: Mapping[str, object]) -> Position:
        side = _text(payload, "side")
        if side not in {"long", "short"}:
            raise BrokerProtocolError("broker position side is unsupported")
        unsigned_quantity = _decimal(payload, "qty")
        quantity = unsigned_quantity if side == "long" else -unsigned_quantity
        market_value = _decimal(payload, "market_value")
        if quantity == 0:
            raise BrokerProtocolError("broker open position quantity must be nonzero")
        if quantity > 0 and market_value < 0 or quantity < 0 and market_value > 0:
            raise BrokerProtocolError("broker position sign is inconsistent")
        try:
            return Position(
                symbol=_text(payload, "symbol"),
                quantity=quantity,
                market_price=_decimal(payload, "current_price"),
                market_value=market_value,
                average_entry_price=_decimal(payload, "avg_entry_price"),
            )
        except ValidationError as error:
            raise BrokerProtocolError("broker position failed domain validation") from error

    async def get_positions(self) -> tuple[Position, ...]:
        response = await self._request("GET", "/v2/positions")
        positions: list[Position] = []
        for item in _array_payload(response):
            if not isinstance(item, dict):
                raise BrokerProtocolError("broker position entry must be an object")
            positions.append(self._position_from_payload(cast(Mapping[str, object], item)))
        return tuple(sorted(positions, key=lambda position: position.symbol))

    @staticmethod
    def _validate_identifier(name: str, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized != value:
            raise ValueError(f"{name} must be nonempty without surrounding whitespace")
        if len(value) > 128:
            raise ValueError(f"{name} must not exceed 128 characters")
        allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.:"
        if any(character not in allowed for character in value):
            raise ValueError(f"{name} contains unsupported characters")
        return value

    @classmethod
    def _validate_symbol(cls, symbol: str) -> str:
        if symbol != symbol.upper():
            raise ValueError("symbol must be uppercase")
        cls._validate_identifier("symbol", symbol)
        return symbol

    async def get_position(self, symbol: str) -> Position | None:
        validated = self._validate_symbol(symbol)
        try:
            response = await self._request(
                "GET",
                f"/v2/positions/{quote(validated, safe='')}",
                expected_statuses=frozenset({200, 404}),
            )
        except BrokerRequestError:
            raise
        if response.status_code == 404:
            return None
        return self._position_from_payload(_object_payload(response))

    @staticmethod
    def _broker_order_from_payload(payload: Mapping[str, object]) -> BrokerOrder:
        try:
            return BrokerOrder(
                broker_order_id=_text(payload, "id"),
                client_order_id=_text(payload, "client_order_id"),
                symbol=_text(payload, "symbol"),
                side=OrderSide(_text(payload, "side").upper()),
                order_type=OrderType(_text(payload, "type").upper()),
                time_in_force=TimeInForce(_text(payload, "time_in_force").upper()),
                quantity=_decimal(payload, "qty"),
                filled_quantity=_decimal(payload, "filled_qty"),
                status=_text(payload, "status"),
                submitted_at=_timestamp(payload, "submitted_at"),
                filled_average_price=_optional_decimal(payload, "filled_avg_price"),
            )
        except (ValidationError, ValueError) as error:
            raise BrokerProtocolError("broker order failed domain validation") from error

    async def get_open_orders(self) -> tuple[BrokerOrder, ...]:
        orders: list[BrokerOrder] = []
        seen_ids: set[str] = set()
        after_order_id: str | None = None
        while True:
            params = {
                "status": "open",
                "direction": "asc",
                "limit": str(self._order_page_size),
            }
            if after_order_id is not None:
                params["after_order_id"] = after_order_id
            response = await self._request("GET", "/v2/orders", params=params)
            page = _array_payload(response)
            for item in page:
                if not isinstance(item, dict):
                    raise BrokerProtocolError("broker order entry must be an object")
                order = self._broker_order_from_payload(cast(Mapping[str, object], item))
                if order.broker_order_id in seen_ids:
                    raise BrokerProtocolError(
                        f"duplicate broker order across pages: {order.broker_order_id}"
                    )
                seen_ids.add(order.broker_order_id)
                orders.append(order)
            if len(page) < self._order_page_size:
                break
            if not orders:
                raise BrokerProtocolError("broker returned a full order page without orders")
            next_id = orders[-1].broker_order_id
            if next_id == after_order_id:
                raise BrokerProtocolError("broker order pagination token cycle detected")
            after_order_id = next_id
        return tuple(orders)

    async def get_order(self, order_id: str) -> BrokerOrder:
        validated = self._validate_identifier("order_id", order_id)
        response = await self._request(
            "GET",
            f"/v2/orders/{quote(validated, safe='')}",
        )
        return self._broker_order_from_payload(_object_payload(response))

    @staticmethod
    def _order_body(order: OrderIntent) -> dict[str, object]:
        body: dict[str, object] = {
            "symbol": order.symbol,
            "qty": _canonical_decimal(order.quantity),
            "side": order.side.value.lower(),
            "type": order.order_type.value.lower(),
            "time_in_force": order.time_in_force.value.lower(),
            "client_order_id": order.client_order_id,
            "extended_hours": order.extended_hours,
        }
        if order.limit_price is not None:
            body["limit_price"] = _canonical_decimal(order.limit_price)
        if order.stop_price is not None:
            body["stop_price"] = _canonical_decimal(order.stop_price)
        return body

    @staticmethod
    def _assert_order_matches_intent(
        order: BrokerOrder,
        payload: Mapping[str, object],
        expected: OrderIntent,
    ) -> None:
        matches = (
            order.client_order_id == expected.client_order_id
            and order.symbol == expected.symbol
            and order.side is expected.side
            and order.order_type is expected.order_type
            and order.time_in_force is expected.time_in_force
            and order.quantity == expected.quantity
            and _optional_decimal(payload, "limit_price") == expected.limit_price
            and _optional_decimal(payload, "stop_price") == expected.stop_price
            and _boolean(payload, "extended_hours") is expected.extended_hours
        )
        if not matches:
            raise BrokerProtocolError("broker order does not match original intent")

    async def get_order_by_client_id(
        self,
        client_order_id: str,
        *,
        expected: OrderIntent | None = None,
        retry_safe: bool = True,
    ) -> BrokerOrder | None:
        validated = self._validate_identifier("client_order_id", client_order_id)
        response = await self._request(
            "GET",
            "/v2/orders:by_client_order_id",
            params={"client_order_id": validated},
            expected_statuses=frozenset({200, 404}),
            retry_safe=retry_safe,
        )
        if response.status_code == 404:
            return None
        payload = _object_payload(response)
        order = self._broker_order_from_payload(payload)
        if expected is not None:
            self._assert_order_matches_intent(order, payload, expected)
        return order

    async def submit_order(self, order: OrderIntent) -> BrokerOrder:
        if order.extended_hours:
            raise ValueError("extended-hours orders are disabled in V1")
        try:
            response = await self._request(
                "POST",
                "/v2/orders",
                json_body=self._order_body(order),
                retry_safe=False,
            )
        except BrokerTransportError as error:
            return await self._recover_submission(order, error)
        except BrokerRequestError as error:
            if error.status_code is None or error.status_code < 500:
                raise
            return await self._recover_submission(order, error)
        try:
            payload = _object_payload(response)
            accepted = self._broker_order_from_payload(payload)
        except BrokerProtocolError as error:
            return await self._recover_submission(order, error)
        self._assert_order_matches_intent(accepted, payload, order)
        return accepted

    async def _recover_submission(
        self,
        order: OrderIntent,
        submission_error: Exception,
    ) -> BrokerOrder:
        """Resolve an ambiguous POST only by deterministic broker lookup."""
        try:
            recovered = await self.get_order_by_client_id(
                order.client_order_id,
                expected=order,
                retry_safe=False,
            )
        except (BrokerTransportError, BrokerRequestError) as lookup_error:
            raise SubmissionUncertainError(
                "broker submission outcome could not be determined"
            ) from lookup_error
        if recovered is None:
            raise SubmissionUncertainError(
                "broker reported the original submission was not accepted; automatic retry disabled"
            ) from submission_error
        return recovered

    async def cancel_order(self, order_id: str) -> bool:
        validated = self._validate_identifier("order_id", order_id)
        response = await self._request(
            "DELETE",
            f"/v2/orders/{quote(validated, safe='')}",
            expected_statuses=frozenset({204}),
            retry_safe=False,
        )
        return response.status_code == 204

    async def cancel_all_orders(self) -> tuple[str, ...]:
        response = await self._request(
            "DELETE",
            "/v2/orders",
            expected_statuses=frozenset({207}),
            retry_safe=False,
        )
        cancelled: list[str] = []
        failures: list[str] = []
        for item in _array_payload(response):
            if not isinstance(item, dict):
                raise BrokerProtocolError("cancel-all result must be an object")
            payload = cast(Mapping[str, object], item)
            order_id = _text(payload, "id")
            status = _decimal(payload, "status")
            if status == 204:
                cancelled.append(order_id)
            else:
                failures.append(order_id)
        if failures:
            raise BrokerRequestError(
                f"Alpaca Paper failed to cancel orders: {', '.join(sorted(failures))}"
            )
        return tuple(sorted(cancelled))

    @staticmethod
    def _fill_from_payload(payload: Mapping[str, object]) -> Fill:
        activity_type = _text(payload, "activity_type")
        if activity_type != "FILL":
            raise BrokerProtocolError("broker activity is not a fill")
        try:
            return Fill(
                fill_id=_text(payload, "id"),
                broker_order_id=_text(payload, "order_id"),
                symbol=_text(payload, "symbol"),
                side=OrderSide(_text(payload, "side").upper()),
                quantity=_decimal(payload, "qty"),
                price=_decimal(payload, "price"),
                occurred_at=_timestamp(payload, "transaction_time"),
                fee=Decimal("0"),
            )
        except (ValidationError, ValueError) as error:
            raise BrokerProtocolError("broker fill failed domain validation") from error

    async def get_fills(self, *, after: datetime | None = None) -> tuple[Fill, ...]:
        base_params = {
            "activity_types": "FILL",
            "direction": "asc",
            "page_size": str(self._activity_page_size),
        }
        if after is not None:
            base_params["after"] = _utc_parameter(after)
        page_token: str | None = None
        seen_tokens: set[str] = set()
        seen_fill_ids: set[str] = set()
        fills: list[Fill] = []
        while True:
            params = dict(base_params)
            if page_token is not None:
                params["page_token"] = page_token
            response = await self._request(
                "GET",
                "/v2/account/activities",
                params=params,
            )
            page = _array_payload(response)
            for item in page:
                if not isinstance(item, dict):
                    raise BrokerProtocolError("broker fill entry must be an object")
                fill = self._fill_from_payload(cast(Mapping[str, object], item))
                if fill.fill_id in seen_fill_ids:
                    raise BrokerProtocolError(f"duplicate fill activity: {fill.fill_id}")
                seen_fill_ids.add(fill.fill_id)
                fills.append(fill)
            if len(page) < self._activity_page_size:
                break
            last = page[-1]
            if not isinstance(last, dict):
                raise BrokerProtocolError("broker fill entry must be an object")
            next_token = _text(cast(Mapping[str, object], last), "id")
            if next_token in seen_tokens:
                raise BrokerProtocolError("broker fill pagination token cycle detected")
            seen_tokens.add(next_token)
            page_token = next_token
        return tuple(fills)

    async def get_market_clock(self) -> MarketClock:
        response = await self._request("GET", "/v2/clock")
        payload = _object_payload(response)
        try:
            return MarketClock(
                timestamp=_timestamp(payload, "timestamp"),
                is_open=_boolean(payload, "is_open"),
                next_open=_timestamp(payload, "next_open"),
                next_close=_timestamp(payload, "next_close"),
            )
        except ValidationError as error:
            raise BrokerProtocolError("broker market clock failed domain validation") from error
