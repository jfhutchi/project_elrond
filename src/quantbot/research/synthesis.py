"""Answering "what has Elrond learned about X" with evidence rather than narrative (#6, #9).

CLAUDE.md sets this module's acceptance test directly: a future agent asking *"what has Elrond
learned about momentum?"* should receive evidence-linked project knowledge rather than regenerated
narrative. `ResearchMemory.recall` returns the matching records; nothing turned them into an
answer.

Synthesis is where a model is most tempting and most dangerous. A summary that drifts from its
sources is a narrative wearing citations, which is precisely the thing this project's institutional
memory exists to prevent -- and it is worse than no summary at all, because a cited claim reads as
checked.

So the guarantee is mechanical rather than prompted:

**Every claim must cite record ids, and every cited id must be one that was supplied.** A claim
citing an id outside the input set is refused, and so is the whole synthesis. That is checkable
without reading a word of the prose, which is the point -- it does not depend on anyone judging
whether the summary was faithful.

**The result is a `SUMMARY`, which cannot carry a verdict.** `ResearchRecord` enforces that: a
summary is derived, not evidence. It can be deleted without losing anything, by construction, and
that is a property worth keeping rather than an accident.

**Nothing is synthesised from nothing.** An empty recall raises rather than producing "no evidence
was found about X", which is a sentence a later reader would quote as though somebody had looked.

**Model provenance travels with it.** A lesson is the most re-quotable artifact this system
produces, so the record carries which model wrote it under which prompt, and a caller can persist
that alongside.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord
from quantbot.research.models import ModelResponse, ModelRole, ModelRuntime, PromptTemplate

SYNTHESIS_PROMPT = PromptTemplate(
    name="lesson-synthesis",
    version="1",
    text="""Below are research records this project has produced about "{topic}". Each is tagged
with an id.

{records}

Summarise what has been learned. Rules:

- Every claim must cite the ids it rests on. A claim you cannot attribute to a listed id must
  not appear at all.
- Do not add context, mechanism or interpretation that is not in the records. You have not seen
  the underlying data and cannot judge it.
- Refuted results are findings. Say what was ruled out, not only what survived.
- If the records disagree, say so rather than reconciling them.

Return JSON with keys: claims (list of objects with "statement" and "cites", a list of ids), and
"open_questions" (list of strings).""",
)


class SynthesisRefused(RuntimeError):
    """Raised when a synthesis cannot be accepted as traceable to its sources."""


class Claim(BaseModel):
    model_config = ConfigDict(extra="ignore")

    statement: str = Field(min_length=1)
    cites: list[str] = Field(min_length=1)


class Synthesis(BaseModel):
    """The shape a model must answer in. Validated, never parsed hopefully."""

    model_config = ConfigDict(extra="ignore")

    claims: list[Claim] = Field(min_length=1)
    open_questions: list[str] = Field(default_factory=list)


def synthesize_lessons(
    runtime: ModelRuntime,
    memory: ResearchMemory,
    topic: str,
    *,
    record_id: str,
    now: datetime,
) -> tuple[ResearchRecord, ModelResponse]:
    """Turn what memory holds about `topic` into one citable summary record.

    Returns the record and the model response, so a caller can persist the provenance beside the
    lesson. The record is not written here: this module reads memory and must not also decide
    what enters it, or a synthesis could cite a synthesis and the chain of evidence would close
    on itself.
    """
    if not topic.strip():
        raise SynthesisRefused("a synthesis needs a topic; summarising everything is not a lesson")

    # Summaries are excluded from the input. A synthesis of syntheses drifts one step further
    # from the evidence at every round, and cites something that was itself uncited.
    sources = [
        record
        for record in memory.recall(topic)
        if record.kind is not RecordKind.SUMMARY
    ]
    if not sources:
        raise SynthesisRefused(
            f"memory holds no records about {topic!r}. Returning a summary saying so would be a "
            "sentence a later reader quotes as though somebody had looked"
        )

    known = {record.record_id for record in sources}
    rendered = "\n".join(
        f"[{record.record_id}] ({record.kind.value}"
        + (f"/{record.verdict.value}" if record.verdict is not None else "")
        + f") {record.subject}: {record.statement}"
        for record in sources
    )

    synthesis, response = runtime.call_structured(
        ModelRole.SUMMARIZER,
        SYNTHESIS_PROMPT,
        {"topic": topic, "records": rendered},
        Synthesis,
        now=now,
    )

    for claim in synthesis.claims:
        invented = [item for item in claim.cites if item not in known]
        if invented:
            raise SynthesisRefused(
                f"a claim cites {', '.join(invented)}, which was not among the records supplied. "
                "A citation to something nobody provided is the failure this check exists for, "
                "and one is enough to distrust the rest"
            )

    statement = "\n".join(
        f"{claim.statement} [{', '.join(claim.cites)}]" for claim in synthesis.claims
    )
    if synthesis.open_questions:
        statement += "\nOpen: " + "; ".join(synthesis.open_questions)

    lesson = ResearchRecord(
        record_id=record_id,
        kind=RecordKind.SUMMARY,
        # No verdict, enforced by `ResearchRecord`. A summary is derived, not evidence.
        subject=topic,
        statement=statement,
        source=f"synthesis over {len(sources)} records by {response.spec.model}",
        recorded_at=now,
        detail={
            "records": ", ".join(sorted(known)),
            "model": response.provenance().model,
            "prompt_template_hash": response.provenance().prompt_template_hash,
        },
    )
    return lesson, response


__all__ = [
    "SYNTHESIS_PROMPT",
    "Claim",
    "Synthesis",
    "SynthesisRefused",
    "synthesize_lessons",
]
