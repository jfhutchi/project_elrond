"""The producer the research loop never had (#3, #13).

`run_cycle` advances a `ResearchTask` by one stage and `cli.py` wires a handler for `PROPOSED`.
Both are consumers. Nothing in `src/` ever constructed a `ResearchTask`, so the queue was empty by
construction and `research-cycle --commit` returned `advanced: []` on a healthy, fully configured
system. The whole orchestration layer was a machine with no input.

This module is that input, and it is deliberately the most constrained producer the project has:
a proposer is the component best placed to spend the multiple-testing budget on questions nobody
chose.

**The model does not decide what to research.** It selects from a slate this module computed
deterministically, and it may explain the choice. Every eligibility decision -- which datasets have
clean confirmatory capacity, which have observations at all, whether an equivalent task already
exists, whether memory holds a refutation nobody has adjudicated, whether an earlier autonomous
question on the same data is still open, what the question is and what it requires -- is made here
from measured state before any model is called. A model that answers with a dataset nobody offered,
a template nobody wrote, or JSON of the wrong shape produces no task at all.

**Eligibility is measured, not declared.** `capacity_inventory` answers most of it, and the part
that is easy to miss is that a source with no `observation_prefixes` keeps no local observations
and so reports zero unsearched sessions however `AVAILABLE` its status looks. On this host
`crypto-daily`, `us-macro-monthly` and `sec-edgar-filings` are all `AVAILABLE` with zero trials
and **no data**.

**Clean capacity is per question, not only per dataset.** A session can be unsearched for the
project and already seen for one question. `ProposalTemplate.exposed_through` records that, and
the reason it exists is a defect in this module's first version: its measurement read every
stored session, including the ones the capacity gate had just counted as clean, and a proof run
published the resulting statistics. Those sessions are spent for those questions, and the
catalogue says so rather than pretending the run never happened.

**Identity is deterministic, and coarser than the state that produced it.** `task_id` is a function
of template, template version and dataset, so a question is proposed once, ever. Every attempt,
including one that opened nothing, is written to `research_proposals` under a content address of
the attempt, so re-running against unchanged state writes nothing new, and restarting the service
cannot mint duplicates. One proposal per task and one per (dataset, sequence) are unique in the
database, so two proposers racing each other cannot both open a question against the same data.

**The novelty gate is not the duplicate check.** `memory.recall` matches the *entire* question text
as a substring, which a full-sentence question essentially never satisfies. That is the right
conservatism for its own job, but it means the gate cannot be relied on to catch a repeat. The
deterministic identity is what prevents duplicate work, and the gate still runs in the cycle.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.research.capacity import (
    DATA_SOURCES,
    CapacityRow,
    capacity_inventory,
    consumed_through,
    stored_sessions,
)
from quantbot.research.director import TERMINAL, ResearchDirector, ResearchTask, TaskState
from quantbot.research.memory import REFUTING, RecordKind, ResearchMemory, ResearchRecord
from quantbot.research.models import (
    ModelError,
    ModelRole,
    ModelRuntime,
    PromptTemplate,
)
from quantbot.research.power import Estimand
from quantbot.storage.database import encode_utc
from quantbot.storage.schema import research_proposals, research_tasks

#: Bumped when the proposer's own decision procedure changes in a way that could legitimately
#: produce a different slate from the same state. Recorded on every proposal, because "why did
#: this exist" is not answerable from the template alone once the selection rules have moved.
#:
#: 2: per-question exposure, mechanical refutation matching, one open autonomous question per
#: dataset, durable attempt records, and refusal as an outcome rather than an exception.
PROPOSER_VERSION = 2

#: Outcomes an attempt can have, matching `research_proposals.outcome`.
CREATED = "CREATED"
NO_ELIGIBLE = "NO_ELIGIBLE"
REFUSED = "REFUSED"

#: Why an attempt opened nothing when the slate was empty. The per-question reasons are in the
#: attempt's evidence; this is the summary line.
NO_ELIGIBLE_REASON = (
    "no eligible candidate: every catalogue question is already asked, refuted, unadjudicated "
    "against a refutation, blocked by an open question on its dataset, or has no clean "
    "confirmatory session to be asked of"
)

#: Actor recorded on the opening event of every task this module creates.
ACTOR_PREFIX = "proposer/"

_COMMIT = re.compile(r"^[0-9a-f]{40}(-dirty)?$")


class ProposalRefused(RuntimeError):
    """Raised when a proposal must not be created, or its provenance cannot be stated."""


class CatalogueDefect(ProposalRefused):
    """A template that cannot be evaluated honestly. A programming error, not a refusal."""


# --- what the state actually supports ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DatasetReadiness:
    """One declared source, and whether a confirmatory question could be asked of it today."""

    identity: str
    dataset: str
    status: str
    trials: int
    #: From `CapacityRow.usable_for_confirmation`: status plus the unsearched-session rescue.
    has_confirmatory_capacity: bool
    #: Session dates this database holds that no search has consumed. Empty means either a fully
    #: consumed range or -- for a source with no `observation_prefixes` -- no local data at all.
    unsearched: tuple[date, ...]
    #: Sessions stored for this source in total, consumed or not. Separates "nothing new" from
    #: "nothing at all", which `unsearched` alone cannot.
    stored_sessions: int
    #: The last session any search consumed. Everything on or before it is exploratory only.
    consumed_through: date | None
    eligible: bool
    reason: str

    @property
    def unsearched_sessions(self) -> int:
        return len(self.unsearched)

    @property
    def first_unsearched(self) -> date | None:
        return self.unsearched[0] if self.unsearched else None

    def as_detail(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "status": self.status,
            "trials": str(self.trials),
            "confirmatory_capacity": str(self.has_confirmatory_capacity).lower(),
            "unsearched_sessions": str(self.unsearched_sessions),
            "first_unsearched": _iso(self.first_unsearched),
            "stored_sessions": str(self.stored_sessions),
            "consumed_through": _iso(self.consumed_through),
            "eligible": str(self.eligible).lower(),
            "reason": self.reason,
        }


def measure_readiness(session: Session, *, as_of: date) -> tuple[DatasetReadiness, ...]:
    """Measure every declared source. Nothing here consumes capacity or calls a model."""
    inventory: dict[str, CapacityRow] = {
        row.observation_identity: row for row in capacity_inventory(session, as_of=as_of)
    }
    readiness: list[DatasetReadiness] = []
    for source in DATA_SOURCES:
        row = inventory.get(source.observation_identity)
        if row is None:  # pragma: no cover - inventory covers DATA_SOURCES by construction
            continue
        stored = len(stored_sessions(session, source, as_of=as_of))
        if not row.usable_for_confirmation:
            eligible, reason = False, (
                f"{row.status.value.lower()} with no clean confirmatory capacity"
            )
        elif stored == 0:
            eligible, reason = False, "declared source with no observations in this store"
        elif not row.unsearched_sessions:
            eligible, reason = False, "every stored session has already been searched"
        else:
            eligible, reason = True, (
                f"{len(row.unsearched_sessions)} unsearched session(s) from "
                f"{row.unsearched_sessions[0].isoformat()}"
            )
        readiness.append(
            DatasetReadiness(
                identity=row.observation_identity,
                dataset=source.dataset,
                status=row.status.value,
                trials=row.trials,
                has_confirmatory_capacity=row.usable_for_confirmation,
                unsearched=row.unsearched_sessions,
                stored_sessions=stored,
                consumed_through=consumed_through(session, source, as_of=as_of),
                eligible=eligible,
                reason=reason,
            )
        )
    return tuple(readiness)


# --- the finite catalogue ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalTemplate:
    """One question this project is prepared to ask, written before any state was consulted.

    A catalogue rather than free-form generation, for the reason the continuous-research design
    gives: a model that writes its own question writes its own success criterion with it, and a
    search whose space is defined after seeing the data has no honest cardinality. Every field
    here is fixed at authoring time, including the decision rule.
    """

    template_id: str
    version: int
    family_id: str
    question: str
    #: Declared by dataset name from `DATA_SOURCES`, which `stages.capacity_stage` matches
    #: structurally. Never inferred from the question text.
    required_datasets: tuple[str, ...]
    estimand: Estimand
    #: Pre-registered before any measurement, so the bar cannot move to meet the result.
    expected_effect: Decimal
    minimum_practical_effect: Decimal
    #: The measurement this template runs, resolved by `autoexperiment.MEASUREMENTS`.
    measurement_id: str
    #: Correlated units observed on the same session, e.g. 23 ETFs ranked together.
    cluster_size: int
    rationale: str
    #: What a refuting finding would have to be *about* to bear on this question. Matched against
    #: the `subject` of every refuting record in memory, the way `unlinked_refutations` matches.
    #: Required: a template with no terms would make the refutation check vacuous.
    subject_terms: tuple[str, ...] = ()
    #: `(record_id, why it does not settle this question)` for every refuting record the terms
    #: match. A match nobody has adjudicated makes the question ineligible, so a refutation
    #: recorded tomorrow retires a question today until somebody writes down why it does not.
    adjudicated_refutations: tuple[tuple[str, str], ...] = ()
    #: The last session whose data has already informed a statistic for *this* question. Sessions
    #: on or before it are exploratory for this question even when the project-wide inventory
    #: counts them as unsearched, because contamination is a fact about what was seen.
    exposed_through: date | None = None
    #: How that exposure happened. Required when `exposed_through` is set.
    exposure: str = ""

    @property
    def catalogue_hash(self) -> str:
        """Content address of the template, so a silently edited question is detectable."""
        material = json.dumps(
            {
                "template_id": self.template_id,
                "version": self.version,
                "family_id": self.family_id,
                "question": self.question,
                "required_datasets": list(self.required_datasets),
                "estimand": self.estimand.value,
                "expected_effect": str(self.expected_effect),
                "minimum_practical_effect": str(self.minimum_practical_effect),
                "measurement_id": self.measurement_id,
                "cluster_size": self.cluster_size,
                "subject_terms": list(self.subject_terms),
                "adjudicated_refutations": [list(pair) for pair in self.adjudicated_refutations],
                "exposed_through": _iso(self.exposed_through),
                "exposure": self.exposure,
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def clean_sessions(self, readiness: DatasetReadiness) -> tuple[date, ...]:
        """Unsearched sessions that this question has not already seen."""
        if self.exposed_through is None:
            return readiness.unsearched
        return tuple(day for day in readiness.unsearched if day > self.exposed_through)

    def exploratory_through(self, readiness: DatasetReadiness) -> date | None:
        """The last session a measurement for this question may read. `None`: nothing may be."""
        bounds = [
            bound for bound in (readiness.consumed_through, self.exposed_through) if bound
        ]
        return max(bounds) if bounds else None


#: Exposure shared by the first three questions, recorded once so the three cannot drift apart.
_PROOF_RUN_EXPOSURE = (
    "A proof run at 2026-09-12T22:18Z on a byte copy of the Omen store measured this question over "
    "every stored session through 2026-09-11 -- including the 14 the capacity gate counted as "
    "unsearched -- because this module's first measurement did not restrict itself to the "
    "exploratory range. The statistic was published in "
    "docs/autonomous-research-proof-2026-09-12.md, so those sessions have informed this question "
    "and cannot confirm it."
)

#: Short and hand-written, like `DATA_SOURCES`. Each entry is a question somebody decided was
#: worth asking, with its decision rule fixed in advance.
#:
#: Every one is chosen to be absent from `REFUTED.md` as a claim, and every refuting record whose
#: subject shares a term is adjudicated in writing below. This project has already settled
#: momentum in six forms, trend, volatility targeting, overnight, turn-of-month, breadth as a return
#: signal, ensembles, leverage, meta-labelling and cross-asset generality; re-asking any of those
#: would spend a trial to re-learn a stored result.
CATALOG: tuple[ProposalTemplate, ...] = (
    ProposalTemplate(
        template_id="dispersion-precedes-volatility",
        version=1,
        family_id="cross-sectional-dispersion",
        question=(
            "Does the cross-sectional dispersion of daily returns across the tradable sleeve "
            "universe predict the magnitude of the next session's benchmark move?"
        ),
        required_datasets=("sip-us-equities-daily",),
        estimand=Estimand.INFORMATION_COEFFICIENT,
        expected_effect=Decimal("0.20"),
        minimum_practical_effect=Decimal("0.10"),
        measurement_id="dispersion_forward_absolute_move",
        cluster_size=1,
        rationale=(
            "Dispersion is a property of the panel rather than of any one sleeve, so it is not "
            "reachable by the momentum, trend and volatility searches that exhausted this range"
        ),
        subject_terms=("dispersion", "volatility"),
        adjudicated_refutations=(
            (
                "refuted-016",
                "#16 refuted scaling exposure by trailing volatility as a source of excess "
                "return. This asks whether cross-sectional dispersion forecasts the size of the "
                "next index move -- a forecast of volatility, not a rule for sizing positions -- "
                "which #16 did not measure",
            ),
        ),
        exposed_through=date(2026, 9, 11),
        exposure=_PROOF_RUN_EXPOSURE,
    ),
    ProposalTemplate(
        template_id="breadth-persists-next-session",
        version=1,
        family_id="cross-sectional-dispersion",
        question=(
            "Does the fraction of sleeves closing positive on one session predict the same "
            "fraction on the next session?"
        ),
        required_datasets=("sip-us-equities-daily",),
        estimand=Estimand.INFORMATION_COEFFICIENT,
        expected_effect=Decimal("0.15"),
        minimum_practical_effect=Decimal("0.08"),
        measurement_id="breadth_autocorrelation",
        cluster_size=1,
        rationale=(
            "Cycle 10 refuted breadth as a *return* signal on individual names; whether the "
            "breadth series is itself serially dependent is a different and untested claim"
        ),
        subject_terms=("breadth",),
        adjudicated_refutations=(
            (
                "refuted-015",
                "#15 refuted cross-sectional breadth as a whole-market momentum return signal "
                "over 511 names. This asks whether the breadth series is serially dependent from "
                "one session to the next, a property of the series rather than a claim that it "
                "earns a return, and #15 did not measure it",
            ),
        ),
        exposed_through=date(2026, 9, 11),
        exposure=_PROOF_RUN_EXPOSURE,
    ),
    ProposalTemplate(
        template_id="dispersion-predicts-benchmark-direction",
        version=1,
        family_id="cross-sectional-dispersion",
        question=(
            "Does cross-sectional dispersion of daily returns predict the signed return of the "
            "benchmark on the following session?"
        ),
        required_datasets=("sip-us-equities-daily",),
        estimand=Estimand.INFORMATION_COEFFICIENT,
        expected_effect=Decimal("0.15"),
        minimum_practical_effect=Decimal("0.08"),
        measurement_id="dispersion_forward_signed_return",
        cluster_size=1,
        rationale=(
            "The signed counterpart of the magnitude claim. Kept separate because a mechanism "
            "that predicts size and one that predicts direction are different claims and must "
            "not be tested as one"
        ),
        subject_terms=("dispersion",),
        exposed_through=date(2026, 9, 11),
        exposure=_PROOF_RUN_EXPOSURE,
    ),
    ProposalTemplate(
        template_id="benchmark-move-size-persists",
        version=1,
        family_id="volatility-persistence",
        question=(
            "Does the magnitude of the benchmark's daily move predict the magnitude of its move "
            "on the following session?"
        ),
        required_datasets=("sip-us-equities-daily",),
        estimand=Estimand.INFORMATION_COEFFICIENT,
        # Pre-registered from the literature, not from this panel: the lag-one autocorrelation of
        # absolute daily index returns is reported near 0.2-0.3 over long samples (Ding, Granger
        # and Engle 1993; Cont 2001). 0.20 is the low end, and 0.10 is half of it.
        expected_effect=Decimal("0.20"),
        minimum_practical_effect=Decimal("0.10"),
        measurement_id="benchmark_absolute_move_persistence",
        cluster_size=1,
        rationale=(
            "Volatility clustering is among the best-documented properties of daily index "
            "returns, so this is the question the measurement instrument should be able to see. "
            "Three dispersion and breadth measurements on this panel returned |t| below 1; if "
            "this one cannot separate from zero either, those nulls say more about the "
            "instrument than about the market. It is not an edge on its own: the size of a move "
            "says nothing about its direction"
        ),
        subject_terms=("volatility", "magnitude"),
        adjudicated_refutations=(
            (
                "refuted-016",
                "#16 refuted volatility targeting as an alpha source. This measures whether the "
                "size of one session's index move predicts the size of the next -- the "
                "persistence volatility targeting assumes -- and makes no claim that trading on "
                "it earns anything",
            ),
        ),
    ),
)


def template_by_id(template_id: str) -> ProposalTemplate | None:
    return next((entry for entry in CATALOG if entry.template_id == template_id), None)


# --- what memory and the queue already hold ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """An autonomous task that has not reached a terminal state."""

    task_id: str
    template_id: str
    state: TaskState


@dataclass(frozen=True, slots=True)
class MemoryState:
    """Everything a slate depends on besides the datasets, read once so the slate is pure."""

    existing_task_ids: frozenset[str]
    #: Template ids with an autonomous task in `REFUTED`, at any version.
    refuted_template_ids: frozenset[str]
    #: Dataset identity -> the open autonomous question holding it.
    open_by_dataset: Mapping[str, OpenQuestion]
    #: Dataset identity -> the highest proposal sequence on record, read in the same statement as
    #: `open_by_dataset`. A new proposal claims exactly one more than this, so a proposer whose
    #: view of "this dataset is free" went stale collides on the unique (dataset, sequence) rule
    #: instead of opening a second question beside the one it did not see.
    sequence_by_dataset: Mapping[str, int]
    #: Every refuting finding in memory, for subject-term matching.
    refutations: tuple[ResearchRecord, ...]

    @classmethod
    def read(cls, session: Session) -> MemoryState:
        director = ResearchDirector(session)
        existing = frozenset(task.task_id for task in director.by_state())
        # One statement, so the open questions and the sequence they imply are one snapshot.
        rows = session.execute(
            select(
                research_proposals.c.task_id,
                research_proposals.c.template_id,
                research_proposals.c.dataset_identity,
                research_proposals.c.dataset_sequence,
                research_tasks.c.state,
            )
            .join(research_tasks, research_proposals.c.task_id == research_tasks.c.task_id)
            .where(research_proposals.c.outcome == CREATED)
        ).all()
        refuted: set[str] = set()
        open_by_dataset: dict[str, OpenQuestion] = {}
        sequence_by_dataset: dict[str, int] = {}
        for task_id, template_id, dataset_identity, sequence, state in rows:
            sequence_by_dataset[str(dataset_identity)] = max(
                sequence_by_dataset.get(str(dataset_identity), 0), int(sequence)
            )
            task_state = TaskState(str(state))
            if task_state is TaskState.REFUTED:
                refuted.add(str(template_id))
            if task_state not in TERMINAL:
                open_by_dataset[str(dataset_identity)] = OpenQuestion(
                    task_id=str(task_id), template_id=str(template_id), state=task_state
                )
        refutations = tuple(
            record
            for record in ResearchMemory(session).recall("", kinds=[RecordKind.FINDING])
            if record.verdict in REFUTING
        )
        return cls(
            existing_task_ids=existing,
            refuted_template_ids=frozenset(refuted),
            open_by_dataset=open_by_dataset,
            sequence_by_dataset=sequence_by_dataset,
            refutations=refutations,
        )


def matching_refutations(
    template: ProposalTemplate, refutations: Sequence[ResearchRecord]
) -> tuple[ResearchRecord, ...]:
    """Refuting records whose subject shares one of the template's terms.

    Subject only and terms of four characters or more, the rule `unlinked_refutations` uses and
    for its reason: `statement` is prose, and a refutation that mentions a word in passing is not
    about it. A match is a prompt to adjudicate, not a verdict, which is why the catalogue carries
    the adjudication rather than this function deciding.
    """
    terms = [term.lower() for term in template.subject_terms if len(term) >= 4]
    return tuple(
        record
        for record in refutations
        if any(term in record.subject.lower() for term in terms)
    )


# --- the slate ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One template paired with the dataset state that makes it askable, or not."""

    template: ProposalTemplate
    readiness: DatasetReadiness
    task_id: str
    eligible: bool
    reason: str
    clean_sessions: tuple[date, ...] = ()
    matched_refutations: tuple[str, ...] = ()

    def as_detail(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "template_id": self.template.template_id,
            "template_version": str(self.template.version),
            "dataset": self.readiness.dataset,
            "eligible": str(self.eligible).lower(),
            "clean_confirmatory_sessions": str(len(self.clean_sessions)),
            "matched_refutations": ", ".join(self.matched_refutations) or "none",
            "reason": self.reason,
        }


