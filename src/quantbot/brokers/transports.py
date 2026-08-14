"""Concrete HTTP transport for broker REST adapters."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

import httpx

from quantbot.brokers.base import (
    BrokerTransportError,
    BrokerTransportResponse,
)


class HttpxBrokerTransport:
    """Map the broker-neutral transport contract onto httpx."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or httpx.AsyncClient()

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
        if not timeout_seconds.is_finite() or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")
        try:
            response = await self._client.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=float(timeout_seconds),
            )
        except httpx.TransportError as error:
            raise BrokerTransportError("broker HTTP transport failed") from error
        return BrokerTransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers.items()),
            content=response.content,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
