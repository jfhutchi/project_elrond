"""Replaceable model infrastructure, with one routing rule that is not configuration (#9).

Most of this is ordinary provider-neutrality: a role names a model, a chain names its fallbacks,
and swapping either is a config change. Two things are not ordinary.

**A critic must not share a model identity with the generator it reviews.** Not because another
vendor is smarter, but because the same model asked to critique its own proposal tends to find
it sound. #7 already refuses to let agreement outvote an objection; this refuses to let the
agreement be an artefact of asking one model twice. It is enforced in the routing rather than
left to whoever writes the config, because a rule that only holds when someone remembers it is
the rule that fails on the busy day.

**Provenance is produced by the runtime, not reconstructed later.** A conclusion whose reasoning
came from a model that has since been updated is not reproducible, and without the identity and
the prompt hash there is no way to know that happened. `ModelResponse.provenance()` hands #18
exactly what a manifest needs, so it cannot go missing through the abstraction layer.

Secrets never reach a prompt. The check is the same credential matcher the manifest uses, and
it raises rather than redacting: a prompt is sent to a third party, and quietly stripping a key
that should never have been there hides the bug that put it there.

Transport is injected, following `market_data.transports`, so an OpenAI-compatible endpoint --
Ollama, LM Studio, vLLM, llama.cpp -- is exercised in tests without a network. The concrete
httpx implementation of `PostJson` lives in `research.transports`, beside the other two.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.manifest import ModelProvenance, canonical_result_json, looks_like_credential

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class ModelRole(StrEnum):
    DIRECTOR = "DIRECTOR"
    SCOUT = "SCOUT"
    GENERATOR = "GENERATOR"
    #: Must not share an identity with GENERATOR. See the module docstring.
    CRITIC = "CRITIC"
    CODER = "CODER"
    SUMMARIZER = "SUMMARIZER"


class CostClass(StrEnum):
    LOCAL = "LOCAL"
    CHEAP = "CHEAP"
    EXPENSIVE = "EXPENSIVE"


class ModelError(RuntimeError):
    """Base error for the model runtime."""


class ModelUnavailable(ModelError):
    """Raised when no model in a role's chain can serve a call."""


class CredentialInPrompt(ModelError):
    """Raised when a prompt carries something shaped like a credential.

    Raising rather than redacting: a prompt leaves this machine, and silently stripping a key
    that should never have been assembled hides whatever put it there.
    """


class RoutingError(ModelError):
    """Raised when a routing table violates a rule that is not negotiable."""


class ModelTransportError(ModelError):
    """Raised when the endpoint could not be reached, or answered with something other than 200.

    Carries a status code and nothing else. Provider error bodies routinely echo part of the
    key that was rejected, and this message is interpolated into `ModelUnavailable` and from
    there into logs, so the body does not travel with it.
    """


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelCapabilities(FrozenModel):
    """What a backend can actually do, so a role is not routed somewhere it cannot run.

    `structured_output` and `tools` were declared here and read nowhere for as long as this class
    existed, so the docstring's promise was not kept by anything: a call needing structured
    output was routed to a model that could not produce it, and the failure appeared later as
    unparseable text rather than as a routing refusal. `ModelRuntime.call` now takes the
    requirement and skips specs that cannot meet it.

    `context_tokens` is still not enforced. Doing so needs a token count for the rendered prompt,
    and this module has no tokenizer -- estimating one would put a guess in the path of a routing
    decision. Stated rather than quietly unimplemented.
    """

    context_tokens: int = Field(ge=1)
    structured_output: bool = False
    tools: bool = False
    local: bool = False
    cost_class: CostClass = CostClass.LOCAL


#: Capabilities a particular call needs. Members are `ModelCapabilities` field names.
Capability = frozenset[str]

STRUCTURED_OUTPUT: Capability = frozenset({"structured_output"})
TOOLS: Capability = frozenset({"tools"})


def _missing(spec: ModelSpec, needs: Capability) -> list[str]:
    """Required capabilities this spec does not have, in a stable order."""
    return sorted(name for name in needs if not getattr(spec.capabilities, name, False))


class ModelSpec(FrozenModel):
    """One configured backend. Provider plus model plus version is its identity."""

    provider: Text
    model: Text
    version: Text
    #: OpenAI-compatible base URL. Local endpoints are the common case.
    endpoint: Text
    parameters: dict[str, str] = Field(default_factory=dict)
    capabilities: ModelCapabilities

    @model_validator(mode="after")
    def refuse_embedded_credentials(self) -> Self:
        """A spec is hashed into provenance and serialised into manifests; a key must not be here.

        The key belongs to the transport, which is neither hashed nor serialised. Using the same
        matcher `manifest.py` uses, and raising rather than redacting, for the same reason: a
        stripped key hides whatever assembled it.
        """
        carried = [self.endpoint, *self.parameters.values()]
        if any(looks_like_credential(value) for value in carried):
            raise ValueError(
                f"{self.provider}/{self.model} carries something shaped like a credential in "
                "its endpoint or parameters; give it to the transport instead, which is never "
                "hashed or serialised"
            )
        return self

    @property
    def identity(self) -> tuple[str, str, str]:
        """What "a different model" means. Two roles sharing this share a model."""
        return (self.provider, self.model, self.version)

    @property
    def parameters_hash(self) -> str:
        return hashlib.sha256(canonical_result_json(self.parameters).encode("utf-8")).hexdigest()