def deterministic_task_id(template: ProposalTemplate, readiness: DatasetReadiness) -> str:
    """Stable identity for one question asked of one dataset.

    Deliberately not a function of the capacity snapshot. Including it would mint a fresh id
    every time a session arrived, so the same question would be re-proposed daily and the
    duplicate suppression would be doing nothing while appearing to work.
    """
    return f"auto-{template.template_id}-v{template.version}-{readiness.identity}"


def _check_catalogue(template: ProposalTemplate) -> None:
    if len(template.required_datasets) != 1:
        # A multi-dataset template would need every one of them eligible -- the "all, not any"
        # rule `capacity_stage` applies -- and is refused rather than evaluated against the first.
        raise CatalogueDefect(
            f"{template.template_id} declares {len(template.required_datasets)} datasets; "
            "this proposer sizes one dataset per question"
        )
    if not any(len(term) >= 4 for term in template.subject_terms):
        raise CatalogueDefect(
            f"{template.template_id} declares no subject term of four characters or more, so "
            "no refutation in memory could ever match it and the check would pass vacuously"
        )
    if (template.exposed_through is None) != (not template.exposure.strip()):
        raise CatalogueDefect(
            f"{template.template_id} must state both when it was exposed and how, or neither"
        )


def candidate_slate(
    readiness: Sequence[DatasetReadiness],
    *,
    memory: MemoryState,
    catalog: Sequence[ProposalTemplate] = CATALOG,
) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
    """Split the catalogue into what may be asked now and what may not, with reasons for both.

    Returns `(eligible, rejected)`. Rejections are returned rather than dropped because "nothing
    was proposed" and "nothing was eligible, and here is why for each" are different answers, and
    only the second is auditable. The first failing check names the rejection.
    """
    by_dataset = {row.dataset: row for row in readiness}
    eligible: list[Candidate] = []
    rejected: list[Candidate] = []

    for template in catalog:
        _check_catalogue(template)
        name = template.required_datasets[0]
        row = by_dataset.get(name)
        if row is None:
            raise CatalogueDefect(
                f"{template.template_id} requires {name}, which is not a declared source; a "
                "template naming an unknown dataset is a catalogue defect, not a near miss"
            )
        task_id = deterministic_task_id(template, row)
        clean = template.clean_sessions(row)
        matched = matching_refutations(template, memory.refutations)
        adjudicated = {record_id for record_id, _ in template.adjudicated_refutations}
        unadjudicated = [
            record.record_id for record in matched if record.record_id not in adjudicated
        ]
        open_question = memory.open_by_dataset.get(row.identity)

        reason: str | None
        if task_id in memory.existing_task_ids:
            reason = "an equivalent task already exists"
        elif template.template_id in memory.refuted_template_ids:
            reason = (
                "an autonomous task for this question was refuted; asking it again needs a new "
                "template that says what changed"
            )
        elif unadjudicated:
            reason = (
                f"memory holds refuting record(s) {', '.join(sorted(unadjudicated))} whose subject "
                "matches this question and which the catalogue has not adjudicated"
            )
        elif not row.eligible:
            reason = f"{row.dataset}: {row.reason}"
        elif open_question is not None:
            reason = (
                f"{open_question.task_id} is still {open_question.state.value} on {row.identity}; "
                "one autonomous question per dataset at a time"
            )
        elif not clean:
            reason = (
                f"{row.dataset}: all {row.unsearched_sessions} unsearched session(s) were already "
                f"seen by this question (exposed through {_iso(template.exposed_through)})"
            )
        else:
            reason = None

        candidate = Candidate(
            template=template,
            readiness=row,
            task_id=task_id,
            eligible=reason is None,
            reason=(
                reason
                if reason is not None
                else f"{row.dataset}: {len(clean)} clean confirmatory session(s) from "
                f"{clean[0].isoformat()}"
            ),
            clean_sessions=clean,
            matched_refutations=tuple(record.record_id for record in matched),
        )
        (eligible if reason is None else rejected).append(candidate)

    # Deterministic order, so the slate a model is shown does not depend on dict iteration or on
    # which template happened to be appended first.
    eligible.sort(key=lambda entry: entry.task_id)
    rejected.sort(key=lambda entry: entry.task_id)
    return tuple(eligible), tuple(rejected)


