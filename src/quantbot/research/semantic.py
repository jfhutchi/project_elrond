"""Semantic research that proposes what could be wrong, not what to buy (#37).

TradingAgents contributes fundamental, news, sentiment and macro analysis, a bull and a bear
researcher, and a synthesis. The issue lists its outputs, and two of them are worth far more to
Elrond than the rest: **proposed confounders** and **falsification conditions**.

The deterministic critic is strong at mechanical checks and has no way to ask *"what else could
explain this?"* or *"what would we see if this were false?"*. Those need judgement, they are
exactly what CLAUDE.md reserves for LLM critics, and -- usefully -- they are far less
hindsight-contaminated than a directional call. A proposed confounder is a hypothesis for Elrond
to test. A BUY is a conclusion Elrond did not reach.

So this worker deliberately **cannot** return a directional call. There is no field for one.
`TradingAgents BUY/HOLD/SELL outputs are never execution instructions` is easy to write in a
policy and easy to erode; a schema with no such field cannot erode.

**Contaminated sources are refused, not marked.** A model reading text published before its
training cutoff already knows how the story ended (#45), and a bear argument constructed against
a 2019 thesis by a model that knows 2020 is not an argument that could have been made. Confirmatory
semantic research runs on post-cutoff text or it does not run.

**Disagreement is preserved rather than resolved.** A bull and a bear who reach the same
conclusion have told you less than two who do not, and a synthesis that averages them away
destroys the only part a critic can act on. Unresolved disagreement is a first-class output.

**Search cardinality is one per call and countable.** Each analysis is one configuration --
one prompt, one model, one source bundle. A caller that sweeps prompts and keeps the best answer
has searched as many times as it swept, and `record_analysis` writes a durable row per call so
the burden is counted from records rather than disclosed, as #23 requires.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from quantbot.research.hindsight import HindsightAssessment, require_no_hindsight
from quantbot.research.manifest import content_id
from quantbot.research.models import ModelResponse, ModelRole, ModelRuntime, PromptTemplate
from quantbot.research.sources import Source
from quantbot.research.workers import (
    WorkerCapability,
    WorkerInput,
    WorkerResult,
    WorkerSpec,
)

ANALYSIS_PROMPT = PromptTemplate(
    name="semantic-falsification",
    version="1",
    text="""You are reviewing a proposed market hypothesis against source material.

Hypothesis: {hypothesis}

Sources (all published after your training cutoff, so you have no outcome knowledge):
{sources}

Do NOT say whether to buy, sell or hold. That is not your role and an answer containing one
will be discarded.

Return JSON with:
- "confounders": things other than the proposed mechanism that could produce the same result.
- "falsification_tests": specific observations that would show the hypothesis is false.
- "disagreements": points where the sources conflict, or where a reasonable bull and bear
  would disagree. Say what each side would claim. Do not resolve them.
