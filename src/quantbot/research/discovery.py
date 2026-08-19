"""Propose candidates without spending the evidence that would test them (#13).

The generator is the component most able to destroy this project's remaining statistical
capacity, so it is the one hemmed in hardest. Four rules, all mechanical.

**A generator that looked at protected evaluation data has already spent it.** A candidate
declares the roles it inspected, and one that inspected `PROTECTED_EVALUATION` can never be
registered against that dataset -- there is no confirmatory test left to run, because the answer
was seen before the question was frozen. This is the isolation rule as a refusal rather than as
a deployment convention.

**Data-dependent search reports everything it looked at, not what it returned.** A miner that
evaluates 900 candidates and proposes 3 spent 900 trials. `search_cardinality` counts the
discarded ones, and #14 charges for them.

**Cross-domain analogy earns zero evidentiary credit.** It may generate candidates -- analogies
are a fine source of questions -- but `plausibility_credit()` returns zero for it, and no mode
can bypass the power gate or the critic. A generated story about why something might work is not
a reason to believe it does.

**A mode that keeps producing candidates which fail cheap gates gets throttled.** Yield is
measured as the share reaching registration, not as candidate count. Optimising for volume
against a fixed dataset is how a research programme destroys its own dataset, and the throttle
is what stops throughput being rewarded.

And the constraint that shapes everything: `REFUTED.md` records that cycles 2-10 consumed every
out-of-sample window on 2016-2026 US equities. `exploratory_only()` says so mechanically. With
an exhausted window a generated idea stays exploratory however good it looks, until new
protected data or authentic forward evidence exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.research.sources import EvidenceBasis

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class DiscoveryMode(StrEnum):
    LITERATURE_GAP = "LITERATURE_GAP"
    CONTRADICTION = "CONTRADICTION"
    #: Mining DISCOVERY/VALIDATION data. The only mode that inherently spends search budget.
    DATA_ANOMALY = "DATA_ANOMALY"
    EVENT_CHAIN = "EVENT_CHAIN"
    #: May propose. Earns no evidentiary credit for the analogy itself.
    CROSS_DOMAIN_ANALOGY = "CROSS_DOMAIN_ANALOGY"
    MUTATION = "MUTATION"


#: Roles a generator may look at. Everything else is off limits until a hypothesis is frozen.
DISCOVERY_ROLES = frozenset({DataRole.DISCOVERY, DataRole.VALIDATION})


class ContaminatedGenerator(RuntimeError):
    """Raised when a candidate was produced by inspecting data it now wants to be tested on.

    Deliberately not a `ValueError`: pydantic converts those into a generic `ValidationError`,
    and contamination is the one failure here a caller has to be able to tell apart from a
    malformed field. Raised from the validator, so a contaminated candidate cannot be
    constructed at all rather than merely being rejected downstream.
    """


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Candidate(FrozenModel):
    """A proposal, in the shape #5 can accept, before anything has been frozen."""

    candidate_id: Text
    mode: DiscoveryMode
    question: Text
    family_id: Text
    #: What is allegedly overlooked, and why it might not already be priced. A hypothesis
    #: about the market's inattention, never evidence of it.
    overlooked: Text
    why_unpriced: Text
    expected_direction: Text
    horizon_observations: int = Field(ge=1)
    universe: tuple[Text, ...] = Field(min_length=1)
    features: tuple[Text, ...] = Field(min_length=1)
    required_datasets: tuple[Text, ...] = Field(min_length=1)
    #: Whether every required input is point-in-time available. A candidate that needs data
    #: nobody could have had is not a candidate.
    point_in_time_available: bool
    #: The roles this generator actually looked at while producing the candidate.
    inspected_roles: frozenset[DataRole]
    #: Everything examined, including what was discarded. Not the count proposed.
    search_cardinality: int = Field(ge=1)
    confounders: tuple[Text, ...] = Field(min_length=1)
    #: The cheapest experiment that could refute it. If there isn't one, there is no candidate.
    simplest_refutation: Text
    prior_work: tuple[Text, ...] = ()
    why_different: str | None = None
    basis: EvidenceBasis
    estimated_trials: int = Field(ge=1)
    proposed_by: Text
    proposed_at: datetime

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        forbidden = self.inspected_roles - DISCOVERY_ROLES
        if forbidden:
            raise ContaminatedGenerator(
                f"{self.candidate_id} was generated after inspecting "
                + ", ".join(sorted(role.value for role in forbidden))
                + "; a candidate cannot be tested on data that produced it"
            )
        if self.mode is DiscoveryMode.DATA_ANOMALY and not self.inspected_roles:
            raise ValueError(
                "anomaly mining inspects data by definition; declare the roles it read"
            )
        if self.mode is not DiscoveryMode.DATA_ANOMALY and self.search_cardinality > 1:
            if not self.inspected_roles:
                raise ValueError(
                    f"{self.mode.value} reports a search of {self.search_cardinality} without "
                    "naming the data it searched"
                )
        if self.prior_work and self.why_different is None:
            raise ValueError(
                "a candidate that names prior work must say what is materially different"
            )
        return self

    @property
    def inspected_protected_data(self) -> bool:
        """Always False: the validator refuses to construct one that did."""
        return bool(self.inspected_roles - DISCOVERY_ROLES)


