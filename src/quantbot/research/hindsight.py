"""A pretrained model reading old text already knows how the story ended (#37, #44, #9).

Ask a model to analyse a 2019 filing and it answers with knowledge of 2020. It knows which banks
failed, which theses were vindicated, which narratives held. Ask a bear researcher to argue
against a 2019 position and the argument it constructs is not one that could have been made in
2019 -- it is a model that already knows the answer reasoning toward it.

**This does not appear in the output.** The model does not announce it, no downstream check
detects it, and the result does not look wrong. It looks like an unusually good semantic signal,
because it is one -- about the past. A sentiment backtest over 2016-2024 run by a model trained
through 2024 is measuring hindsight, and it will produce an impressive number.

It is the same class as the FRED release-hour defect, which marked every winter macro release
knowable thirty minutes early, and the generated-signal causality bug, where an LLM computed its
moving average from the final bar of the series. This one is larger than both: those leaked hours
and a series, this leaks years across an entire corpus, through the component whose entire job is
interpretation.

**The rule, and why it is a type rather than a convention.**

A model may read text published after its training cutoff without qualification -- it cannot know
what it has not seen. Text published before the cutoff is not forbidden, because there are honest
uses (summarising a known document, extracting a stated figure); it is *marked*, and anything
resting on it is `EXPLORATORY_ONLY` and cannot become confirmatory evidence.

The consequence is deliberate and worth stating plainly: **historical semantic backtesting is
largely unavailable.** That is the correct answer rather than a limitation to engineer around.
Better to establish it before building a pipeline than after it produces a result nobody can
explain away.

**Absent is not exempt.** A model whose cutoff nobody recorded is treated as contaminating
everything, not as clean. The safe direction is the one where a missing fact costs evidentiary
weight rather than granting it, and a cutoff is a fact somebody has to look up once per model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from quantbot.research.sources import Source


class HindsightRefused(RuntimeError):
    """Raised when an analysis would rest on text the model already knew the outcome of."""


@dataclass(frozen=True, slots=True)
class HindsightAssessment:
    """Which sources a model could not have known, and which it could.

    Both halves travel together. Knowing that two sources are contaminated is only useful
    alongside how many were checked -- two of three is a different fact from two of two hundred,
    and a report that gave only the numerator would invite reading a large clean corpus and a
    tiny dirty one as the same result.
    """

    model: str
    cutoff: date | None
    #: Where the cutoff came from, carried so a manifest records whether the boundary was
    #: documented or estimated. An estimated cutoff can be wrong in the unsafe direction --
    #: guessed early, and text believed post-cutoff was in training after all.
    cutoff_source: str
    contaminated: tuple[str, ...]
    clean: tuple[str, ...]

    @property
    def usable_as_evidence(self) -> bool:
        """True only when a cutoff is known and nothing predates it.

        Three things must hold:

        * **Nothing predates the cutoff.** The main rule.
        * **Something was actually read.** An empty corpus has no contaminated sources and
          establishes nothing; without this clause, analysing zero documents would be the
          cleanest result available. Load-bearing -- removing it fails a test.
        * **A cutoff is known.** Honestly redundant today, and kept deliberately: with an
          unknown cutoff `assess_hindsight` puts every source in `contaminated`, so the second
          clause already refuses. No mutation can distinguish it, which is recorded here rather
          than left for a reader to assume it is doing work. It stays as a guard against a
          future change to that classification, and if it ever becomes reachable it needs a
          test of its own.
        """
        return (
            self.cutoff is not None
            and not self.contaminated
            and bool(self.clean)
        )

    def manifest_entry(self) -> dict[str, str]:
        """Cutoff status in the shape a bundle stores (#45, #18).

        A result marked exploratory for contamination, and one that was never checked, look the
        same afterwards unless the bundle says which. This is what a later reader needs to tell
        an evidentiary downgrade from an absence of assessment.
        """
        return {
            "hindsight_model": self.model,
            "hindsight_cutoff": "unrecorded" if self.cutoff is None else self.cutoff.isoformat(),
            "hindsight_cutoff_source": self.cutoff_source or "unattributed",
            "hindsight_sources_clean": str(len(self.clean)),
            "hindsight_sources_contaminated": str(len(self.contaminated)),
            "hindsight_usable_as_evidence": str(self.usable_as_evidence).lower(),
        }

    @property
    def reason(self) -> str:
        if self.cutoff is None:
            return (
                f"{self.model} has no recorded training cutoff, so nothing can be said about "
                "what it already knew"
            )
        if self.contaminated:
            return (
                f"{self.model} was trained through {self.cutoff.isoformat()} and "
                f"{len(self.contaminated)} of {len(self.contaminated) + len(self.clean)} sources "
                "were published before that"
            )
        return f"every source postdates {self.model}'s {self.cutoff.isoformat()} cutoff"


def assess_hindsight(
    sources: Sequence[Source],
    *,
    model: str,
    cutoff: date | None,
    cutoff_source: str = "",
) -> HindsightAssessment:
    """Split sources by whether the model could already have known their outcome.

    Compares against `published_at` rather than `retrieved_at`. When a document was *fetched* says
    nothing about whether the model saw it in training; when it was *published* is the question.
    """
    contaminated: list[str] = []
    clean: list[str] = []
    for source in sources:
        if cutoff is None or _as_date(source.published_at) <= cutoff:
            contaminated.append(source.source_id)
        else:
            clean.append(source.source_id)
    return HindsightAssessment(
        model=model,
        cutoff=cutoff,
        cutoff_source=cutoff_source,
        contaminated=tuple(contaminated),
        clean=tuple(clean),
    )


def require_no_hindsight(
    sources: Sequence[Source],
    *,
    model: str,
    cutoff: date | None,
    cutoff_source: str = "",
) -> HindsightAssessment:
    """Refuse outright when an analysis would rest on text the model already knew.

    For the confirmatory path. `assess_hindsight` is for callers that want to mark a result
    exploratory and continue; this is for callers that must not proceed at all.
    """
    assessment = assess_hindsight(
        sources, model=model, cutoff=cutoff, cutoff_source=cutoff_source
    )
    if not assessment.usable_as_evidence:
        raise HindsightRefused(assessment.reason)
    return assessment


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


__all__ = [
    "HindsightAssessment",
    "HindsightRefused",
    "assess_hindsight",
    "require_no_hindsight",
]
