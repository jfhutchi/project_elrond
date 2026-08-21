"""The judgment half of the critic, which is a model and therefore the half to distrust (#7, #9).

`DeterministicCritic` runs the mechanical checks and abstains loudly from everything else,
listing `JUDGMENT_DIMENSIONS` as unassessed. This fills that gap, and every design choice here is
about making sure it cannot fill it dishonestly.

**It cannot overrule the arithmetic, and not because the prompt asks it not to.** A prompt
instructing a critic to defer to mechanical findings is not a control -- it is a request, and the
model is free to decline. The guarantee is structural instead: this critic is never shown the
deterministic objections, so it has nothing to overrule, and `consensus()` takes the most severe
verdict rather than the majority one. A model PROCEED against a deterministic REVISE is a REVISE,
and there is no configuration in which it is not.

**It is never shown the mechanical findings, for a second reason too.** A critic that could see
them might simply restate them, and two critics agreeing would then look like independent
confirmation of something only one of them checked. Independence is the entire reason for having
a second critic; showing it the first one's homework destroys that quietly.

**It may not opine on mechanical dimensions at all.** An objection naming one of
`MECHANICAL_CHECKS` is refused rather than downgraded. A model asserting something about a
paired-versus-unpaired t-test is producing text that resembles a statistical finding, and the
resemblance is the danger: it would sit in the same `objections` tuple as a computed one, and
nothing downstream distinguishes them.

**Silence is not approval.** `MECHANICAL_CHECKS` always appear in `unassessed`, because this
critic did not assess them. A critique that listed nothing as unassessed would read as full
coverage from a reviewer that examined five dimensions out of eleven.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from quantbot.research.critic import (
    JUDGMENT_DIMENSIONS,
    MECHANICAL_CHECKS,
    CriticVerdict,
    Critique,
    Objection,
    Severity,
)
from quantbot.research.manifest import ModelProvenance
from quantbot.research.models import ModelResponse, ModelRole, ModelRuntime, PromptTemplate

CRITIC_PROMPT = PromptTemplate(
    name="adversarial-critic",
    version="3",
    text="""You are reviewing a pre-registered trading research hypothesis. Your job is to find
reasons it is wrong, not reasons it is interesting.

Assess ONLY these dimensions:
{dimensions}

You have NOT been given the statistical checks, and you must not comment on statistics, test
choice, sample size, multiple testing, look-ahead, or costs. Those are computed elsewhere and
an opinion about them from you would be indistinguishable from a measurement.

Hypothesis:
{hypothesis}

Return only JSON, with these keys:

- "verdict": copy exactly one of these three strings, character for character. Do not
  substitute a synonym. The only accepted values are:
    "PROCEED"  - no objection that should stop this
    "REVISE"   - fixable problems; say what they are
    "REJECT"   - the idea does not survive scrutiny
- "reasons": list of strings, at least one.
- "objections": list of objects. Include an entry ONLY for a dimension where you have a
  specific, concrete problem to state. Do not grade every dimension. Each entry needs all
  three of:
    "dimension" - which of the dimensions above
    "severity"  - exactly "BLOCKING" or "ADVISORY"
    "finding"   - one or two sentences saying what is actually wrong. An objection without
                  this is a label nobody can act on or dismiss, and it will be rejected.
- "falsification_tests": list of strings.
- "unassessed": list of the dimensions above you could not judge from what you were given. Say
  so here rather than guessing.""",
)


class ModelJudgment(BaseModel):
    """The shape a model must answer in. Validated, never parsed hopefully."""

    model_config = ConfigDict(extra="ignore")

    verdict: CriticVerdict
    reasons: list[str] = Field(min_length=1)
    objections: list[dict[str, str]] = Field(default_factory=list)
    falsification_tests: list[str] = Field(default_factory=list)
    unassessed: list[str] = Field(default_factory=list)


class ModelCriticRefused(RuntimeError):
    """Raised when a model's review cannot be accepted as a critique."""


