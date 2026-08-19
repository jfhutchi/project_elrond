"""What the model runtime must refuse, and what it must always hand back."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.research.models import (
    CircuitBreaker,
    CostClass,
    CredentialInPrompt,
    ModelCapabilities,
    ModelError,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    ModelUnavailable,
    OpenAICompatibleTransport,
    PromptTemplate,
    RoleRouting,
    RoutingError,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def spec(model: str = "qwen2.5-32b", *, version: str = "1", provider: str = "ollama") -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model=model,
        version=version,
        endpoint="http://localhost:11434/v1",
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=32768, structured_output=True, local=True, cost_class=CostClass.LOCAL
        ),
    )


def template() -> PromptTemplate:
    return PromptTemplate(
        name="critique-hypothesis",
        version="2026-08-18",
        text="Review this hypothesis and state what is wrong with it: {claim}",
    )


class FakeChat:
    """A backend that answers, or fails a set number of times first."""

    def __init__(self, *, answer: str = "no objection", failures: int = 0) -> None:
        self.answer = answer
        self.failures = failures
        self.calls: list[tuple[str, str]] = []

    def complete(
        self, model_spec: ModelSpec, prompt: str, *, timeout_seconds: float
    ) -> tuple[str, int, int]:
        self.calls.append((model_spec.model, prompt))
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("backend unreachable")
        return self.answer, 120, 40


def test_the_critic_cannot_share_a_model_with_the_generator_it_reviews() -> None:
    """The same model asked to critique its own proposal tends to find it sound."""
    shared = spec("qwen2.5-32b")
    with pytest.raises(RoutingError, match="must not share a model identity"):
        RoleRouting(
            chains={ModelRole.GENERATOR: (shared,), ModelRole.CRITIC: (shared,)}
        )

    # A different version is a different identity, and is accepted.
    routing = RoleRouting(
        chains={
            ModelRole.GENERATOR: (spec("qwen2.5-32b", version="1"),),
            ModelRole.CRITIC: (spec("llama3.3-70b", version="1"),),
        }
    )
    assert routing.chain(ModelRole.CRITIC)[0].model == "llama3.3-70b"

    # A shared model anywhere in either chain is caught, not just the primary.
    with pytest.raises(RoutingError):
        RoleRouting(
            chains={
                ModelRole.GENERATOR: (spec("llama3.3-70b"), spec("qwen2.5-32b")),
                ModelRole.CRITIC: (spec("mistral-large"), spec("qwen2.5-32b")),
            }
        )


def test_the_rule_can_be_waived_only_explicitly() -> None:
    """Configuration may override it; forgetting to configure it cannot."""
    shared = spec()
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (shared,), ModelRole.CRITIC: (shared,)},
        require_distinct_critic=False,
    )
    assert routing.chain(ModelRole.CRITIC)[0].identity == shared.identity


def test_different_roles_can_run_different_models_in_one_workflow() -> None:
    routing = RoleRouting(
        chains={
            ModelRole.GENERATOR: (spec("qwen2.5-32b"),),
            ModelRole.CRITIC: (spec("llama3.3-70b"),),
            ModelRole.SUMMARIZER: (spec("phi4", provider="lmstudio"),),
        }
    )
    transport = FakeChat()
    runtime = ModelRuntime(routing, transport)

    for role, expected in (
        (ModelRole.GENERATOR, "qwen2.5-32b"),
        (ModelRole.CRITIC, "llama3.3-70b"),
        (ModelRole.SUMMARIZER, "phi4"),
    ):
        assert runtime.resolve(role, now=NOW).model == expected


def test_swapping_a_role_model_is_configuration_not_code() -> None:
    transport = FakeChat()
    for model in ("qwen2.5-32b", "llama3.3-70b"):
        routing = RoleRouting(chains={ModelRole.SUMMARIZER: (spec(model),)})
        response = ModelRuntime(routing, transport).call(
            ModelRole.SUMMARIZER, template(), {"claim": "momentum works"}, now=NOW
        )
        assert response.spec.model == model
    assert [call[0] for call in transport.calls] == ["qwen2.5-32b", "llama3.3-70b"]


def test_a_response_carries_everything_the_manifest_needs() -> None:
    """Retrofitting provenance through an abstraction layer is how it goes missing."""
    routing = RoleRouting(chains={ModelRole.CRITIC: (spec(),)})
    response = ModelRuntime(routing, FakeChat(answer="the sample cannot resolve this")).call(
        ModelRole.CRITIC, template(), {"claim": "momentum works"}, now=NOW
    )

    provenance = response.provenance()
    assert provenance.provider == "ollama"
    assert provenance.model == "qwen2.5-32b@1"
    assert provenance.prompt_template_hash == template().template_hash
    assert provenance.parameters_hash == spec().parameters_hash
    assert len(response.response_hash) == 64
    assert response.total_tokens == 160

    # A changed prompt is a changed experiment, and the hash says so.
    revised = template().model_copy(update={"version": "2026-09-01"})
    assert revised.template_hash != template().template_hash


def test_a_prompt_carrying_a_credential_is_refused_rather_than_redacted() -> None:
    """A prompt leaves this machine. Stripping the key hides whatever assembled it."""
    with pytest.raises(CredentialInPrompt):
        template().render({"claim": "use PKTEST1234567890ABCDEF to fetch the bars"})
    assert template().render({"claim": "momentum works"}).endswith("momentum works")


def test_failure_fails_closed_by_default_and_falls_back_only_when_configured() -> None:
    routing = RoleRouting(
        chains={ModelRole.CRITIC: (spec("primary"), spec("secondary"))},
    )
    with pytest.raises(ModelUnavailable, match="fail-closed"):
        ModelRuntime(routing, FakeChat(failures=1)).call(
            ModelRole.CRITIC, template(), {"claim": "x"}, now=NOW
        )

    permitted = routing.model_copy(update={"fall_back": True})
    transport = FakeChat(failures=1)
    response = ModelRuntime(permitted, transport).call(
        ModelRole.CRITIC, template(), {"claim": "x"}, now=NOW
    )
    assert response.spec.model == "secondary"
    assert [call[0] for call in transport.calls] == ["primary", "secondary"]


def test_a_backend_that_keeps_failing_is_taken_out_and_let_back_in() -> None:
    breaker = CircuitBreaker(threshold=2, cooldown_seconds=30)
    backend = spec("flaky")
    assert not breaker.is_open(backend, now=NOW)

    breaker.record_failure(backend, now=NOW)
    assert not breaker.is_open(backend, now=NOW)
    breaker.record_failure(backend, now=NOW)
    assert breaker.is_open(backend, now=NOW)

    # Still open inside the cooldown, closed after it.
    assert breaker.is_open(backend, now=NOW + timedelta(seconds=29))
    assert not breaker.is_open(backend, now=NOW + timedelta(seconds=30))

    breaker.record_failure(backend, now=NOW)
    breaker.record_failure(backend, now=NOW)
    breaker.record_success(backend)
    assert not breaker.is_open(backend, now=NOW)


def test_an_exhausted_chain_says_what_it_tried() -> None:
    routing = RoleRouting(
        chains={ModelRole.CRITIC: (spec("primary"), spec("secondary"))}, fall_back=True
    )
    with pytest.raises(ModelUnavailable, match="exhausted its chain") as error:
        ModelRuntime(routing, FakeChat(failures=2)).call(
            ModelRole.CRITIC, template(), {"claim": "x"}, now=NOW
        )
    assert "primary" in str(error.value)
    assert "secondary" in str(error.value)


def test_an_unconfigured_role_is_unavailable_rather_than_defaulted() -> None:
    routing = RoleRouting(chains={ModelRole.CRITIC: (spec(),)})
    with pytest.raises(ModelUnavailable, match="no model is configured"):
        ModelRuntime(routing, FakeChat()).resolve(ModelRole.GENERATOR, now=NOW)
    with pytest.raises(ValueError, match="has no model configured"):
        RoleRouting(chains={ModelRole.CRITIC: ()})


def test_a_local_openai_compatible_endpoint_runs_a_complete_role() -> None:
    """Exercised end to end against an injected client, so no network and no vendor SDK."""
    seen: dict[str, object] = {}

    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        seen["url"] = url
        seen["payload"] = payload
        seen["timeout"] = timeout_seconds
        return json.dumps(
            {
                "choices": [{"message": {"content": "the sample cannot resolve this"}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            }
        )

    routing = RoleRouting(chains={ModelRole.CRITIC: (spec(),)})
    runtime = ModelRuntime(routing, OpenAICompatibleTransport(post), timeout_seconds=15.0)
    response = runtime.call(ModelRole.CRITIC, template(), {"claim": "momentum works"}, now=NOW)

    assert response.text == "the sample cannot resolve this"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["timeout"] == 15.0
    payload = seen["payload"]
    assert isinstance(payload, dict)
    assert payload["model"] == "qwen2.5-32b"
    assert payload["temperature"] == "0"
    assert response.provenance().model == "qwen2.5-32b@1"


def test_an_unreadable_backend_response_is_an_error_not_an_empty_answer() -> None:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return "<html>502 Bad Gateway</html>"

    with pytest.raises(ModelError, match="unreadable response"):
        OpenAICompatibleTransport(post).complete(spec(), "hello", timeout_seconds=5.0)