@dataclass(frozen=True, slots=True)
class Survey:
    """One read of the state a proposal depends on. Writes nothing and calls no model."""

    as_of: date
    readiness: tuple[DatasetReadiness, ...]
    memory: MemoryState
    eligible: tuple[Candidate, ...]
    rejected: tuple[Candidate, ...]

    @property
    def context(self) -> dict[str, str]:
        return {
            "readiness": "\n".join(
                f"- {row.identity}: {row.status}, {row.trials} trials spent, "
                f"{row.unsearched_sessions} unsearched session(s), "
                f"{row.stored_sessions} stored session(s)"
                for row in self.readiness
            ),
            "settled": "\n".join(
                f"- {record.subject}" for record in self.memory.refutations
            )
            or "- (none recorded)",
        }

    def evidence(self) -> dict[str, object]:
        return {
            "as_of": self.as_of.isoformat(),
            "readiness": [row.as_detail() for row in self.readiness],
            "eligible": [entry.as_detail() for entry in self.eligible],
            "rejected": [entry.as_detail() for entry in self.rejected],
            "refuting_records_in_memory": len(self.memory.refutations),
            "open_autonomous_questions": {
                identity: f"{item.task_id} ({item.state.value})"
                for identity, item in sorted(self.memory.open_by_dataset.items())
            },
        }