class ModelCritic:
    """Satisfies `Critic`, so it drops into the gate beside the deterministic one.

    `describe` renders the hypothesis for the prompt. Injected because this module must not read
    the registry: a critic that could load a registration could load its power assessment and
    its consumed windows, and would then be reviewing the gate's own workings rather than the
    idea. The caller that already holds the draft supplies the text.
    """

    name = "model"

    def __init__(
        self,
        runtime: ModelRuntime,
        describe: Callable[[str, int], str],
        *,
        template: PromptTemplate = CRITIC_PROMPT,
    ) -> None:
        self._runtime = runtime
        self._describe = describe
        self._template = template
        self._last: ModelResponse | None = None

    @property
    def last_response(self) -> ModelResponse | None:
        """Provenance for the most recent review, so #18 can persist what produced it.

        Held rather than returned because `Critic.review` returns a `Critique` and widening that
        signature would change the gate for every critic to suit this one. A caller that wants
        the provenance asks for it; one that does not still gets the same critique.
        """
        return self._last

    def review(self, hypothesis_id: str, version: int, *, now: datetime) -> Critique:
        judgment, response = self._runtime.call_structured(
            ModelRole.CRITIC,
            self._template,
            {
                "dimensions": "\n".join(f"- {item}" for item in JUDGMENT_DIMENSIONS),
                "hypothesis": self._describe(hypothesis_id, version),
            },
            ModelJudgment,
            now=now,
        )
        self._last = response

        provenance = response.provenance()
        objections = tuple(_objection(raw, provenance) for raw in judgment.objections)
        mechanical = [item.check for item in objections if item.check in MECHANICAL_CHECKS]
        if mechanical:
            raise ModelCriticRefused(
                f"the model returned objections on mechanical checks ({', '.join(mechanical)}); "
                "those are computed, and a model's opinion about one would sit in the same "
                "tuple as a measurement with nothing downstream to tell them apart"
            )

        blocking = any(item.severity is Severity.BLOCKING for item in objections)
        # A model that raises a blocking objection and still says PROCEED has contradicted
        # itself. `Critique` refuses that pairing, so the verdict is corrected upward here
        # rather than letting the whole review fail on a formatting inconsistency -- upward
        # only, because correcting downward is how a critic gets talked out of an objection.
        verdict = (
            CriticVerdict.REVISE
            if blocking and judgment.verdict is CriticVerdict.PROCEED
            else judgment.verdict
        )

        return Critique(
            hypothesis_id=hypothesis_id,
            hypothesis_version=version,
            critic=self.name,
            verdict=verdict,
            reasons=tuple(judgment.reasons),
            objections=objections,
            falsification_tests=tuple(judgment.falsification_tests),
            # Whatever the model admits it could not judge, plus every mechanical check, which
            # it was never asked about and must not appear to have cleared.
            unassessed=tuple(judgment.unassessed) + MECHANICAL_CHECKS,
            produced_at=now,
        )


def _objection(raw: dict[str, str], provenance: ModelProvenance) -> Objection:
    """Turn one reported objection into a typed one, refusing anything malformed.

    **What goes in `evidence` is the interesting decision.** That field is mandatory because, as
    `Objection` puts it, an objection without one is an opinion -- and a model objection *is* an
    opinion. Putting the model's own prose there would dress a judgment as a measurement, which
    is the one thing this whole module is arranged to prevent.

    So the evidence for a judgment objection is its provenance: which model said it, under which
    prompt. That is genuinely what a later reader needs in order to weigh it, and it can never
    be mistaken for a computation the way a plausible-sounding number would be.

    An unparseable severity refuses the whole critique rather than defaulting. Defaulting down
    is how an objection quietly stops blocking anything; defaulting up makes every malformatted
    response halt research. A critic that cannot grade its own objection has produced a review
    nobody can act on, which is the same reason `call_structured` refuses unparseable output.
    """
    check = str(raw.get("dimension", "")).strip()
    finding = str(raw.get("finding", "")).strip()
    if not check or not finding:
        raise ModelCriticRefused(
            "a model objection must name a dimension and state a finding; an unexplained "
            "objection cannot be acted on and cannot be dismissed"
        )
    try:
        severity = Severity(str(raw.get("severity", "")).strip().upper())
    except ValueError as error:
        raise ModelCriticRefused(
            f"{raw.get('severity')!r} is not a severity; a critic that cannot grade its own "
            f"objection has produced a review nobody can act on. Expected one of "
            f"{', '.join(item.value for item in Severity)}"
        ) from error
    return Objection(
        check=check,
        severity=severity,
        finding=finding,
        evidence={
            "source": "model-judgment",
            "model": provenance.model,
            "prompt_template_hash": provenance.prompt_template_hash,
        },
    )


__all__ = [
    "CRITIC_PROMPT",
    "ModelCritic",
    "ModelCriticRefused",
    "ModelJudgment",
]
