"""A model's answer depends on its preceding calls, so an artifact must record which it was.

Measured 2026-08-30 against local Ollama with `mistral:7b` at `temperature=0`, one fixed prompt,
one fixed source set. The same call issued to a freshly loaded model and issued after a single
unrelated completion returns *different* analyses -- 26-37% of the confounders and falsification
tests differ. Both conditions are individually reproducible; the difference between them is not
visible in anything the artifact records.

These tests cover the provenance field that makes it visible. They deliberately do not assert
anything about model behaviour: that is a measurement, recorded in `REFUTED.md`, and a unit test
that pretended to verify it would be asserting against a stub.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from quantbot.research.models import (
    ModelCapabilities,
    ModelResponse,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    PromptTemplate,
    RoleRouting,
)

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
TEMPLATE = PromptTemplate(name="probe", version="1", text="analyse {subject}")


def spec(model: str = "mistral:7b") -> ModelSpec:
    return ModelSpec(
        provider="openai-compatible",
        model=model,
        version="configured",
        endpoint="http://localhost:11434/v1",
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(context_tokens=8192, structured_output=True),
    )


class CountingTransport:
    """Returns a different answer each call, as a real backend under session drift would."""

    def __init__(self) -> None:
        self.calls = 0

    def complete(
        self, spec: ModelSpec, prompt: str, *, timeout_seconds: float
    ) -> tuple[str, int, int]:
        self.calls += 1
        return f"answer {self.calls}", 10, 10


def runtime(critic: str = "mistral:7b") -> tuple[ModelRuntime, CountingTransport]:
    """A routing table where the critic is a distinct model, as `RoleRouting` insists.

    Not a workaround: the rule exists because a model asked to review its own proposal tends to
    find it sound, and a fixture that sidestepped it would be testing a configuration the system
    refuses to run.
    """
    transport = CountingTransport()
    routing = RoleRouting(
        chains={
            role: (spec(critic),) if role is ModelRole.CRITIC else (spec("llama3.1:8b"),)
            for role in ModelRole
        },
        critic_minimum_context_tokens=8192,
    )
    return ModelRuntime(routing, transport), transport


def test_the_first_call_to_a_model_records_position_zero() -> None:
    """The condition that is reproducible on its own, and distinguishable only if recorded."""
    engine, _ = runtime()

    response = engine.call(ModelRole.CRITIC, TEMPLATE, {"subject": "x"}, now=NOW)

    assert response.session_call_index == 0


def test_successive_calls_record_their_position() -> None:
    """Without this, a warm repeat and a cold call produce artifacts that look identical.

    They are not identical: the measurement in the module docstring found 26-37% of the returned
    claims differ between those two conditions.
    """
    engine, _ = runtime()

    positions = [
        engine.call(ModelRole.CRITIC, TEMPLATE, {"subject": "x"}, now=NOW).session_call_index
        for _ in range(3)
    ]

    assert positions == [0, 1, 2]


def test_the_count_is_per_model_rather_than_per_role() -> None:
    """Two roles pointing at one backend share its session state.

    Counting per role would report 0 for a call that was in fact the second one that model had
    seen -- an artifact claiming a cold-start position it did not have, which is worse than no
    field at all.
    """
    engine, _ = runtime()

    # Both roles routed to the same backend for this test: SUMMARIZER and GENERATOR share
    # `llama3.1:8b`, which is exactly the case a per-role counter would misreport.
    first = engine.call(ModelRole.GENERATOR, TEMPLATE, {"subject": "x"}, now=NOW)
    second = engine.call(ModelRole.SUMMARIZER, TEMPLATE, {"subject": "x"}, now=NOW)

    assert first.spec.model == second.spec.model, "the premise of this test"
    assert (first.session_call_index, second.session_call_index) == (0, 1)


def test_two_models_are_counted_separately() -> None:
    """A second model's first call is a first call, whatever the other one has been doing.

    Same runtime, two backends: the critic has already answered once, and the generator's first
    call must still read as position 0.
    """
    engine, _ = runtime()
    engine.call(ModelRole.CRITIC, TEMPLATE, {"subject": "x"}, now=NOW)

    response = engine.call(ModelRole.GENERATOR, TEMPLATE, {"subject": "x"}, now=NOW)

    assert response.session_call_index == 0


def test_a_fresh_runtime_starts_over() -> None:
    """The counter describes one process's session, which is the scope the effect has.

    A number that persisted across processes would be describing something else entirely, and
    would say a call was warm when the model that answered it had just loaded.
    """
    engine, _ = runtime()
    for _ in range(4):
        engine.call(ModelRole.CRITIC, TEMPLATE, {"subject": "x"}, now=NOW)

    restarted, _ = runtime()
    response = restarted.call(ModelRole.CRITIC, TEMPLATE, {"subject": "x"}, now=NOW)

    assert response.session_call_index == 0


def test_the_position_cannot_be_negative() -> None:
    """A negative position would be a claim about calls that did not happen."""
    with pytest.raises(ValueError):
        ModelResponse(
            spec=spec(),
            template=TEMPLATE,
            prompt="analyse x",
            text="answer",
            prompt_tokens=1,
            completion_tokens=1,
            produced_at=NOW,
            session_call_index=-1,
        )
