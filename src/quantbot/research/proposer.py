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
exists, what the question is, what it requires -- is made here from measured state before any model
is called. A model that answers with a dataset nobody offered, a template nobody wrote, or JSON of
the wrong shape produces no task at all. The one input with no measurement behind it is never
allowed to widen what gets tested.

**Eligibility is measured, not declared.** `capacity_inventory` already answers both halves of the
question, and the second half is easy to miss: `CapacityRow.unsearched_sessions` is built from
`DataSource.observation_prefixes`, and a source with no prefixes keeps no local observations, so it
reports zero unsearched sessions however `AVAILABLE` its status looks. On this host `crypto-daily`,
`fred-macro-monthly` and `sec-edgar-filings` are all `AVAILABLE` with zero trials and **no data**.
A proposer that read `status` alone would propose confidently against three empty datasets.

**Identity is deterministic, and coarser than the state that produced it.** `task_id` is a function
of template, template version and dataset -- not of the capacity snapshot. So a question is proposed
once, ever: tomorrow's extra session does not re-propose today's question, and `research_tasks`'
primary key refuses a duplicate in the database rather than in a check somebody could race. Asking
the same question again with more data is a new template version, which is a deliberate act.

**The novelty gate is not the duplicate check.** `memory.recall` matches the *entire* question text
as a substring of a stored subject or statement, which a full-sentence question essentially never
satisfies. That is the right conservatism for its own job -- a false match silently retires a live
idea -- but it means the gate cannot be relied on to catch a repeat. The deterministic identity
above is what actually prevents duplicate work, and the gate still runs afterwards in the cycle.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy.orm import Session

from quantbot.research.capacity import (
    DATA_SOURCES,
    CapacityRow,
    capacity_inventory,
    stored_sessions,
)
from quantbot.research.director import ResearchDirector, ResearchTask, TaskState
from quantbot.research.models import (
    ModelError,
    ModelRole,
    ModelRuntime,
    PromptTemplate,
)
from quantbot.research.power import Estimand

#: Bumped when the proposer's own decision procedure changes in a way that could legitimately
#: produce a different slate from the same state. Recorded on every proposal, because "why did
#: this exist" is not answerable from the template alone once the selection rules have moved.
PROPOSER_VERSION = 1


class ProposalRefused(RuntimeError):
    """Raised when a proposal must not be created, or its provenance cannot be stated."""


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
    #: Sessions this database holds that no search has consumed. Zero means either a fully
    #: consumed range or -- for a source with no `observation_prefixes` -- no local data at all.
    unsearched_sessions: int
    first_unsearched: date | None
    #: Sessions stored for this source in total, consumed or not. Separates "nothing new" from
    #: "nothing at all", which `unsearched_sessions` alone cannot.
    stored_sessions: int
    eligible: bool
    reason: str

    def as_detail(self) -> dict[str, str]:
        return {
            "identity": self.identity,
            "status": self.status,
            "trials": str(self.trials),
            "confirmatory_capacity": str(self.has_confirmatory_capacity).lower(),
            "unsearched_sessions": str(self.unsearched_sessions),
            "stored_sessions": str(self.stored_sessions),
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
                unsearched_sessions=len(row.unsearched_sessions),
                first_unsearched=row.unconsumed_from,
                stored_sessions=stored,
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
            },
            sort_keys=True,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


#: Short and hand-written, like `DATA_SOURCES`. Each entry is a question somebody decided was
#: worth asking, with its decision rule fixed in advance.
#:
#: Every one is chosen to be absent from `REFUTED.md`. This project has already settled momentum
#: in six forms, trend, volatility targeting, overnight, turn-of-month, breadth, ensembles,
#: leverage, meta-labelling and cross-asset generality; re-asking any of those would spend a
#: trial to re-learn a stored result. These are about the *dispersion and agreement* of the
#: sleeves rather than their direction, which is the part of this dataset nothing has searched.
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
    ),
)


def template_by_id(template_id: str) -> ProposalTemplate | None:
    return next((entry for entry in CATALOG if entry.template_id == template_id), None)


# --- the slate ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candidate:
    """One template paired with the dataset state that makes it askable, or not."""

    template: ProposalTemplate
    readiness: DatasetReadiness
    task_id: str
    eligible: bool
    reason: str

    def as_detail(self) -> dict[str, str]:
        return {
            "task_id": self.task_id,
            "template_id": self.template.template_id,
            "template_version": str(self.template.version),
            "dataset": self.readiness.dataset,
            "eligible": str(self.eligible).lower(),
            "reason": self.reason,
        }


def deterministic_task_id(template: ProposalTemplate, readiness: DatasetReadiness) -> str:
    """Stable identity for one question asked of one dataset.

    Deliberately not a function of the capacity snapshot. Including it would mint a fresh id
    every time a session arrived, so the same question would be re-proposed daily and the
    duplicate suppression would be doing nothing while appearing to work.
    """
    return f"auto-{template.template_id}-v{template.version}-{readiness.identity}"