def survey(
    session: Session, *, now: datetime, catalog: Sequence[ProposalTemplate] = CATALOG
) -> Survey:
    as_of = now.astimezone(UTC).date()
    readiness = measure_readiness(session, as_of=as_of)
    memory = MemoryState.read(session)
    eligible, rejected = candidate_slate(readiness, memory=memory, catalog=catalog)
    return Survey(
        as_of=as_of, readiness=readiness, memory=memory, eligible=eligible, rejected=rejected
    )


# --- selection ----------------------------------------------------------------------------------


#: Longest answer a selecting model may give, per field. Enforced after parsing rather than in
#: the schema -- see `CandidateChoice`.
_MAX_TEMPLATE_ID = 128
_MAX_REASON = 2000


# The length bounds on `CandidateChoice` are enforced in a validator rather than declared as
# `min_length` / `max_length` on the fields, and that is not a style choice. `call_structured`
# sends `model_json_schema()` to the server as a grammar to constrain decoding, and Ollama's
# grammar compiler rejects `minLength`/`maxLength` on a string property outright:
#
#     HTTP 400  Failed to initialize samplers: failed to parse grammar
#
# Isolated against the live endpoint on 2026-09-12 by adding one schema feature at a time: plain
# strings, `additionalProperties: false`, titles and descriptions all compile; the two length
# keywords are the ones that do not. Declaring them made every selection call fail, which the
# proposer correctly reported as a refusal and which looked exactly like a model outage.
#
# Validating after parsing is also the stronger control. A server that accepted the constraint
# and ignored it would return an oversized answer either way, so the bound has to be checked here
# to mean anything.
#
# The docstring is one line for a related reason: pydantic puts it in the schema's `description`,
# and the schema is sent to the model on every call.
class CandidateChoice(BaseModel):
    """The only shape a model may answer in. Anything else produces no task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template_id: str
    reason: str

    @model_validator(mode="after")
    def validate_choice(self) -> Self:
        if not self.template_id.strip():
            raise ValueError("template_id must name an option, not empty space")
        if len(self.template_id) > _MAX_TEMPLATE_ID:
            raise ValueError(f"template_id exceeds {_MAX_TEMPLATE_ID} characters")
        if not self.reason.strip():
            raise ValueError("a selection without a reason is a vote, and votes are not evidence")
        if len(self.reason) > _MAX_REASON:
            raise ValueError(f"reason exceeds {_MAX_REASON} characters")
        return self


SELECTION_PROMPT = PromptTemplate(
    name="research-candidate-selection",
    version="1",
    text="""You choose which pre-approved research question a quantitative research system should