- "unsupported_claims": statements in the hypothesis the sources do not support.""",
)

#: Words that indicate a directional call leaked into a field that must not carry one. Checked
#: because a schema without a field is not a schema a model cannot smuggle a call into -- it will
#: put "this is a strong buy" in a confounder if nothing looks.
DIRECTIONAL_TERMS = ("buy", "sell", "hold", "long position", "short position", "overweight")


class SemanticRefused(RuntimeError):
    """Raised when an analysis cannot be accepted as research input."""


class SemanticAnalysis(BaseModel):
    """What the worker may return. Note what is absent."""

    model_config = ConfigDict(extra="ignore")

    confounders: list[str] = Field(default_factory=list)
    falsification_tests: list[str] = Field(default_factory=list)
    disagreements: list[str] = Field(default_factory=list)
    unsupported_claims: list[str] = Field(default_factory=list)

    @property
    def useful(self) -> bool:
        """Whether this contributed anything a critic can act on.

        An analysis with no confounders and no falsification tests has produced prose. It is not
        an error -- a model may genuinely have nothing to add -- but it must not be recorded as
        research input, because a stored empty analysis reads later as "this was reviewed".
        """
        return bool(self.confounders or self.falsification_tests)


class SemanticWorker:
    """Turns post-cutoff source material into things Elrond can test.

    Holds a runtime and nothing else. It cannot reach the registry, the ledger or the broker:
    a semantic worker that could read a registration could read its power assessment and its
    consumed windows, and would be reviewing the gate rather than the idea.
    """

    name = "semantic"

    def __init__(
        self,
        runtime: ModelRuntime,
        *,
        model_cutoff: date | None,
        cutoff_source: str = "",
    ) -> None:
        self._runtime = runtime
        self._cutoff = model_cutoff
        self._cutoff_source = cutoff_source

    def analyse(
        self,
        hypothesis: str,
        sources: Sequence[Source],
        *,
        now: datetime,
    ) -> tuple[SemanticAnalysis, HindsightAssessment, ModelResponse]:
        """Analyse sources against a hypothesis, or refuse.

        Returns the analysis, the hindsight assessment that permitted it, and the model response.
        All three travel together because a caller storing the analysis without the assessment
        would be storing a claim without the reason it is allowed to be a claim.
        """
        if not hypothesis.strip():
            raise SemanticRefused("a semantic analysis needs a hypothesis to review")
        if not sources:
            raise SemanticRefused(
                "a semantic analysis needs sources; reviewing nothing produces prose that reads "
                "later as though somebody had looked"
            )

        # Refused rather than marked. This is the confirmatory path; a caller that wants an
        # exploratory read of older text uses `assess_hindsight` and labels the result itself.
        assessment = require_no_hindsight(
            sources,
            model=self._runtime.resolve(ModelRole.CRITIC, now=now).model,
            cutoff=self._cutoff,
            cutoff_source=self._cutoff_source,
        )

        rendered = "\n".join(
            f"[{source.source_id}] {source.published_at.date().isoformat()} {source.title}"
            for source in sources
        )
        analysis, response = self._runtime.call_structured(
            ModelRole.CRITIC,
            ANALYSIS_PROMPT,
            {"hypothesis": hypothesis, "sources": rendered},
            SemanticAnalysis,
            now=now,
        )

        leaked = _directional(analysis)
        if leaked:
            raise SemanticRefused(
                f"the analysis contains a directional call: {leaked!r}. This worker contributes "
                "confounders and falsification tests; a recommendation is a conclusion Elrond "
                "did not reach"
            )
        if not analysis.useful:
            raise SemanticRefused(
                "the analysis proposed no confounders and no falsification tests; storing it "
                "would read later as though the hypothesis had been reviewed"
            )
        return analysis, assessment, response


class SemanticResearchWorker:
    """`SemanticWorker` behind the generic worker boundary (#11, #37).

    The issue asks that TradingAgents be invoked through `ResearchWorker` rather than through a
    bespoke path, and the reason is not tidiness: orchestration, budget admission and search
    accounting are written against that contract, so a worker reached any other way is a worker
    none of them can see.

    **Cardinality is one per call, disclosed.** That makes the spec `CONFIRMATORY_ELIGIBLE`, and
    it is worth being precise about what that does and does not mean. It means the burden this
    worker creates is accountable and gets charged to the family's trial budget -- an
    `EXPLORATORY_ONLY` worker spends nothing, which for an agent that sweeps prompts would be
    the laundering path #23 closed for miners, reopened. It does **not** mean the analysis is
    evidence. Whether a semantic output can support a confirmatory claim is decided by the
    hindsight assessment and by the fact that its outputs are confounders and falsification
    tests -- questions for Elrond to answer, not answers.

    The worker is handed sources by a loader rather than fetching them. A research worker that
    goes looking for its own inputs decides its own point-in-time boundary, and the boundary is
    the thing #45 exists to enforce.
    """

    def __init__(
        self,
        worker: SemanticWorker,
        source_loader: Callable[[WorkerInput], Sequence[Source]],
        *,
        version: str,
        configuration_hash: str,
    ) -> None:
        self._worker = worker
        self._load = source_loader
        self._spec = WorkerSpec(
            name="semantic",
            version=version,
            capabilities=frozenset({WorkerCapability.SEMANTIC_ANALYSIS}),
            configuration_hash=configuration_hash,
            discloses_search_cardinality=True,
        )

    @property
    def spec(self) -> WorkerSpec:
        return self._spec

    def run(self, request: WorkerInput, *, now: datetime) -> WorkerResult:
        """Analyse the mandate against loaded sources, or raise.

        Never returns a partial result. A refusal -- contaminated sources, a leaked directional
        call, an analysis with nothing to add -- propagates, because a `WorkerResult` recording
        an analysis that was rejected would read later as though the hypothesis had been
        reviewed.
        """
        self._spec.require(WorkerCapability.SEMANTIC_ANALYSIS)
        started_at = now
        sources = list(self._load(request))
        analysis, assessment, response = self._worker.analyse(request.mandate, sources, now=now)
        return WorkerResult(
            worker=self._spec,
            request=request,
            started_at=started_at,
            finished_at=now,
            # One prompt, one model, one source bundle: one candidate evaluated. A caller
            # sweeping prompts calls this once per sweep and is charged once per sweep.
            search_cardinality=1,
            metrics={
                "confounders": str(len(analysis.confounders)),
                "falsification_tests": str(len(analysis.falsification_tests)),
                "preserved_disagreements": str(len(analysis.disagreements)),
                "unsupported_claims": str(len(analysis.unsupported_claims)),
                "sources_reviewed": str(len(sources)),
            },
            artifacts={"analysis.json": content_id(analysis.model_dump(mode="json"))},
            # What the worker could not express, which forces the result exploratory. A model
            # with no recorded cutoff is exactly that case: the analysis may be fine and nothing
            # can establish that it is.
            unsupported=() if assessment.usable_as_evidence else ("hindsight_unverified",),
        )


def _directional(analysis: SemanticAnalysis) -> str | None:
    """Find a directional call that leaked into a field not meant to carry one.

    A schema without a recommendation field is not a schema a model cannot smuggle one into. It
    will write "this is a strong buy" in a confounder if nothing looks, and a downstream reader
    skimming confounders would take it as one.
    """
    for field in (
        analysis.confounders,
        analysis.falsification_tests,
        analysis.disagreements,
        analysis.unsupported_claims,
    ):
        for entry in field:
            lowered = entry.lower()
            for term in DIRECTIONAL_TERMS:
                # Word-boundary-ish: "buy" must not match "buyer" or "buyback", both of which
                # appear in honest fundamental analysis.
                if f" {term} " in f" {lowered} " or lowered.startswith(f"{term} "):
                    return entry
    return None


def analysis_record(
    analysis: SemanticAnalysis,
    assessment: HindsightAssessment,
    response: ModelResponse,
) -> dict[str, str]:
    """What a durable record of one analysis holds (#18, #23).

    One row per call, so the burden is counted from records rather than disclosed. A caller that
    sweeps prompts and keeps the best answer has searched as many times as it swept, and each
    sweep leaves a row.
    """
    provenance = response.provenance()
    record = {
        "confounders": json.dumps(analysis.confounders),
        "falsification_tests": json.dumps(analysis.falsification_tests),
        "disagreements": json.dumps(analysis.disagreements),
        "unsupported_claims": json.dumps(analysis.unsupported_claims),
        "model": provenance.model,
        "prompt_template_hash": provenance.prompt_template_hash,
        "sources": ", ".join(sorted(assessment.clean)),
    }
    record.update(assessment.manifest_entry())
    return record


__all__ = [
    "ANALYSIS_PROMPT",
    "DIRECTIONAL_TERMS",
    "SemanticAnalysis",
    "SemanticRefused",
    "SemanticResearchWorker",
    "SemanticWorker",
    "analysis_record",
]