def candidate_slate(
    readiness: Sequence[DatasetReadiness],
    *,
    existing_task_ids: frozenset[str],
    catalog: Sequence[ProposalTemplate] = CATALOG,
) -> tuple[tuple[Candidate, ...], tuple[Candidate, ...]]:
    """Split the catalogue into what may be asked now and what may not, with reasons for both.

    Returns `(eligible, rejected)`. Rejections are returned rather than dropped because "nothing
    was proposed" and "nothing was eligible, and here is why for each" are different answers, and
    only the second is auditable.
    """
    by_dataset = {row.dataset: row for row in readiness}
    eligible: list[Candidate] = []
    rejected: list[Candidate] = []

    for template in catalog:
        # One declared dataset per template today. A multi-dataset template would need every one
        # of them eligible -- the same "all, not any" rule `capacity_stage` applies -- and is
        # refused here rather than silently evaluated against the first.
        if len(template.required_datasets) != 1:
            raise ProposalRefused(
                f"{template.template_id} declares {len(template.required_datasets)} datasets; "
                "this proposer sizes one dataset per question"
            )
        name = template.required_datasets[0]
        row = by_dataset.get(name)
        if row is None:
            raise ProposalRefused(
                f"{template.template_id} requires {name}, which is not a declared source; a "
                "template naming an unknown dataset is a catalogue defect, not a near miss"
            )
        task_id = deterministic_task_id(template, row)
        if task_id in existing_task_ids:
            rejected.append(
                Candidate(
                    template=template,
                    readiness=row,
                    task_id=task_id,
                    eligible=False,
                    reason="an equivalent task already exists",
                )
            )
            continue
        if not row.eligible:
            rejected.append(
                Candidate(
                    template=template,
                    readiness=row,
                    task_id=task_id,
                    eligible=False,
                    reason=f"{row.dataset}: {row.reason}",
                )
            )
            continue
        eligible.append(
            Candidate(
                template=template,
                readiness=row,
                task_id=task_id,
                eligible=True,
                reason=f"{row.dataset}: {row.reason}",
            )
        )

    # Deterministic order, so the slate a model is shown does not depend on dict iteration or on
    # which template happened to be appended first.
    eligible.sort(key=lambda entry: entry.task_id)
    rejected.sort(key=lambda entry: entry.task_id)
    return tuple(eligible), tuple(rejected)


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
    prompt_template_hash: str | None = None
    model_fallback: bool = False


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
                f"the selecting model did not produce a usable choice: {error}"
            ) from error

        chosen = offered.get(choice.template_id)
        if chosen is None:
            raise ProposalRefused(
                f"the model chose {choice.template_id!r}, which was not among the "
                f"{len(offered)} option(s) it was given; a selection outside the slate is a "
                "hallucinated question and must not become a task"
            )
        return Selection(
            candidate=chosen,
            reason=choice.reason.strip(),
            selector=self.identity,
            model_identity=f"{response.spec.provider}:{response.spec.model}@{response.spec.version}",
            model_response_hash=response.response_hash,
            prompt_template_hash=response.template.template_hash,
            model_fallback=response.is_fallback,
        )


# --- proposing -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProposalOutcome:
    """What one proposal attempt did, whether or not it produced anything."""

    created: ResearchTask | None
    proposal_id: str | None
    selection: Selection | None
    readiness: tuple[DatasetReadiness, ...]
    eligible: tuple[Candidate, ...]
    rejected: tuple[Candidate, ...]
    reason: str
    provenance: Mapping[str, str]

    @property
    def ok(self) -> bool:
        return self.created is not None

    def summary(self) -> dict[str, object]:
        return {
            "created": None if self.created is None else self.created.task_id,
            "proposal_id": self.proposal_id,
            "reason": self.reason,
            "eligible": [entry.as_detail() for entry in self.eligible],
            "rejected": [entry.as_detail() for entry in self.rejected],
            "readiness": [entry.as_detail() for entry in self.readiness],
        }


