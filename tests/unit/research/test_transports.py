"""What the wire to a model must refuse, and what it must never carry back out.

Every test here drives the production `httpx` path through `httpx.MockTransport`: a real client,
real request building, real response handling, no network and no endpoint running.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    ModelTransportError,
    ModelUnavailable,
    OpenAICompatibleTransport,
    PromptTemplate,
    RoleRouting,
)
from quantbot.research.transports import HttpxModelTransport

NOW = datetime(2026, 8, 20, 14, 30, tzinfo=UTC)

#: Shaped like a real key so a leak would be visible, and matched by the manifest's matcher.
API_KEY = "sk-test-0123456789abcdef0123"

COMPLETION = {
    "choices": [{"message": {"content": "the sample cannot resolve this"}}],
    "usage": {"prompt_tokens": 120, "completion_tokens": 40},
}


def spec(*, endpoint: str = "http://localhost:11434/v1") -> ModelSpec:
    return ModelSpec(
        provider="ollama",
        model="qwen2.5-32b",
        version="1",
        endpoint=endpoint,
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=32768, structured_output=True, local=True, cost_class=CostClass.LOCAL
        ),
    )


def template() -> PromptTemplate:
    return PromptTemplate(
        name="critique-hypothesis",
        version="2026-08-20",
        text="Review this hypothesis and state what is wrong with it: {claim}",
    )


def client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def runtime(transport: HttpxModelTransport, *, timeout_seconds: float = 12.0) -> ModelRuntime:
    return ModelRuntime(
        RoleRouting(chains={ModelRole.CRITIC: (spec(),)}),
        OpenAICompatibleTransport(transport),
        timeout_seconds=timeout_seconds,
    )


def test_a_local_endpoint_runs_a_complete_role_through_the_production_http_path() -> None:
    """Endpoint, model and timeout all arrive from configuration, and reach the wire."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        seen["timeout"] = request.extensions["timeout"]["read"]
        return httpx.Response(200, json=COMPLETION)

    with client(handler) as raw:
        response = runtime(HttpxModelTransport(raw), timeout_seconds=12.0).call(
            ModelRole.CRITIC, template(), {"claim": "momentum works"}, now=NOW
        )

    assert response.text == "the sample cannot resolve this"
    assert response.total_tokens == 160
    assert response.provenance().model == "qwen2.5-32b@1"
    assert seen["method"] == "POST"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["timeout"] == 12.0
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["model"] == "qwen2.5-32b"
    assert body["temperature"] == "0"
    assert body["messages"][0]["content"].endswith("momentum works")


def test_a_hosted_endpoint_is_the_same_interface_with_a_different_configuration() -> None:
    """Swapping local for hosted moves a URL and supplies a key. No application code changes."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["authorization"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=COMPLETION)

    hosted = spec(endpoint="https://api.openai.example/v1")
    with client(handler) as raw:
        transport = HttpxModelTransport(raw, api_key=SecretStr(API_KEY))
        text, prompt_tokens, _ = OpenAICompatibleTransport(transport).complete(
            hosted, "review this", timeout_seconds=5.0
        )

    assert text == "the sample cannot resolve this"
    assert prompt_tokens == 120
    assert seen["url"] == "https://api.openai.example/v1/chat/completions"
    assert seen["authorization"] == f"Bearer {API_KEY}"
    # The credential authenticates the request and appears nowhere else in it.
    assert API_KEY not in str(seen["body"])


def test_the_key_is_never_stored_in_a_form_a_repr_or_traceback_can_read() -> None:
    """A SecretStr on the instance, and the header assembled per call. Nothing to leak."""
    transport = HttpxModelTransport(api_key=SecretStr(API_KEY))
    try:
        assert API_KEY not in repr(transport)
        assert API_KEY not in repr(vars(transport))
        assert API_KEY not in str(vars(transport))
    finally:
        transport.close()


def test_an_endpoint_without_a_key_sends_no_authorization_header() -> None:
    """Ollama and llama.cpp want no credential, and inventing one is a 401 waiting to happen."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, json=COMPLETION)

    with client(handler) as raw:
        OpenAICompatibleTransport(HttpxModelTransport(raw)).complete(
            spec(), "review this", timeout_seconds=5.0
        )

    assert seen["authorization"] is None


def test_a_non_200_fails_even_when_the_body_would_have_parsed() -> None:
    """A proxy's error page must not become a critic's verdict because the status went unread."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json=COMPLETION)

    with client(handler) as raw:
        with pytest.raises(ModelTransportError, match="HTTP 500"):
            OpenAICompatibleTransport(HttpxModelTransport(raw)).complete(
                spec(), "review this", timeout_seconds=5.0
            )


def test_an_error_body_that_echoes_the_key_never_reaches_the_error_message() -> None:
    """Providers quote the rejected key back at you. This message ends up in logs."""
    echoed = f"Incorrect API key provided: {API_KEY}. You can find your key at ..."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": echoed, "code": "invalid_api_key"}})

    with client(handler) as raw:
        transport = OpenAICompatibleTransport(HttpxModelTransport(raw, api_key=SecretStr(API_KEY)))
        with pytest.raises(ModelTransportError) as direct:
            transport.complete(spec(), "review this", timeout_seconds=5.0)

        # And again through the runtime, which interpolates the transport's message into its own.
        with pytest.raises(ModelUnavailable) as routed:
            ModelRuntime(RoleRouting(chains={ModelRole.CRITIC: (spec(),)}), transport).call(
                ModelRole.CRITIC, template(), {"claim": "momentum works"}, now=NOW
            )

    for message in (str(direct.value), str(routed.value)):
        assert API_KEY not in message
        assert "Incorrect API key" not in message
    assert "HTTP 401" in str(direct.value)


def test_a_timeout_is_a_typed_error_rather_than_an_httpx_exception() -> None:
    """`ModelTransportError` is not an httpx exception, so this cannot pass by accident."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with client(handler) as raw:
        with pytest.raises(ModelTransportError, match="did not answer within 5s"):
            OpenAICompatibleTransport(HttpxModelTransport(raw)).complete(
                spec(), "review this", timeout_seconds=5.0
            )


def test_a_connection_failure_is_typed_and_does_not_repeat_what_httpx_said() -> None:
    """httpx puts the target in its message, and some endpoints carry a token in the URL."""
    leaky = f"connection refused: http://localhost:11434/v1?api_key={API_KEY}"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(leaky, request=request)

    with client(handler) as raw:
        with pytest.raises(ModelTransportError, match="could not be reached") as error:
            OpenAICompatibleTransport(HttpxModelTransport(raw)).complete(
                spec(), "review this", timeout_seconds=5.0
            )

    assert API_KEY not in str(error.value)
    assert "connection refused" not in str(error.value)
    # The cause is preserved for a debugger who already has the process, not for a log line.
    assert isinstance(error.value.__cause__, httpx.ConnectError)


@pytest.mark.parametrize("timeout", [0.0, -1.0, float("inf"), float("nan")])
def test_a_timeout_that_cannot_bound_the_call_is_refused(timeout: float) -> None:
    transport = HttpxModelTransport()
    try:
        with pytest.raises(ValueError, match="positive and finite"):
            transport("http://localhost:11434/v1/chat/completions", {}, timeout_seconds=timeout)
    finally:
        transport.close()


def test_closing_the_transport_closes_the_injected_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=COMPLETION)

    raw = client(handler)
    HttpxModelTransport(raw).close()
    assert raw.is_closed is True