class PromptTemplate(FrozenModel):
    """A prompt with a version, because a changed prompt is a changed experiment."""

    name: Text
    version: Text
    text: Text

    @property
    def template_hash(self) -> str:
        return hashlib.sha256(f"{self.name}@{self.version}\n{self.text}".encode()).hexdigest()

    def render(self, values: Mapping[str, str]) -> str:
        rendered = self.text.format(**values)
        if looks_like_credential(rendered):
            raise CredentialInPrompt(
                f"{self.name}@{self.version} rendered a value shaped like a credential; "
                "a prompt must never carry one"
            )
        return rendered


class RoleRouting(FrozenModel):
    """Which models serve which roles, and in what order when one is unavailable."""

    chains: dict[ModelRole, tuple[ModelSpec, ...]]
    #: Fall back down the chain on failure, or refuse. Fail-closed is the default because a
    #: silent downgrade to a weaker critic is the failure nobody notices.
    fall_back: bool = False
    #: Enforced rather than advisory. Set False only with a recorded reason.
    require_distinct_critic: bool = True

    @model_validator(mode="after")
    def validate_routing(self) -> Self:
        if not self.chains:
            raise ValueError("a routing table needs at least one role")
        for role, chain in self.chains.items():
            if not chain:
                raise ValueError(f"{role.value} has no model configured")

        generator = self.chains.get(ModelRole.GENERATOR)
        critic = self.chains.get(ModelRole.CRITIC)
        if self.require_distinct_critic and generator and critic:
            shared = {spec.identity for spec in generator} & {spec.identity for spec in critic}
            if shared:
                raise RoutingError(
                    "the critic must not share a model identity with the generator it "
                    "reviews: " + ", ".join(":".join(item) for item in sorted(shared))
                )
        return self

    def chain(self, role: ModelRole) -> tuple[ModelSpec, ...]:
        chain = self.chains.get(role)
        if chain is None:
            raise ModelUnavailable(f"no model is configured for {role.value}")
        return chain