def proposal_id(
    selection: Selection, readiness: DatasetReadiness, *, proposer_version: int = PROPOSER_VERSION
) -> str:
    """Content address of the decision, including the state that produced it.

    Finer-grained than `task_id` on purpose: the task is the question, this is the occasion. Two
    attempts on genuinely different capacity produce different proposal ids even though only the
    first can produce a task, which is what makes a suppressed duplicate distinguishable from a
    proposal that never happened.
    """
    material = json.dumps(
        {
            "proposer_version": proposer_version,
            "catalogue_hash": selection.candidate.template.catalogue_hash,
            "identity": readiness.identity,
            "status": readiness.status,
            "trials": readiness.trials,
            "unsearched_sessions": readiness.unsearched_sessions,
            "first_unsearched": None
            if readiness.first_unsearched is None
            else readiness.first_unsearched.isoformat(),
            "selector": selection.selector,
        },
        sort_keys=True,
    )
    return "prop-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def propose(
    session: Session,
    *,
    selector: CandidateSelector,
    now: datetime,
    code_commit: str,
    settled_subjects: Sequence[str] = (),
    catalog: Sequence[ProposalTemplate] = CATALOG,
    proposer_version: int = PROPOSER_VERSION,
) -> ProposalOutcome:
    """Measure, decide, and open at most one task. Never opens two.

    One task per call is not timidity: every registration permanently raises the luck bar for
    everything after it, so a producer that could open several in a loop is one bug away from
    spending the project's remaining statistical capacity in an afternoon. `run_cycle` made the
    same choice for the same reason and says so.
    """
    as_of = now.astimezone(UTC).date()
    readiness = measure_readiness(session, as_of=as_of)
    director = ResearchDirector(session)
    existing = frozenset(
        task.task_id for state in TaskState for task in director.by_state(state)
    )
    eligible, rejected = candidate_slate(
        readiness, existing_task_ids=existing, catalog=catalog
    )

    if not eligible:
        return ProposalOutcome(
            created=None,
            proposal_id=None,
            selection=None,
            readiness=readiness,
            eligible=(),
            rejected=rejected,
            reason=(
                "no eligible candidate: every catalogue question is either already asked or "
                "names a dataset with no clean confirmatory capacity and no observations"
            ),
            provenance={},
        )

    context = {
        "readiness": "\n".join(
            f"- {row.identity}: {row.status}, {row.trials} trials spent, "
            f"{row.unsearched_sessions} unsearched session(s), "
            f"{row.stored_sessions} stored session(s)"
            for row in readiness
        ),
        "settled": "\n".join(f"- {subject}" for subject in settled_subjects) or "- (none recorded)",
    }
    selection = selector.select(eligible, context=context)
    candidate = selection.candidate
    identifier = proposal_id(selection, candidate.readiness, proposer_version=proposer_version)

    provenance: dict[str, str] = {
        "proposal_id": identifier,
        "proposer_version": str(proposer_version),
        "code_commit": code_commit,
        "proposed_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "template_id": candidate.template.template_id,
        "template_version": str(candidate.template.version),
        "catalogue_hash": candidate.template.catalogue_hash,
        "measurement_id": candidate.template.measurement_id,
        "estimand": candidate.template.estimand.value,
        "expected_effect": str(candidate.template.expected_effect),
        "minimum_practical_effect": str(candidate.template.minimum_practical_effect),
        "dataset": candidate.readiness.dataset,
        "dataset_identity": candidate.readiness.identity,
        "dataset_status": candidate.readiness.status,
        "dataset_trials_spent": str(candidate.readiness.trials),
        "dataset_unsearched_sessions": str(candidate.readiness.unsearched_sessions),
        "dataset_stored_sessions": str(candidate.readiness.stored_sessions),
        "capacity_class": "confirmatory-capacity-present",
        "novelty_decision": "no equivalent task id exists; the cycle's novelty gate runs next",
        "selector": selection.selector,
        "selection_reason": selection.reason,
        "alternatives_considered": ", ".join(
            entry.template.template_id for entry in eligible if entry is not candidate
        )
        or "(none: this was the only eligible option)",
        "alternatives_rejected": ", ".join(
            f"{entry.template.template_id} ({entry.reason})" for entry in rejected
        )
        or "(none)",
    }
    if selection.model_identity is not None:
        provenance["model_identity"] = selection.model_identity
    if selection.model_response_hash is not None:
        provenance["model_response_sha256"] = selection.model_response_hash
    if selection.prompt_template_hash is not None:
        provenance["prompt_template_hash"] = selection.prompt_template_hash
    if selection.model_fallback:
        provenance["model_degraded"] = "true: answered by a fallback model, not the primary"

    task = ResearchTask(
        task_id=candidate.task_id,
        state=TaskState.PROPOSED,
        question=candidate.template.question,
        family_id=candidate.template.family_id,
        required_datasets=candidate.template.required_datasets,
        created_at=now,
        updated_at=now,
    )
    try:
        created = director.open_task(
            task,
            actor=f"proposer/{proposer_version}",
            reason=(
                f"{candidate.template.template_id} v{candidate.template.version} selected by "
                f"{selection.selector}: {selection.reason}"
            ),
            detail=provenance,
        )
    except ValueError:
        # Lost a race, or the caller passed a stale `existing_task_ids`. Either way the task is
        # already there and this attempt must not create a second one. Reported, not swallowed.
        return ProposalOutcome(
            created=None,
            proposal_id=identifier,
            selection=selection,
            readiness=readiness,
            eligible=eligible,
            rejected=rejected,
            reason=f"{candidate.task_id} already exists; this attempt created nothing",
            provenance=provenance,
        )

    return ProposalOutcome(
        created=created,
        proposal_id=identifier,
        selection=selection,
        readiness=readiness,
        eligible=eligible,
        rejected=rejected,
        reason=f"opened {created.task_id}",
        provenance=provenance,
    )


__all__ = [
    "CATALOG",
    "PROPOSER_VERSION",
    "SELECTION_PROMPT",
    "Candidate",
    "CandidateChoice",
    "CandidateSelector",
    "DatasetReadiness",
    "FirstEligibleSelector",
    "ModelCandidateSelector",
    "ProposalOutcome",
    "ProposalRefused",
    "ProposalTemplate",
    "Selection",
    "candidate_slate",
    "deterministic_task_id",
    "measure_readiness",
    "proposal_id",
    "propose",
    "template_by_id",
]
