"""Build the research components from configuration. The composition root (#9, #3).

Nothing in `src/` constructed a `ModelRuntime`, so #9's runtime could route, count tokens, fall
back and enforce its rules, and no code path ever reached it. This is where the wiring lives.

Deliberately **not** in `quantbot/config.py`. That module is the trading daemon's settings, and
it is loaded on the path that decides whether real orders may be sent. Research configuration
belongs nowhere near it: a malformed model endpoint must not be able to change what the trading
Settings validate, and an operator editing model routing should not be editing the file that
carries `LIVE_TRADING_ACKNOWLEDGED`.

Everything here **fails closed and loudly**. An unconfigured system raises `ResearchNotConfigured`
naming the variable it needs. It does not fall back to a default endpoint, invent a model name, or
return a runtime that will fail later at the first call -- a research component that appears to
exist and cannot work is worse than one that says it is absent, because the first is discovered
halfway through a cycle and the second before one starts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from quantbot.research.models import (
    STRUCTURED_OUTPUT,
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    OpenAICompatibleTransport,
    RoleRouting,
)
from quantbot.research.transports import HttpxModelTransport

#: Environment variables this reads. Named here so an error can point at one.
ENDPOINT = "QUANTBOT_MODEL_ENDPOINT"
GENERATOR = "QUANTBOT_MODEL_GENERATOR"
CRITIC = "QUANTBOT_MODEL_CRITIC"
API_KEY = "QUANTBOT_MODEL_API_KEY"
CONTEXT_TOKENS = "QUANTBOT_MODEL_CONTEXT_TOKENS"
CRITIC_MIN_CONTEXT = "QUANTBOT_CRITIC_MIN_CONTEXT_TOKENS"


class ResearchNotConfigured(RuntimeError):
    """Raised when a research component cannot be built from the current environment."""


@dataclass(frozen=True, slots=True)
class ModelConfiguration:
    """What the operator declared, before anything is constructed from it."""

    endpoint: str
    generator: str
    critic: str
    context_tokens: int
    critic_minimum_context_tokens: int
    api_key: SecretStr | None


def _required(name: str, purpose: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ResearchNotConfigured(
            f"{name} is not set, so {purpose}. Research components fail closed rather than "
            "guessing an endpoint or a model name"
        )
    return value


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ResearchNotConfigured(f"{name} must be an integer, not {raw!r}") from error
    if value < 1:
        raise ResearchNotConfigured(f"{name} must be positive, not {value}")
    return value


def model_configuration() -> ModelConfiguration:
    """Read the declared model configuration, refusing an incomplete one."""
    generator = _required(GENERATOR, "no model is configured to generate")
    critic = _required(CRITIC, "no model is configured to criticise")
    if generator == critic:
        # RoleRouting refuses this too, on identity rather than on name. Catching it here means
        # the operator is told which variable to change instead of reading a routing error.
        raise ResearchNotConfigured(
            f"{GENERATOR} and {CRITIC} name the same model ({generator}). A model asked to "
            "review its own proposal tends to find it sound; configure a different one"
        )
    key = os.environ.get(API_KEY, "").strip()
    return ModelConfiguration(
        endpoint=_required(ENDPOINT, "there is nowhere to send a completion"),
        generator=generator,
        critic=critic,
        context_tokens=_positive_int(CONTEXT_TOKENS, 8192),
        critic_minimum_context_tokens=_positive_int(CRITIC_MIN_CONTEXT, 8192),
        # A local endpoint legitimately has no key, and an empty string is not a key. Storing
        # "" as a SecretStr would send an `Authorization: Bearer ` header, which some servers
        # reject and none need.
        api_key=SecretStr(key) if key else None,
    )


def _spec(model: str, endpoint: str, context_tokens: int) -> ModelSpec:
    """One configured backend.

    `structured_output` is declared True because `call_structured` needs it and the operator has
    named this model for a role that uses it. That is a claim about the configuration, not a
    probe of the server -- nothing here has contacted the endpoint, and a capability cannot be
    verified without doing so.
    """
    return ModelSpec(
        provider="openai-compatible",
        model=model,
        version="configured",
        endpoint=endpoint,
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=context_tokens,
            structured_output=True,
            local="localhost" in endpoint or "127.0.0.1" in endpoint,
            cost_class=CostClass.LOCAL if "localhost" in endpoint else CostClass.CHEAP,
        ),
    )


def build_model_runtime(
    configuration: ModelConfiguration | None = None,
    *,
    timeout_seconds: float = 60.0,
    client: httpx.Client | None = None,
) -> ModelRuntime:
    """Construct a runtime from configuration, or raise saying what is missing.

    `client` exists so a test can swap the socket while keeping every layer this function
    assembled -- the routing, the adapter, the request construction. Reaching into the returned
    object's private attributes instead would test a runtime this function did not build.

    `fall_back` is left at its default of False. A silent downgrade to a weaker critic is the
    failure nobody notices, and a two-model configuration has nowhere to fall back to anyway --
    the only other model is the one the routing rules forbid for that role.
    """
    settings = configuration or model_configuration()
    routing = RoleRouting(
        chains={
            ModelRole.GENERATOR: (
                _spec(settings.generator, settings.endpoint, settings.context_tokens),
            ),
            ModelRole.CRITIC: (_spec(settings.critic, settings.endpoint, settings.context_tokens),),
        },
        critic_minimum_context_tokens=settings.critic_minimum_context_tokens,
        critic_requires=STRUCTURED_OUTPUT,
    )
    # Two layers on purpose: HttpxModelTransport is the wire (PostJson) and
    # OpenAICompatibleTransport is the protocol adapter (ChatTransport). Keeping them separate
    # is what lets every test drive the real request path with a mock socket.
    return ModelRuntime(
        routing,
        OpenAICompatibleTransport(HttpxModelTransport(client=client, api_key=settings.api_key)),
        timeout_seconds=timeout_seconds,
    )


def research_is_configured() -> bool:
    """Whether a runtime could be built, without building one or contacting anything."""
    try:
        model_configuration()
    except ResearchNotConfigured:
        return False
    return True


__all__ = [
    "API_KEY",
    "CONTEXT_TOKENS",
    "CRITIC",
    "CRITIC_MIN_CONTEXT",
    "ENDPOINT",
    "GENERATOR",
    "ModelConfiguration",
    "ResearchNotConfigured",
    "build_model_runtime",
    "model_configuration",
    "research_is_configured",
]