class CircuitBreaker:
    """Stop calling a backend that keeps failing, and let it back in after a cooldown."""

    def __init__(self, *, threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least one failure")
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._failures: dict[tuple[str, str, str], int] = {}
        self._opened_at: dict[tuple[str, str, str], datetime] = {}

    def record_failure(self, spec: ModelSpec, *, now: datetime) -> None:
        key = spec.identity
        self._failures[key] = self._failures.get(key, 0) + 1
        if self._failures[key] >= self._threshold:
            self._opened_at[key] = now

    def record_success(self, spec: ModelSpec) -> None:
        self._failures.pop(spec.identity, None)
        self._opened_at.pop(spec.identity, None)

    def is_open(self, spec: ModelSpec, *, now: datetime) -> bool:
        opened = self._opened_at.get(spec.identity)
        if opened is None:
            return False
        if (now - opened).total_seconds() >= self._cooldown:
            self._failures.pop(spec.identity, None)
            self._opened_at.pop(spec.identity, None)
            return False
        return True


class ModelResponse(FrozenModel):
    """What came back, and everything needed to say what produced it."""

    spec: ModelSpec
    template: PromptTemplate
    prompt: Text
    text: Text
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    produced_at: datetime
    #: Where in the role's chain this model sat. 0 is the primary (#9).
    chain_position: int = Field(default=0, ge=0)
    #: Models tried before this one and why they did not serve, in order. Empty on a primary.
    #:
    #: Recorded because a fallback answer used to be indistinguishable from a primary one: the
    #: attempt list was built and then thrown away on success, so a critique produced by a
    #: degraded backup model read exactly like one from the model the operator chose. A result
    #: that cannot say it was produced under degradation cannot be weighed against one that was
    #: not.
    superseded: tuple[Text, ...] = ()

    @property
    def is_fallback(self) -> bool:
        return self.chain_position > 0

    @property
    def response_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def provenance(self) -> ModelProvenance:
        """Exactly what #18's manifest needs, produced here rather than reconstructed."""
        return ModelProvenance(
            provider=self.spec.provider,
            model=f"{self.spec.model}@{self.spec.version}",
            prompt_template_hash=self.template.template_hash,
            parameters_hash=self.spec.parameters_hash,
        )


class ChatTransport(Protocol):
    """The one call a backend has to serve. Injected so tests need no network."""

    def complete(
        self, spec: ModelSpec, prompt: str, *, timeout_seconds: float
    ) -> tuple[str, int, int]: ...


class ModelRuntime:
    """Route a role to a model, call it, and hand back provenance with the answer."""

    def __init__(
        self,
        routing: RoleRouting,
        transport: ChatTransport,
        *,
        breaker: CircuitBreaker | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        self._routing = routing
        self._transport = transport
        self._breaker = breaker or CircuitBreaker()
        self._timeout = timeout_seconds

    def resolve(
        self,
        role: ModelRole,
        *,
        now: datetime,
        needs: Capability = frozenset(),
    ) -> ModelSpec:
        """The first model in the role's chain that is neither tripped nor incapable."""
        for spec in self._routing.chain(role):
            if self._breaker.is_open(spec, now=now):
                continue
            if not _missing(spec, needs):
                return spec
        raise ModelUnavailable(f"every model configured for {role.value} is unavailable")

    def call(
        self,
        role: ModelRole,
        template: PromptTemplate,
        values: Mapping[str, str],
        *,
        now: datetime,
        needs: Capability = frozenset(),
    ) -> ModelResponse:
        prompt = template.render(values)
        chain = self._routing.chain(role)
        attempted: list[str] = []
        for position, spec in enumerate(chain):
            if self._breaker.is_open(spec, now=now):
                attempted.append(f"{spec.model} (circuit open)")
                continue
            lacking = _missing(spec, needs)
            if lacking:
                # Skipped rather than tried. Routing a structured-output call to a model that
                # cannot produce it does not fail at the routing layer -- it fails later as
                # unparseable text, which reads like a bad answer rather than a bad route.
                attempted.append(f"{spec.model} (cannot {', '.join(lacking)})")
                continue
            try:
                text, prompt_tokens, completion_tokens = self._transport.complete(
                    spec, prompt, timeout_seconds=self._timeout
                )
            except Exception as error:  # noqa: BLE001 - any backend failure trips the breaker
                self._breaker.record_failure(spec, now=now)
                attempted.append(f"{spec.model} ({error})")
                if not self._routing.fall_back:
                    raise ModelUnavailable(
                        f"{role.value} failed on {spec.model} and the policy is fail-closed"
                    ) from error
                continue
            self._breaker.record_success(spec)
            return ModelResponse(
                spec=spec,
                template=template,
                prompt=prompt,
                text=text,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                produced_at=now,
                chain_position=position,
                superseded=tuple(attempted),
            )
        raise ModelUnavailable(f"{role.value} exhausted its chain: {'; '.join(attempted)}")


class OpenAICompatibleTransport:
    """Talk to any OpenAI-compatible `/chat/completions`, local or hosted.

    Kept deliberately thin. The HTTP client is injected exactly as `market_data.transports` does
    it, so a local endpoint is exercised in tests without a network and without a vendor SDK.
    """

    def __init__(self, post: PostJson) -> None:
        self._post = post

    def complete(
        self, spec: ModelSpec, prompt: str, *, timeout_seconds: float
    ) -> tuple[str, int, int]:
        payload = {
            "model": spec.model,
            "messages": [{"role": "user", "content": prompt}],
            **spec.parameters,
        }
        body = self._post(
            f"{spec.endpoint.rstrip('/')}/chat/completions",
            payload,
            timeout_seconds=timeout_seconds,
        )
        try:
            decoded = json.loads(body)
            text = decoded["choices"][0]["message"]["content"]
            usage = decoded.get("usage") or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
            raise ModelError(f"{spec.model} returned an unreadable response") from error
        if not isinstance(text, str) or not text.strip():
            # An empty completion becoming "the model had no objection" is precisely the
            # failure this project is built to refuse. A missing answer is not an answer.
            raise ModelError(f"{spec.model} returned no completion text; silence is not agreement")
        if prompt_tokens < 0 or completion_tokens < 0:
            raise ModelError(f"{spec.model} reported negative token usage")
        return text, prompt_tokens, completion_tokens


class PostJson(Protocol):
    def __call__(
        self, url: str, payload: Mapping[str, object], *, timeout_seconds: float
    ) -> str: ...


def distinct_identities(specs: Sequence[ModelSpec]) -> int:
    return len({spec.identity for spec in specs})


__all__ = [
    "ChatTransport",
    "CircuitBreaker",
    "CostClass",
    "CredentialInPrompt",
    "Capability",
    "ModelCapabilities",
    "STRUCTURED_OUTPUT",
    "TOOLS",
    "ModelError",
    "ModelResponse",
    "ModelRole",
    "ModelRuntime",
    "ModelSpec",
    "ModelTransportError",
    "ModelUnavailable",
    "OpenAICompatibleTransport",
    "PostJson",
    "PromptTemplate",
    "RoleRouting",
    "RoutingError",
    "distinct_identities",
]