def plausibility_credit(mode: DiscoveryMode) -> Decimal:
    """How much a mode's story counts as evidence. For analogy, deliberately nothing.

    A cross-domain analogy is a fine source of questions and no kind of support for an answer.
    Nothing here returns credit for narrative in any mode: the number is about how much prior
    weight the generation path earns, and the answer is always zero. It exists as a function so
    a later change to that has to be made deliberately and read as what it is.
    """
    _ = mode
    return Decimal("0")


def exploratory_only(
    consumption: Sequence[WindowConsumption], *, forward_evidence: bool = False
) -> bool:
    """Whether a candidate can only ever be exploratory on the data available.

    Cycles 2-10 consumed every out-of-sample window on 2016-2026 US equities. Against an
    exhausted window a generated idea stays exploratory however good it looks, unless there is
    new protected data or authentic forward evidence.
    """
    if forward_evidence:
        return False
    return any(not window.usable for window in consumption)


class Generator(Protocol):
    """What a discovery mode has to provide. An LLM one drops in here (#9 routes it)."""

    @property
    def mode(self) -> DiscoveryMode: ...

    def propose(self, *, now: datetime) -> tuple[Candidate, ...]: ...


class ModeYield(FrozenModel):
    """How a discovery mode is actually doing, measured downstream rather than by volume."""

    mode: DiscoveryMode
    proposed: int = Field(default=0, ge=0)
    registered: int = Field(default=0, ge=0)
    underpowered: int = Field(default=0, ge=0)
    critic_rejected: int = Field(default=0, ge=0)
    duplicate: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_yield(self) -> Self:
        accounted = self.registered + self.underpowered + self.critic_rejected + self.duplicate
        if accounted > self.proposed:
            raise ValueError("more outcomes than candidates proposed")
        return self

    @property
    def registration_rate(self) -> Decimal:
        """The only rate that matters. Candidate count is not an achievement."""
        if self.proposed == 0:
            return Decimal("0")
        return (Decimal(self.registered) / Decimal(self.proposed)).quantize(Decimal("0.0001"))

    def throttled(self, *, minimum_rate: Decimal, minimum_sample: int = 10) -> bool:
        """Whether this mode should stop until someone looks at it.

        Deliberately not "reward the productive mode": a mode producing candidates that fail
        cheap gates is spending trials to learn nothing, and against a fixed dataset that is
        how a research programme destroys its own dataset.
        """
        return self.proposed >= minimum_sample and self.registration_rate < minimum_rate


def total_search_cost(candidates: Sequence[Candidate]) -> int:
    """What the generation actually cost in trials, including everything discarded."""
    return sum(candidate.search_cardinality for candidate in candidates)


def admissible(
    candidate: Candidate,
    *,
    consumption: Sequence[WindowConsumption],
    forward_evidence: bool = False,
) -> tuple[bool, str]:
    """Whether this candidate is worth carrying into registration, and why not if not.

    Deliberately cheap and deliberately before the power gate and the critic: those cost
    compute, and a candidate whose data cannot support a confirmatory answer should not reach
    them at all.
    """
    if not candidate.point_in_time_available:
        return False, "requires data that was not point-in-time available"
    if exploratory_only(consumption, forward_evidence=forward_evidence):
        return (
            False,
            "every requested window is exhausted, so this can only ever be exploratory; it "
            "needs new protected data or authentic forward evidence",
        )
    return True, "admissible"


__all__ = [
    "DISCOVERY_ROLES",
    "Candidate",
    "ContaminatedGenerator",
    "DiscoveryMode",
    "Generator",
    "ModeYield",
    "admissible",
    "exploratory_only",
    "plausibility_credit",
    "total_search_cost",
]
