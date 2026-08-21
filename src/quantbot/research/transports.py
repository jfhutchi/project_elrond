"""Concrete HTTP transport for the model runtime's OpenAI-compatible endpoints (#9).

`models.py` built the router, the routing rule and the response parser, and left the wire to an
injected `PostJson`. Nothing implemented it, so the runtime could not reach a model at all. This
is that implementation, and it is deliberately the only one: `/v1/chat/completions` is the shape
Ollama, LM Studio, llama.cpp's server, vLLM and the hosted APIs all speak, so one adapter covers
local and hosted without a vendor SDK.

The client is injected exactly as `market_data.transports` and `brokers.transports` inject
theirs, so the production path is exercised in tests through `httpx.MockTransport` with no
network and no endpoint running.

Two things this file refuses to do.

**It does not return a body it could not verify.** A non-200, a connection failure and a timeout
each raise `ModelTransportError`. An endpoint that answers HTTP 500 with a well-formed-looking
completion is still a failure; letting the status pass unread is how a proxy's error page becomes
a critic's verdict.

**It does not put the response body in an error message.** Provider error bodies routinely echo
the key that was rejected -- OpenAI's 401 quotes a prefix of it -- and this message is
interpolated into `ModelUnavailable` by `ModelRuntime` and from there into logs. The status code
travels; the body does not. For the same reason the key is held as a `SecretStr` and the
`Authorization` header is assembled per call rather than stored, so no plaintext key exists on
the instance for a repr or a traceback to find.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import httpx
from pydantic import SecretStr

from quantbot.research.models import ModelTransportError


class HttpxModelTransport:
    """POST a chat-completions payload over httpx. Satisfies `models.PostJson`.

    The base URL and model name arrive on the `ModelSpec`, and the timeout from the
    `ModelRuntime`, so pointing a role at a different endpoint is configuration. The credential
    arrives here instead, because a `ModelSpec` is hashed into provenance and serialised into
    manifests and this is not.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        api_key: SecretStr | None = None,
    ) -> None:
        self._client = client or httpx.Client()
        #: Held as a SecretStr and never unwrapped onto the instance. See the module docstring.
        self._api_key = api_key

    def __call__(self, url: str, payload: Mapping[str, object], *, timeout_seconds: float) -> str:
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive and finite")

        headers = {"Accept": "application/json"}
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"

        try:
            response = self._client.post(
                url,
                json=dict(payload),
                headers=headers,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise ModelTransportError(
                f"the chat-completions endpoint did not answer within {timeout_seconds:g}s"
            ) from error
        except httpx.TransportError as error:
            message = "the chat-completions endpoint could not be reached"
            raise ModelTransportError(message) from error

        if response.status_code != httpx.codes.OK:
            raise ModelTransportError(
                f"the chat-completions endpoint answered HTTP {response.status_code}"
            )
        return response.text

    def close(self) -> None:
        self._client.close()


__all__ = ["HttpxModelTransport"]
