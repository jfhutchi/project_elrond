"""What the model runtime must refuse, and what it must always hand back."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from quantbot.research.models import (
    STRUCTURED_OUTPUT,
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
    StructuredOutputError,
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
        RoleRouting(chains={ModelRole.GENERATOR: (shared,), ModelRole.CRITIC: (shared,)})

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
    # A float, not the string this asserted until a real endpoint read it. The stored form is a
    # string because `parameters` is hashed into provenance; the wire form is what the OpenAI
    # schema says, and Ollama refuses anything else. This assertion previously encoded the
    # defect, which is what a mock transport buys you if it is the only reader you ever have.
    assert payload["temperature"] == 0.0
    assert isinstance(payload["temperature"], float)
    assert response.provenance().model == "qwen2.5-32b@1"


def test_an_unreadable_backend_response_is_an_error_not_an_empty_answer() -> None:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return "<html>502 Bad Gateway</html>"

    with pytest.raises(ModelError, match="unreadable response"):
        OpenAICompatibleTransport(post).complete(spec(), "hello", timeout_seconds=5.0)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty string"),
        pytest.param("   \n\t ", id="whitespace only"),
        pytest.param(None, id="null content"),
        pytest.param({"text": "hi"}, id="not a string"),
    ],
)
def test_a_missing_completion_is_a_failure_rather_than_a_silent_no_objection(
    content: object,
) -> None:
    """An empty answer becoming "the model had no objection" is the failure class this exists for.

    Checked at the transport, not at `ModelResponse`: the response model's non-empty constraint
    would only fire for the empty string, and would let `None` through as the literal text
    "None" -- an answer nobody wrote, attributed to a model, carrying provenance.
    """

    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps({"choices": [{"message": {"content": content}}]})

    with pytest.raises(ModelError, match="silence is not agreement"):
        OpenAICompatibleTransport(post).complete(spec(), "hello", timeout_seconds=5.0)


def test_unusable_token_accounting_is_an_error_not_a_negative_budget() -> None:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "choices": [{"message": {"content": "a real answer"}}],
                "usage": {"prompt_tokens": -5, "completion_tokens": 40},
            }
        )

    with pytest.raises(ModelError, match="negative token usage"):
        OpenAICompatibleTransport(post).complete(spec(), "hello", timeout_seconds=5.0)

    def unparseable(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "choices": [{"message": {"content": "a real answer"}}],
                "usage": "not an object",
            }
        )

    with pytest.raises(ModelError, match="unreadable response"):
        OpenAICompatibleTransport(unparseable).complete(spec(), "hello", timeout_seconds=5.0)


def test_a_spec_cannot_carry_the_credential_that_belongs_to_the_transport() -> None:
    """A spec is hashed into provenance and serialised into manifests. The transport is not."""
    with pytest.raises(ValidationError, match="shaped like a credential"):
        ModelSpec(
            provider="openai",
            model="gpt-4o",
            version="2026-08-01",
            endpoint="https://api.example/v1?api_key=sk-live-0123456789abcdef01",
            capabilities=ModelCapabilities(context_tokens=128000),
        )

    with pytest.raises(ValidationError, match="shaped like a credential"):
        ModelSpec(
            provider="openai",
            model="gpt-4o",
            version="2026-08-01",
            endpoint="https://api.example/v1",
            parameters={"api_key": "sk-live-0123456789abcdef01"},
            capabilities=ModelCapabilities(context_tokens=128000),
        )

    # An ordinary endpoint and ordinary parameters are untouched.
    assert spec().endpoint == "http://localhost:11434/v1"


def _incapable(model: str) -> ModelSpec:
    """A model that answers fine but cannot produce structured output."""
    return ModelSpec(
        provider="ollama",
        model=model,
        version="1",
        endpoint="http://localhost:11434/v1",
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=8192, structured_output=False, local=True, cost_class=CostClass.LOCAL
        ),
    )


def test_a_fallback_answer_says_that_it_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """#9: a response produced under degradation must be distinguishable from a primary one.

    The attempt list was built and thrown away on success, so a critique from a degraded backup
    read exactly like one from the model the operator chose. A result that cannot say it was
    produced under degradation cannot be weighed against one that was not.
    """
    primary, backup = spec("primary-model"), spec("backup-model", version="2")
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (primary, backup), ModelRole.CRITIC: (spec("critic"),)},
        fall_back=True,
    )
    # One failure: the primary falls over, the backup answers.
    chat = FakeChat(failures=1)
    runtime = ModelRuntime(routing, chat)

    response = runtime.call(ModelRole.GENERATOR, template(), {"claim": "x"}, now=NOW)

    assert response.spec.model == "backup-model"
    assert response.is_fallback
    assert response.chain_position == 1
    assert response.superseded, "the response names what it superseded"
    assert "primary-model" in response.superseded[0]


def test_a_primary_answer_is_not_marked_as_a_fallback() -> None:
    """The other half: if everything is a fallback, the flag says nothing."""
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (spec("primary-model"),), ModelRole.CRITIC: (spec("c"),)}
    )
    response = ModelRuntime(routing, FakeChat()).call(
        ModelRole.GENERATOR, template(), {"claim": "x"}, now=NOW
    )

    assert not response.is_fallback
    assert response.chain_position == 0
    assert response.superseded == ()


def test_a_model_that_cannot_meet_the_requirement_is_skipped_not_tried() -> None:
    """#9: capability metadata was declared and consulted nowhere.

    Routing a structured-output call to a model that cannot produce it does not fail at the
    routing layer -- it fails later as unparseable text, which reads like a bad answer rather
    than a bad route. Silent degradation is the failure mode the criterion names.
    """
    weak, strong = _incapable("no-json-model"), spec("json-model")
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (weak, strong), ModelRole.CRITIC: (spec("critic"),)},
        fall_back=True,
    )
    chat = FakeChat()
    runtime = ModelRuntime(routing, chat)

    response = runtime.call(
        ModelRole.GENERATOR, template(), {"claim": "x"}, now=NOW, needs=STRUCTURED_OUTPUT
    )

    assert response.spec.model == "json-model"
    assert "no-json-model" in response.superseded[0]
    assert "cannot structured_output" in response.superseded[0]
    # Skipped, not called: the weak model must never have been asked.
    assert "no-json-model" not in [model for model, _ in chat.calls]


def test_a_requirement_no_model_can_meet_is_refused_rather_than_downgraded() -> None:
    """Fail closed. Answering with a model that cannot do the job is worse than not answering."""
    routing = RoleRouting(
        chains={
            ModelRole.GENERATOR: (_incapable("a"), _incapable("b")),
            ModelRole.CRITIC: (spec("critic"),),
        },
        fall_back=True,
    )
    runtime = ModelRuntime(routing, FakeChat())

    with pytest.raises(ModelUnavailable, match="exhausted its chain"):
        runtime.call(
            ModelRole.GENERATOR, template(), {"claim": "x"}, now=NOW, needs=STRUCTURED_OUTPUT
        )


class Verdict(BaseModel):
    """A minimal structured answer, standing in for a real critic schema."""

    verdict: str
    confidence: float


def test_a_weak_model_cannot_be_configured_as_the_critic() -> None:
    """#9: nothing previously stopped a weak local model serving as CRITIC.

    The critic decides whether a hypothesis survives, so a weak one quietly lowers the bar for
    everything the system will ever believe. The floor is declared by the operator rather than
    invented here: there is no universal threshold for "weak", and a number this module made up
    would be applied to models it has never seen.
    """
    weak = _incapable("tiny-local")  # 8192 context, no structured output

    with pytest.raises(RoutingError, match="against a critic floor"):
        RoleRouting(
            chains={ModelRole.GENERATOR: (spec("gen"),), ModelRole.CRITIC: (weak,)},
            critic_minimum_context_tokens=32000,
        )

    with pytest.raises(RoutingError, match="cannot structured_output"):
        RoleRouting(
            chains={ModelRole.GENERATOR: (spec("gen"),), ModelRole.CRITIC: (weak,)},
            critic_requires=STRUCTURED_OUTPUT,
        )

    # A model that clears the declared bar is accepted, so the floor is a filter and not a ban.
    RoleRouting(
        chains={ModelRole.GENERATOR: (spec("gen"),), ModelRole.CRITIC: (spec("strong-critic"),)},
        critic_minimum_context_tokens=32000,
        critic_requires=STRUCTURED_OUTPUT,
    )


def test_a_floor_of_zero_is_not_silently_applied_to_the_generator() -> None:
    """The floor governs CRITIC only; a cheap generator is a legitimate configuration."""
    weak = _incapable("tiny-local")
    RoleRouting(
        chains={ModelRole.GENERATOR: (weak,), ModelRole.CRITIC: (spec("strong-critic"),)},
        critic_minimum_context_tokens=32000,
    )


def test_structured_output_is_validated_rather_than_assumed() -> None:
    """#9: `structured_output` was a declared capability that nothing ever checked the result of.

    Prose, truncated JSON and well-formed JSON of the wrong shape each produced a ModelResponse
    that looked entirely successful, leaving the caller to crash further from the cause or read
    a half-parsed answer as a real one.
    """
    routing = RoleRouting(
        chains={ModelRole.GENERATOR: (spec("json-model"),), ModelRole.CRITIC: (spec("critic"),)}
    )

    good = ModelRuntime(routing, FakeChat(answer='{"verdict": "REVISE", "confidence": 0.4}'))
    parsed, response = good.call_structured(
        ModelRole.GENERATOR, template(), {"claim": "x"}, Verdict, now=NOW
    )
    assert parsed.verdict == "REVISE"
    assert parsed.confidence == 0.4
    # The raw response travels with the parsed object: a structured answer without provenance
    # cannot say which model produced it, or whether that model was a fallback.
    assert response.spec.model == "json-model"

    prose = ModelRuntime(routing, FakeChat(answer="I think it should be revised, honestly"))
    with pytest.raises(StructuredOutputError, match="did not return JSON"):
        prose.call_structured(ModelRole.GENERATOR, template(), {"claim": "x"}, Verdict, now=NOW)

    wrong_shape = ModelRuntime(routing, FakeChat(answer='{"verdict": "REVISE"}'))
    with pytest.raises(StructuredOutputError, match="not a valid Verdict"):
        wrong_shape.call_structured(
            ModelRole.GENERATOR, template(), {"claim": "x"}, Verdict, now=NOW
        )


def test_a_structured_call_never_reaches_a_model_that_cannot_produce_json() -> None:
    """The requirement is added automatically, so the caller cannot forget it."""
    routing = RoleRouting(
        chains={
            ModelRole.GENERATOR: (_incapable("no-json"), spec("json-model")),
            ModelRole.CRITIC: (spec("critic"),),
        },
        fall_back=True,
    )
    chat = FakeChat(answer='{"verdict": "PROCEED", "confidence": 0.9}')

    _, response = ModelRuntime(routing, chat).call_structured(
        ModelRole.GENERATOR, template(), {"claim": "x"}, Verdict, now=NOW
    )

    assert response.spec.model == "json-model"
    assert "no-json" not in [model for model, _ in chat.calls]


def test_numeric_parameters_reach_the_wire_as_numbers_not_strings() -> None:
    """The defect the first real endpoint found, and that no mock could have.

    `ModelSpec.parameters` is `dict[str, str]` because it is hashed into provenance, and a
    canonical hash over mixed types changes when nothing did. But the wire format is not the
    storage format: an OpenAI-compatible server parses `temperature` as a float, and Ollama
    answers `cannot unmarshal string into Go struct field ... of type float64`. Every call
    failed, and `httpx.MockTransport` had accepted the string happily for as long as it was the
    only reader.
    """
    from quantbot.research.models import _typed_parameters

    wire = _typed_parameters({"temperature": "0", "max_tokens": "512", "stop": "###"})

    assert wire["temperature"] == 0.0
    assert isinstance(wire["temperature"], float)
    assert wire["max_tokens"] == 512
    assert isinstance(wire["max_tokens"], int)
    # Unknown names pass through untouched. A blanket "try float() on everything" would
    # eventually mangle a parameter that is genuinely a string, and silently changing a value's
    # type is how a request stops meaning what it said.
    assert wire["stop"] == "###"


def test_a_declared_numeric_parameter_that_is_not_numeric_fails_here_not_at_the_server() -> None:
    """The endpoint would reject the whole request without saying which parameter was wrong.

    And this transport deliberately does not echo provider error bodies, because they routinely
    contain part of the key that was rejected -- so a server-side rejection arrives as a bare
    status code. Failing locally is the only place the parameter name survives.
    """
    from quantbot.research.models import ModelError, _typed_parameters

    with pytest.raises(ModelError, match="temperature"):
        _typed_parameters({"temperature": "warm"})
