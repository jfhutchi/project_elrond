"""The judgment critic, and the several ways it must not be able to launder an opinion (#7, #9)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from quantbot.research.critic import (
    MECHANICAL_CHECKS,
    CriticVerdict,
    Critique,
    Objection,
    Severity,
    consensus,
)
from quantbot.research.model_critic import ModelCritic, ModelCriticRefused
from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    OpenAICompatibleTransport,
    RoleRouting,
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def spec(model: str = "critic-model") -> ModelSpec:
    return ModelSpec(
        provider="openai-compatible",
        model=model,
        version="1",
        endpoint="http://localhost:11434/v1",
        parameters={"temperature": "0"},
        capabilities=ModelCapabilities(
            context_tokens=32768,
            structured_output=True,
            local=True,
            cost_class=CostClass.LOCAL,
        ),
    )


def runtime_returning(judgment: dict[str, object]) -> ModelRuntime:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(judgment)}}],
                "usage": {"prompt_tokens": 400, "completion_tokens": 120},
            }
        )

    return ModelRuntime(
        RoleRouting(chains={ModelRole.CRITIC: (spec(),)}), OpenAICompatibleTransport(post)
    )


def describe(hypothesis_id: str, version: int) -> str:
    return f"{hypothesis_id} v{version}: a 200-day trend filter on SPY earns positive Sharpe."


def critic(judgment: dict[str, object]) -> ModelCritic:
    return ModelCritic(runtime_returning(judgment), describe)


def test_a_judgment_objection_carries_its_provenance_as_evidence_not_its_prose() -> None:
    """`Objection.evidence` is mandatory because an objection without one is an opinion.

    A model objection *is* an opinion, so putting its own reasoning in that field would dress a
    judgment as a measurement -- the exact thing this module is arranged to prevent. The honest
    evidence for a judgment is its provenance: which model said it, under which prompt. A later
    reader can weigh that, and can never mistake it for a computation.
    """
    review = critic(
        {
            "verdict": "REVISE",
            "reasons": ["the mechanism is not stated"],
            "objections": [
                {
                    "dimension": "economic mechanism plausibility",
                    "severity": "BLOCKING",
                    "finding": "no reason is given why this should be unpriced",
                }
            ],
        }
    ).review("H-1", 1, now=NOW)

    objection = review.objections[0]
    assert objection.evidence["source"] == "model-judgment"
    assert objection.evidence["model"].startswith("critic-model")
    assert objection.evidence["prompt_template_hash"]
    # Nothing in the evidence looks like a measurement.
    assert not any(value.replace(".", "").isdigit() for value in objection.evidence.values())


def test_the_model_may_not_opine_on_a_mechanical_check() -> None:
    """A model asserting something about a paired t-test produces text resembling a finding.

    The resemblance is the danger: it would sit in the same `objections` tuple as a computed
    one, with nothing downstream to tell them apart. Refused rather than downgraded.
    """
    mechanical = MECHANICAL_CHECKS[0]
    review = critic(
        {
            "verdict": "REVISE",
            "reasons": ["the statistics look wrong to me"],
            "objections": [
                {"dimension": mechanical, "severity": "BLOCKING", "finding": "seems off"}
            ],
        }
    )

    with pytest.raises(ModelCriticRefused, match="mechanical checks"):
        review.review("H-1", 1, now=NOW)


def test_every_mechanical_check_is_reported_unassessed() -> None:
    """Silence is not approval.

    A critique listing nothing as unassessed reads as full coverage from a reviewer that looked
    at five dimensions out of eleven.
    """
    review = critic(
        {"verdict": "PROCEED", "reasons": ["the mechanism is plausible"], "unassessed": []}
    ).review("H-1", 1, now=NOW)

    assert set(MECHANICAL_CHECKS) <= set(review.unassessed)


def test_a_blocking_objection_cannot_arrive_alongside_proceed() -> None:
    """A model contradicting itself is corrected upward, never downward.

    Correcting downward is how a critic gets talked out of an objection it actually raised.
    """
    review = critic(
        {
            "verdict": "PROCEED",
            "reasons": ["broadly fine"],
            "objections": [
                {
                    "dimension": "capacity and execution realism",
                    "severity": "BLOCKING",
                    "finding": "the strategy needs more liquidity than the universe has",
                }
            ],
        }
    ).review("H-1", 1, now=NOW)

    assert review.verdict is CriticVerdict.REVISE


def test_an_ungradeable_severity_refuses_the_review_rather_than_defaulting() -> None:
    """Defaulting down stops it blocking; defaulting up halts research on a formatting slip.

    A critic that cannot grade its own objection has produced a review nobody can act on, which
    is the same reason structured output is refused when it will not parse.
    """
    review = critic(
        {
            "verdict": "REVISE",
            "reasons": ["something is wrong"],
            "objections": [
                {
                    "dimension": "confounders and alternative explanations",
                    "severity": "kind of important",
                    "finding": "the 2020 crash may dominate",
                }
            ],
        }
    )

    with pytest.raises(ModelCriticRefused, match="not a severity"):
        review.review("H-1", 1, now=NOW)


def test_a_model_proceed_cannot_overrule_a_deterministic_objection() -> None:
    """The structural guarantee, asserted rather than trusted to the prompt.

    The model critic is never shown the mechanical findings, so it has nothing to overrule, and
    `consensus` takes the most severe verdict rather than the majority. There is no arrangement
    of these two critiques in which the model's approval wins.
    """
    model_says_fine = critic(
        {"verdict": "PROCEED", "reasons": ["the mechanism is plausible"]}
    ).review("H-1", 1, now=NOW)

    deterministic_blocks = Critique(
        hypothesis_id="H-1",
        hypothesis_version=1,
        critic="deterministic",
        verdict=CriticVerdict.REVISE,
        reasons=["paired-test: the comparison is two series of the same asset"],
        objections=(
            Objection(
                check="paired-test",
                severity=Severity.BLOCKING,
                finding="independent-samples assumption on paired data",
                evidence={"z": "1.01"},
            ),
        ),
        produced_at=NOW,
    )

    assert model_says_fine.verdict is CriticVerdict.PROCEED
    assert consensus([model_says_fine, deterministic_blocks]) is CriticVerdict.REVISE
    # And order does not matter, because it is severity and not precedence.
    assert consensus([deterministic_blocks, model_says_fine]) is CriticVerdict.REVISE


def test_the_critic_never_sees_the_deterministic_findings() -> None:
    """Independence is the point of having a second critic.

    A critic shown the first one's homework can restate it, and two critics agreeing then looks
    like independent confirmation of something only one of them checked. `describe` renders the
    hypothesis; there is no parameter through which a finding could arrive.
    """
    import inspect

    parameters = set(inspect.signature(ModelCritic.__init__).parameters)
    assert parameters == {"self", "runtime", "describe", "template"}

    seen: list[str] = []

    def record(hypothesis_id: str, version: int) -> str:
        seen.append(f"{hypothesis_id} v{version}")
        return describe(hypothesis_id, version)

    ModelCritic(
        runtime_returning({"verdict": "PROCEED", "reasons": ["fine"]}), record
    ).review("H-1", 1, now=NOW)

    assert seen == ["H-1 v1"]


def test_provenance_is_available_for_the_review_that_was_just_produced() -> None:
    """#18 needs the model identity and prompt hash linked to the exact hypothesis version."""
    reviewer = critic({"verdict": "PROCEED", "reasons": ["fine"]})

    assert reviewer.last_response is None, "nothing has been reviewed yet"
    reviewer.review("H-1", 1, now=NOW)

    response = reviewer.last_response
    assert response is not None
    assert response.provenance().model.startswith("critic-model")
    assert response.total_tokens == 520


def test_a_review_with_no_reasons_is_not_a_critique() -> None:
    """A verdict without reasons is a vote, and this project does not count votes.

    Refused at the schema rather than by this module, which is the right layer: `ModelJudgment`
    declares `reasons` with `min_length=1`, so an empty list never becomes a `ModelJudgment` and
    `call_structured` reports it as output that is not the requested shape. Worth a test anyway
    -- the constraint lives in a field declaration that a later edit could drop without anything
    else noticing.
    """
    reviewer = critic({"verdict": "PROCEED", "reasons": []})

    with pytest.raises(Exception, match="not a valid ModelJudgment"):
        reviewer.review("H-1", 1, now=NOW)
