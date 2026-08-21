"""The composition root: what it refuses to build, and that what it builds actually works."""

from __future__ import annotations

import json

import httpx
import pytest

from quantbot.research.composition import (
    API_KEY,
    CONTEXT_TOKENS,
    CRITIC,
    CRITIC_MIN_CONTEXT,
    ENDPOINT,
    GENERATOR,
    ModelConfiguration,
    ResearchNotConfigured,
    build_model_runtime,
    model_configuration,
    research_is_configured,
)
from quantbot.research.models import ModelRole, ModelRuntime, RoutingError

CONFIGURED = {
    ENDPOINT: "http://localhost:11434/v1",
    GENERATOR: "qwen2.5:32b",
    CRITIC: "llama3.3:70b",
}


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: str | None) -> None:
    for name in (ENDPOINT, GENERATOR, CRITIC, API_KEY, CONTEXT_TOKENS, CRITIC_MIN_CONTEXT):
        monkeypatch.delenv(name, raising=False)
    for name, value in {**CONFIGURED, **overrides}.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


def test_an_unconfigured_system_says_which_variable_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail closed and loudly.

    A component that appears to exist and cannot work is worse than one that says it is absent:
    the first is discovered halfway through a research cycle, the second before one starts.
    """
    _configure(monkeypatch, **{GENERATOR: None})
    with pytest.raises(ResearchNotConfigured, match=GENERATOR):
        model_configuration()
    assert research_is_configured() is False

    _configure(monkeypatch, **{ENDPOINT: None})
    with pytest.raises(ResearchNotConfigured, match=ENDPOINT):
        model_configuration()

    _configure(monkeypatch, **{CRITIC: None})
    with pytest.raises(ResearchNotConfigured, match=CRITIC):
        model_configuration()


def test_no_default_endpoint_is_invented(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guessing localhost:11434 would make an unconfigured system look configured.

    It would then fail at the first call, deep inside a cycle, with a connection error rather
    than a configuration error -- which is a much harder thing to diagnose than a missing
    variable.
    """
    _configure(monkeypatch, **{ENDPOINT: None})
    with pytest.raises(ResearchNotConfigured):
        build_model_runtime()


def test_the_same_model_for_both_roles_is_refused_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Caught here so the operator is told which variable to change.

    `RoleRouting` refuses this too, on model identity. Catching it at configuration means the
    message names `QUANTBOT_MODEL_CRITIC` rather than surfacing as a routing error about
    identities the operator never typed.
    """
    _configure(monkeypatch, **{CRITIC: CONFIGURED[GENERATOR]})
    with pytest.raises(ResearchNotConfigured, match="review its own proposal"):
        model_configuration()


def test_a_critic_below_the_declared_floor_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The floor reaches the runtime, rather than being a field nobody passes."""
    _configure(monkeypatch, **{CONTEXT_TOKENS: "4096", CRITIC_MIN_CONTEXT: "32768"})
    with pytest.raises(RoutingError, match="against a critic floor"):
        build_model_runtime()


def test_a_malformed_number_is_refused_rather_than_defaulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Silently defaulting a typo would apply a limit the operator did not choose."""
    _configure(monkeypatch, **{CONTEXT_TOKENS: "lots"})
    with pytest.raises(ResearchNotConfigured, match="must be an integer"):
        model_configuration()

    _configure(monkeypatch, **{CONTEXT_TOKENS: "0"})
    with pytest.raises(ResearchNotConfigured, match="must be positive"):
        model_configuration()


def test_an_absent_api_key_is_none_rather_than_an_empty_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local endpoint has no key, and "" is not a key.

    Storing an empty SecretStr would send `Authorization: Bearer ` with nothing after it, which
    some servers reject and none need.
    """
    _configure(monkeypatch)
    assert model_configuration().api_key is None

    _configure(monkeypatch, **{API_KEY: "sk-realkey1234567890abcdef"})
    key = model_configuration().api_key
    assert key is not None
    assert key.get_secret_value().startswith("sk-")


def test_the_configuration_never_puts_the_key_in_its_repr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configuration object gets logged; the key must not travel with it."""
    _configure(monkeypatch, **{API_KEY: "sk-supersecret1234567890abcd"})
    configuration = model_configuration()

    assert "supersecret" not in repr(configuration)


def test_what_it_builds_actually_completes_a_call() -> None:
    """The point of a composition root is that the thing it returns works.

    Driven through a mock socket rather than a real endpoint: this exercises the production
    request path -- real routing, real adapter, real client, real request construction -- and
    stops short of the network, which is the one part no test can assert for the operator.
    """
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "no objection"}}], "usage": {}},
        )

    configuration = ModelConfiguration(
        endpoint="http://localhost:11434/v1",
        generator="qwen2.5:32b",
        critic="llama3.3:70b",
        context_tokens=32768,
        critic_minimum_context_tokens=8192,
        api_key=None,
    )
    # Only the socket is injected; the routing, adapter and request construction are the ones
    # the composition root assembled.
    runtime = build_model_runtime(
        configuration, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    assert isinstance(runtime, ModelRuntime)

    from datetime import UTC, datetime

    from quantbot.research.models import PromptTemplate

    response = runtime.call(
        ModelRole.GENERATOR,
        PromptTemplate(name="t", version="1", text="say something about {x}"),
        {"x": "momentum"},
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert response.text == "no objection"
    assert response.spec.model == "qwen2.5:32b"
    assert not response.is_fallback
    assert captured, "the request reached the wire"
    assert json.loads(captured[0].content)["model"] == "qwen2.5:32b"