investigate next.

You may ONLY choose from the numbered options below. You may not invent a question, a dataset or
a metric, and you may not decide whether evidence is sufficient, whether a result is significant,
or whether anything may be promoted. Those are decided by the system, not by you.

Measured dataset state:
{readiness}

Questions this project has already settled, which must not be re-asked:
{settled}

Eligible options:
{options}

Answer with JSON only: {{"template_id": "<copied exactly from one option>", "reason": "<why that
option is the most informative next test given the state above>"}}""",
)


@dataclass(frozen=True, slots=True)
class Selection:
    """Which candidate won, why, and what produced the decision."""

    candidate: Candidate
    reason: str
    selector: str
    model_identity: str | None = None
    model_response_hash: str | None = None
    prompt_template: str | None = None
    prompt_template_hash: str | None = None
    model_fallback: bool = False

    def as_detail(self) -> dict[str, str]:
        detail = {
            "selected_template_id": self.candidate.template.template_id,
            "selector": self.selector,
            "selection_reason": self.reason,
        }
        if self.model_identity is not None:
            detail["model_identity"] = self.model_identity
        if self.model_response_hash is not None:
            detail["model_response_sha256"] = self.model_response_hash
        if self.prompt_template is not None:
            detail["prompt_template"] = self.prompt_template
        if self.prompt_template_hash is not None:
            detail["prompt_template_hash"] = self.prompt_template_hash
        if self.model_fallback:
            detail["model_degraded"] = "true: answered by a fallback model, not the primary"
        return detail


class CandidateSelector(Protocol):
    """How one candidate is chosen from a slate this module already validated."""

    @property
    def identity(self) -> str: ...

    def select(self, candidates: Sequence[Candidate], *, context: Mapping[str, str]) -> Selection:
        ...


@dataclass(frozen=True, slots=True)
class FirstEligibleSelector:
    """Deterministic fallback selector, used by tests and by no production path.

    Exists so the proposer's own logic can be tested without a model, and so a reader can see
    that selection is the *only* thing a model contributes: swap this in and everything else --
    eligibility, identity, provenance, gating -- behaves identically.
    """

    identity: str = "first-eligible"

    def select(self, candidates: Sequence[Candidate], *, context: Mapping[str, str]) -> Selection:
        if not candidates:
            raise ProposalRefused("no eligible candidate to select from")
        return Selection(
            candidate=candidates[0],
            reason="first eligible candidate in deterministic order",
            selector=self.identity,
        )


class ModelCandidateSelector:
    """Asks the configured generator to choose, and refuses anything it was not offered."""

    identity = "model"

    def __init__(self, runtime: ModelRuntime, *, role: ModelRole = ModelRole.GENERATOR) -> None:
        self._runtime = runtime
        self._role = role

    def select(self, candidates: Sequence[Candidate], *, context: Mapping[str, str]) -> Selection:
        if not candidates:
            raise ProposalRefused("no eligible candidate to select from")
        offered = {entry.template.template_id: entry for entry in candidates}
        options = "\n".join(
            f"{index + 1}. template_id={entry.template.template_id}\n"
            f"   question: {entry.template.question}\n"
            f"   dataset: {entry.readiness.dataset} ({entry.reason})\n"
            f"   why it is available: {entry.template.rationale}"
            for index, entry in enumerate(candidates)
        )
        try:
            choice, response = self._runtime.call_structured(
                self._role,
                SELECTION_PROMPT,
                {
                    "readiness": context.get("readiness", ""),
                    "settled": context.get("settled", ""),
                    "options": options,
                },
                CandidateChoice,
                now=datetime.now(UTC),
            )
        except ModelError as error:
            # Fail closed. An unavailable or malformed model means no task, never a task chosen
            # by falling back to something the operator did not configure.
            raise ProposalRefused(
                f"the selecting model did not produce a usable choice: "
                f"{type(error).__name__}: {error}"
            ) from error

        chosen = offered.get(choice.template_id)
        if chosen is None:
            raise ProposalRefused(
                f"the model chose {choice.template_id[:_MAX_TEMPLATE_ID]!r}, which was not among "
                f"the {len(offered)} option(s) it was given; a selection outside the slate is a "
                "hallucinated question and must not become a task"
            )
        return Selection(
            candidate=chosen,
            reason=choice.reason.strip(),
            selector=self.identity,
            model_identity=f"{response.spec.provider}:{response.spec.model}@{response.spec.version}",
            model_response_hash=response.response_hash,
            prompt_template=f"{response.template.name}@{response.template.version}",
            prompt_template_hash=response.template.template_hash,
            model_fallback=response.is_fallback,
        )


def select_candidate(state: Survey, selector: CandidateSelector) -> Selection:
    """Ask the selector to choose from the slate. Raises `ProposalRefused`; writes nothing."""
    if not state.eligible:
        raise ProposalRefused("no eligible candidate to select from")
    selection = selector.select(state.eligible, context=state.context)
    if selection.candidate not in state.eligible:
        raise ProposalRefused(
            f"{selector.identity} returned {selection.candidate.task_id}, which was not on the "
            "slate it was given"
        )
    return selection


# --- recording ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What one proposal attempt did, whether or not it produced anything."""

    outcome: str
    created: ResearchTask | None
    proposal_id: str
    #: False when an identical attempt was already on record, so nothing new was written.
    recorded: bool
    selection: Selection | None
    readiness: tuple[DatasetReadiness, ...]
    eligible: tuple[Candidate, ...]
    rejected: tuple[Candidate, ...]
    reason: str
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.created is not None

    def summary(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "created": None if self.created is None else self.created.task_id,
            "proposal_id": self.proposal_id,
            "recorded": self.recorded,
            "reason": self.reason,
            "eligible": [entry.as_detail() for entry in self.eligible],
            "rejected": [entry.as_detail() for entry in self.rejected],
            "readiness": [entry.as_detail() for entry in self.readiness],
        }


def validate_commit(code_commit: str) -> str:
    """A proposal names the code that made it, or it is not made."""
    if not _COMMIT.fullmatch(code_commit.strip().lower()):
        raise ProposalRefused(
            f"the code commit {code_commit!r} is not a full SHA, so the proposal could not say "
            "what produced it; provenance that cannot be stated is not provenance"
        )
    return code_commit.strip().lower()


def _attempt_id(material: Mapping[str, object]) -> str:
    """Content address of one attempt: the same state, code and decision give the same id."""
    encoded = json.dumps(material, sort_keys=True, default=str)
    return "prop-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]


def _insert_attempt(
    session: Session,
    *,
    proposal_id: str,
    outcome: str,
    proposer_version: int,
    code_commit: str,
    now: datetime,
    evidence: Mapping[str, object],
    candidate: Candidate | None = None,
    task_id: str | None = None,
    dataset_sequence: int | None = None,
) -> bool:
    existing = session.execute(
        select(research_proposals.c.proposal_id).where(
            research_proposals.c.proposal_id == proposal_id
        )
    ).one_or_none()
    if existing is not None:
        return False
    session.execute(
        research_proposals.insert().values(
            proposal_id=proposal_id,
            outcome=outcome,
            task_id=task_id,
            template_id=None if candidate is None else candidate.template.template_id,
            template_version=None if candidate is None else candidate.template.version,
            catalogue_hash=None if candidate is None else candidate.template.catalogue_hash,
            dataset_identity=None if candidate is None else candidate.readiness.identity,
            dataset_sequence=dataset_sequence,
            proposer_version=proposer_version,
            code_commit=code_commit,
            attempted_at=encode_utc(now),
            evidence_json=json.dumps(evidence, sort_keys=True, default=str),
        )
    )
    return True


def record_no_task(
    session: Session,
    state: Survey,
    *,
    now: datetime,
    code_commit: str,
    reason: str,
    selection: Selection | None = None,
    proposer_version: int = PROPOSER_VERSION,
) -> ProposalOutcome:
    """Record an attempt that opened nothing, so the decision not to act is auditable too."""
    outcome = NO_ELIGIBLE if not state.eligible else REFUSED
    commit = code_commit.strip().lower() or "unknown"
    evidence: dict[str, object] = {
        **state.evidence(),
        "outcome": outcome,
        "reason": reason,
        "proposer_version": proposer_version,
        "code_commit": commit,
    }
    if selection is not None:
        evidence["selection"] = selection.as_detail()
    # The date is evidence but not identity: the same state and code on a later day is the same
    # attempt, so an unattended loop that keeps finding nothing writes one row, not one a day.
    identifier = _attempt_id({key: value for key, value in evidence.items() if key != "as_of"})
    recorded = _insert_attempt(
        session,
        proposal_id=identifier,
        outcome=outcome,
        proposer_version=proposer_version,
        code_commit=commit[:64],
        now=now,
        evidence={**evidence, "attempted_at": now.astimezone(UTC).isoformat(timespec="seconds")},
        candidate=None if selection is None else selection.candidate,
    )
    return ProposalOutcome(
        outcome=outcome,
        created=None,
        proposal_id=identifier,
        recorded=recorded,
        selection=selection,
        readiness=state.readiness,
        eligible=state.eligible,
        rejected=state.rejected,
        reason=reason,
    )


def commit_proposal(
    session: Session,
    selection: Selection,
    *,
    now: datetime,
    code_commit: str,
    catalog: Sequence[ProposalTemplate] = CATALOG,
    proposer_version: int = PROPOSER_VERSION,
) -> ProposalOutcome:
    """Re-survey, confirm the selection is still eligible, and open the task with its record.

    The re-survey is what makes selection outside a transaction safe. A model call can take a
    minute; holding a write transaction open that long would stall the trading daemon's writes on
    this single-file store, so selection happens between transactions -- and anything could have
    changed during it. A candidate that is no longer on the slate is recorded as refused.

    Everything written here -- the task, its opening event, the proposal record -- is in the
    caller's transaction and commits together or not at all. A concurrent proposer that got
    there first surfaces as an `IntegrityError` from the uniqueness rules, which the caller rolls
    back; nothing is swallowed.
    """
    state = survey(session, now=now, catalog=catalog)
    try:
        commit = validate_commit(code_commit)
    except ProposalRefused as refused:
        return record_no_task(
            session,
            state,
            now=now,
            code_commit=code_commit,
            reason=str(refused),
            selection=selection,
            proposer_version=proposer_version,
        )
    current = next(
        (entry for entry in state.eligible if entry.task_id == selection.candidate.task_id),
        None,
    )
    if current is None:
        return record_no_task(
            session,
            state,
            now=now,
            code_commit=commit,
            reason=(
                f"{selection.candidate.task_id} was selected but is no longer eligible: the state "
                "changed while the selection was being made"
            ),
            selection=selection,
            proposer_version=proposer_version,
        )

    template = current.template
    readiness = current.readiness
    # From the survey's snapshot, never re-read: see `MemoryState.sequence_by_dataset`.
    sequence = state.memory.sequence_by_dataset.get(readiness.identity, 0) + 1
    evidence: dict[str, object] = {
        **state.evidence(),
        "outcome": CREATED,
        "proposer_version": proposer_version,
        "code_commit": commit,
        "selection": selection.as_detail(),
        "task_id": current.task_id,
        "dataset_sequence": sequence,
    }
    identifier = _attempt_id({key: value for key, value in evidence.items() if key != "as_of"})

    adjudications = dict(template.adjudicated_refutations)
    provenance: dict[str, str] = {
        "proposal_id": identifier,
        "proposer_version": str(proposer_version),
        "code_commit": commit,
        "proposed_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "as_of": state.as_of.isoformat(),
        "template_id": template.template_id,
        "template_version": str(template.version),
        "catalogue_hash": template.catalogue_hash,
        "family_id": template.family_id,
        "measurement_id": template.measurement_id,
        "estimand": template.estimand.value,
        "expected_effect": str(template.expected_effect),
        "minimum_practical_effect": str(template.minimum_practical_effect),
        "dataset": readiness.dataset,
        "dataset_identity": readiness.identity,
        "dataset_sequence": str(sequence),
        "dataset_status": readiness.status,
        "dataset_trials_spent": str(readiness.trials),
        "dataset_stored_sessions": str(readiness.stored_sessions),
        "dataset_unsearched_sessions": str(readiness.unsearched_sessions),
        "dataset_consumed_through": _iso(readiness.consumed_through),
        "exposed_through": _iso(template.exposed_through),
        "exploratory_through": _iso(template.exploratory_through(readiness)),
        "clean_confirmatory_sessions": str(len(current.clean_sessions)),
        "first_clean_session": current.clean_sessions[0].isoformat(),
        "capacity_class": "clean-confirmatory-sessions-present",
        "novelty_decision": (
            "no task with this identity exists, no autonomous task for this question was "
            "refuted, and every refuting record matching its subject terms is adjudicated; the "
            "cycle's novelty gate runs next"
        ),
        "refuting_records_checked": str(len(state.memory.refutations)),
        "refutations_matched": ", ".join(current.matched_refutations) or "none",
        "refutation_adjudications": "; ".join(
            f"{record_id}: {adjudications[record_id]}" for record_id in current.matched_refutations
        )
        or "none required",
        **selection.as_detail(),
        "alternatives_considered": ", ".join(
            entry.template.template_id for entry in state.eligible if entry is not current
        )
        or "(none: this was the only eligible option)",
        "alternatives_rejected": "; ".join(
            f"{entry.template.template_id} ({entry.reason})" for entry in state.rejected
        )
        or "(none)",
    }

    task = ResearchTask(
        task_id=current.task_id,
        state=TaskState.PROPOSED,
        question=template.question,
        family_id=template.family_id,
        required_datasets=template.required_datasets,
        created_at=now,
        updated_at=now,
    )
    try:
        created = ResearchDirector(session).open_task(
            task,
            actor=f"{ACTOR_PREFIX}{proposer_version}",
            reason=(
                f"{template.template_id} v{template.version} selected by {selection.selector}: "
                f"{selection.reason}"
            ),
            detail=provenance,
        )
    except ValueError:
        # The task appeared between the survey and this write: a concurrent proposer committed
        # it. Nothing has been written in this transaction yet, so recording the refusal is safe.
        # A collision that happens *during* the writes surfaces as an IntegrityError instead and
        # is the caller's to roll back.
        return record_no_task(
            session,
            survey(session, now=now, catalog=catalog),
            now=now,
            code_commit=commit,
            reason=f"{current.task_id} was opened by a concurrent proposal first",
            selection=selection,
            proposer_version=proposer_version,
        )
    _insert_attempt(
        session,
        proposal_id=identifier,
        outcome=CREATED,
        proposer_version=proposer_version,
        code_commit=commit,
        now=now,
        evidence={**evidence, "provenance": provenance},
        candidate=current,
        task_id=created.task_id,
        dataset_sequence=sequence,
    )
    return ProposalOutcome(
        outcome=CREATED,
        created=created,
        proposal_id=identifier,
        recorded=True,
        selection=selection,
        readiness=state.readiness,
        eligible=state.eligible,
        rejected=state.rejected,
        reason=f"opened {created.task_id}",
        provenance=provenance,
    )


def propose(
    session: Session,
    *,
    selector: CandidateSelector,
    now: datetime,
    code_commit: str,
    catalog: Sequence[ProposalTemplate] = CATALOG,
    proposer_version: int = PROPOSER_VERSION,
) -> ProposalOutcome:
    """Measure, decide, and open at most one task, all in one caller-owned transaction.

    One task per call is not timidity: every registration permanently raises the luck bar for
    everything after it, so a producer that could open several in a loop is one bug away from
    spending the project's remaining statistical capacity in an afternoon.

    A refusal -- nothing eligible, a model that failed or answered outside its slate, provenance
    that cannot be stated -- is an *outcome*, recorded and returned, not an exception: raising
    would roll back the record of the refusal along with everything else. Catalogue defects still
    raise, because they are programming errors rather than decisions.

    `cli.py` does not call this for a model-backed selection. It surveys, selects with no
    transaction open, and commits in a second transaction, for the reason `commit_proposal` gives.
    """
    state = survey(session, now=now, catalog=catalog)
    if not state.eligible:
        return record_no_task(
            session,
            state,
            now=now,
            code_commit=code_commit,
            reason=NO_ELIGIBLE_REASON,
            proposer_version=proposer_version,
        )
    try:
        selection = select_candidate(state, selector)
    except CatalogueDefect:
        raise
    except ProposalRefused as refused:
        return record_no_task(
            session,
            state,
            now=now,
            code_commit=code_commit,
            reason=str(refused),
            proposer_version=proposer_version,
        )
    return commit_proposal(
        session,
        selection,
        now=now,
        code_commit=code_commit,
        catalog=catalog,
        proposer_version=proposer_version,
    )


@dataclass(frozen=True, slots=True)
class ProposalRecord:
    """The durable record a task was opened from."""

    proposal_id: str
    task_id: str
    template_id: str
    template_version: int
    catalogue_hash: str
    dataset_identity: str
    code_commit: str


def proposal_for_task(session: Session, task_id: str) -> ProposalRecord | None:
    """The CREATED proposal that opened `task_id`, or `None` for a task nobody proposed."""
    row = (
        session.execute(
            select(research_proposals).where(
                research_proposals.c.task_id == task_id,
                research_proposals.c.outcome == CREATED,
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return ProposalRecord(
        proposal_id=str(row["proposal_id"]),
        task_id=str(row["task_id"]),
        template_id=str(row["template_id"]),
        template_version=int(row["template_version"]),
        catalogue_hash=str(row["catalogue_hash"]),
        dataset_identity=str(row["dataset_identity"]),
        code_commit=str(row["code_commit"]),
    )


def _iso(value: date | None) -> str:
    return "none" if value is None else value.isoformat()


__all__ = [
    "ACTOR_PREFIX",
    "CATALOG",
    "CREATED",
    "NO_ELIGIBLE",
    "NO_ELIGIBLE_REASON",
    "PROPOSER_VERSION",
    "REFUSED",
    "SELECTION_PROMPT",
    "Candidate",
    "CandidateChoice",
    "CandidateSelector",
    "CatalogueDefect",
    "DatasetReadiness",
    "FirstEligibleSelector",
    "MemoryState",
    "ModelCandidateSelector",
    "OpenQuestion",
    "ProposalOutcome",
    "ProposalRecord",
    "ProposalRefused",
    "ProposalTemplate",
    "Selection",
    "Survey",
    "candidate_slate",
    "commit_proposal",
    "deterministic_task_id",
    "matching_refutations",
    "measure_readiness",
    "proposal_for_task",
    "propose",
    "record_no_task",
    "select_candidate",
    "survey",
    "template_by_id",
    "validate_commit",
]
