"""One complete research role against a real local endpoint (#9).

Every other model test in this project runs through `httpx.MockTransport`, and a mock accepts
whatever it is handed. That is how `{"temperature": "0"}` -- a *string* where the OpenAI schema
wants a float -- survived: the request looked correct for as long as nothing strict read it.
The first real endpoint answered `cannot unmarshal string into Go struct field ... of type
float64` and every call failed.

So this file exists to be the strict reader. It skips loudly when no endpoint is configured,
because a skipped integration test and a passing one must not look alike -- that is the same
rule the WSL sandbox suite follows and for the same reason.

Nothing here asserts anything about the *quality* of a local model's answer. A test that
required a particular completion would be testing the weights, and would start failing when the
operator pulled a better model. What is asserted is the contract: a role resolves, a real
request is accepted, a non-empty answer comes back, and provenance is produced rather than
reconstructed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx
import pytest

from quantbot.research.composition import CRITIC, ENDPOINT, GENERATOR, build_model_runtime
from quantbot.research.models import ModelRole, PromptTemplate


def _endpoint_live() -> bool:
    """Whether an endpoint is both configured and actually answering.

    Configuration alone is not enough. An `QUANTBOT_MODEL_ENDPOINT` pointing at a server nobody
    started would make this suite fail rather than skip, and a red suite that means "Ollama is
    not running" trains people to ignore red.
    """
    base = os.environ.get(ENDPOINT, "").strip()
    if not base or not os.environ.get(GENERATOR, "").strip():
        return False
    try:
        response = httpx.get(f"{base.rstrip('/')}/models", timeout=5)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


pytestmark = pytest.mark.skipif(
    not _endpoint_live(),
    reason=f"set {ENDPOINT}, {GENERATOR} and {CRITIC} and start the server to run this",
)


def test_a_research_role_completes_against_the_real_endpoint() -> None:
    """The box that stayed open on #9 for weeks: no real endpoint had ever been contacted."""
    runtime = build_model_runtime()
    template = PromptTemplate(name="live-smoke", version="1", text="Answer in one word: {q}")

    answer = runtime.call(
        ModelRole.GENERATOR, template, {"q": "what is two plus two"}, now=datetime.now(UTC)
    )

    assert answer.text.strip(), "an empty completion is not an answer"
    assert answer.is_fallback is False, "the primary model should have served this"
    assert answer.total_tokens > 0, "a real call reports real usage"
    assert answer.response_hash


def test_provenance_comes_back_with_the_answer_and_not_afterwards() -> None:
    """#18 needs the identity and prompt hash at the moment of the call.

    A conclusion whose reasoning came from a model that has since been updated is not
    reproducible, and without these there is no way to know that happened. Reconstructing them
    later would reconstruct whatever is configured now, which is precisely the wrong answer.
    """
    runtime = build_model_runtime()
    template = PromptTemplate(name="live-smoke", version="1", text="Answer in one word: {q}")

    answer = runtime.call(
        ModelRole.GENERATOR, template, {"q": "what is two plus two"}, now=datetime.now(UTC)
    )
    provenance = answer.provenance()

    assert provenance.model.startswith(answer.spec.model)
    assert provenance.prompt_template_hash == template.template_hash
    assert provenance.parameters_hash == answer.spec.parameters_hash
    assert provenance.provider


def test_the_generator_and_critic_resolve_to_different_models() -> None:
    """The routing rule that is not configuration, checked against the live table.

    A model asked to critique its own proposal tends to find it sound. This is enforced in
    `RoleRouting`, and asserting it here as well is cheap insurance that the *configured*
    deployment has not quietly collapsed both roles onto whatever single model is installed.
    """
    runtime = build_model_runtime()

    generator = runtime.resolve(ModelRole.GENERATOR, now=datetime.now(UTC))
    critic = runtime.resolve(ModelRole.CRITIC, now=datetime.now(UTC))

    assert generator.identity != critic.identity
