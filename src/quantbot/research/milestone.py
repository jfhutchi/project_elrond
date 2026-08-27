"""How far the project is from an edge, measured separately from how far it is from a feature (#52).

Elrond can keep becoming more impressive without becoming more profitable. Infrastructure and
model integrations are easy to measure -- issues closed, frameworks wired in, panels added -- and
alpha is not, so issue closure quietly becomes a surrogate objective. A list of completed
architecture issues then visually overwhelms the fact that the economic thesis is unproven.

This module makes the second number as legible as the first, and derives it from durable state so
it cannot be improved by writing about it. Every level answers to evidence a query can find:

| Level | Reached when |
|---|---|
| `M0_RESEARCH_PLATFORM_OPERATIONAL` | the platform has run experiments and honestly refuted them |
| `M1_CANDIDATE_EDGE_DETECTED` | a frozen candidate cleared the survivor gate, unrefuted |
| `M2_EDGE_GENERALIZES` | that claim spans several instruments and a finding records it surviving |
| `M3_FORWARD_EDGE_OBSERVED` | authentic forward sessions are accruing against the live claim |
| `M4_FORWARD_EDGE_SUPPORTED` | `PAPER_QUALIFIED` under #47, with capital economics under #51 |
| `M5_HUMAN_LIVE_REVIEW_ELIGIBLE` | every gate is clear and a human may look. Not authorisation |

**The ladder is contiguous.** `current` is the highest level reached with every level below it
also reached, so a gap cannot be stepped over. A deployment that somehow showed forward evidence
with no surviving candidate behind it would report M0, not M3, and the M1 check would say why.

**A level that cannot be assessed does not pass.** `unassessed` blocks, exactly as it does in #7
and in forward evidence. M4 currently carries one permanently: #51 is open, so nothing here can
say whether an effect survives at the intended capital rather than at the paper account's $100.
That is the honest state, and reporting M4 without it would be the surrogate-objective failure
this module exists to prevent, arriving through the module itself.

**No calendar deadline, deliberately.** #52 asks for the milestone to constrain *claims and
resource allocation*, not to pressure agents into mining until something wins. Nothing here
expires, and there is no level that gets easier over time.

**`NO_EDGE_ESTABLISHED` is a legitimate destination.** `no_edge_established` reports when the
project sits at M0 with no clean confirmatory capacity left for the families it has searched.
That is a research result, not a software failure, and having a name for it is what stops the
alternative -- adding another framework because the ladder has no way to say "stop".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from sqlalchemy.orm import Session

from quantbot.research.capacity import CapacityStatus, capacity_inventory
from quantbot.research.memory import RecordKind, ResearchMemory, Verdict
from quantbot.research.promotion import (
    MINIMUM_FORWARD_DAYS,
    MINIMUM_FORWARD_TRADES,
    DeploymentRole,
    Stage,
    count_forward_days,
    deployment_role,
    observe_forward_days,
    stage_rank,
)
from quantbot.research.registry import HypothesisRegistry
from quantbot.storage.repositories import PromotionRecord, StorageRepository


class Milestone(StrEnum):
    """Edge milestones, defined independently of software version and issue completion.

    Deliberately not named after releases. A version number moves when code ships; these move
    only when evidence about the market changes, and conflating the two is the whole defect.
    """

    M0_RESEARCH_PLATFORM_OPERATIONAL = "M0_RESEARCH_PLATFORM_OPERATIONAL"
    M1_CANDIDATE_EDGE_DETECTED = "M1_CANDIDATE_EDGE_DETECTED"
    M2_EDGE_GENERALIZES = "M2_EDGE_GENERALIZES"
    M3_FORWARD_EDGE_OBSERVED = "M3_FORWARD_EDGE_OBSERVED"
    M4_FORWARD_EDGE_SUPPORTED = "M4_FORWARD_EDGE_SUPPORTED"
    M5_HUMAN_LIVE_REVIEW_ELIGIBLE = "M5_HUMAN_LIVE_REVIEW_ELIGIBLE"


LADDER: tuple[Milestone, ...] = tuple(Milestone)


@dataclass(frozen=True, slots=True)
class MilestoneCheck:
    """One level, whether it is reached, and the durable artifacts that say so."""

    level: Milestone
    #: Named registrations, strategies and records. Never prose about them, and never empty
    #: on a reached level: a level with no artifact behind it is an opinion.
    evidence: tuple[str, ...]
    blocking: tuple[str, ...]
    #: Checks that could not run. Blocking, for the reason `unassessed` always is here.
    unassessed: tuple[str, ...]

    @property
    def reached(self) -> bool:
        return not self.blocking and not self.unassessed and bool(self.evidence)

    def explain(self) -> str:
        reasons = [*self.blocking, *(f"not assessed: {item}" for item in self.unassessed)]
        if not reasons:
            return f"{self.level.value}: " + "; ".join(self.evidence)
        return f"{self.level.value} not reached: " + "; ".join(reasons)


@dataclass(frozen=True, slots=True)
class MilestoneReport:
    """Where the project's economic thesis actually stands, and what would move it."""

    as_of: date
    checks: tuple[MilestoneCheck, ...]
    #: Strategies whose bound claim survived research and has not been refuted.
    candidate_edges: tuple[str, ...]
    #: Strategies whose forward evidence supports the claim under #47.
    forward_supported: tuple[str, ...]
    #: Observation identities with confirmatory capacity left, for the resource-allocation
    #: question: is there anywhere left to test the next idea?
    clean_capacity: tuple[str, ...]
    #: Why the capacity picture could not be trusted, when it could not. Reserved for the
    #: genuinely indeterminate case -- an inventory that answered nothing -- and deliberately
    #: *not* used for `UNSUPPORTED` or `QUALITY_BLOCKED`, which are determinate answers about a
    #: known state. Keeps "we searched everything" apart from "we could not see anything",
    #: which `no_edge_established` must never confuse (#52).
    capacity_undetermined: tuple[str, ...] = ()
    #: Research directions the project cannot currently reach at all: a declared capability with
    #: no provider behind it. Not a reason to suppress the conclusion, and a reason to qualify
    #: it -- "no edge established" means on what this project can currently search.
    capacity_unavailable: tuple[str, ...] = ()

    @property
    def current(self) -> Milestone | None:
        """The highest level reached with every level below it also reached.

        Contiguous on purpose. A gap means the evidence chain is broken somewhere, and reporting
        the top of a broken chain is how a system claims a conclusion it never derived.

        `None` means the M0 check itself did not pass -- there is no level below M0 to name, and
        defaulting to M0 would report a platform as operational on the strength of having been
        asked. On a store with no refutations that is exactly what happens, and it is the right
        answer: a platform that has never rejected a hypothesis has not shown it can.
        """
        reached: Milestone | None = None
        for check in self.checks:
            if not check.reached:
                break
            reached = check.level
        return reached

    @property
    def next_check(self) -> MilestoneCheck | None:
        """The level the project is stuck on, with its reasons. `None` only at M5."""
        for check in self.checks:
            if not check.reached:
                return check
        return None

    @property
    def no_edge_established(self) -> bool:
        """Whether the honest conclusion available today is that there is no edge.

        Three conditions, and the third is the one #52's review asked for. It reports that the
        project has searched what it can search and found nothing -- a research result, not a
        failure. So it must not fire because the *inventory* is broken:

        * the ladder is at its bottom rung, and
        * no source has clean confirmatory capacity left, and
        * the inventory actually answered.

        `UNSUPPORTED` and `QUALITY_BLOCKED` do **not** suppress it. They are determinate answers
        about a known state, and treating them as indeterminate would make this conclusion
        unreachable for as long as any capability is unimplemented -- a system that can never say
        "no edge" always has a reason to keep going, which is the failure in the other
        direction. They are reported in `capacity_unavailable` so the conclusion reads as what
        it is: no edge established on what this project can currently search.

        Without the third, an empty or unreadable inventory would produce this project's most
        consequential conclusion by accident. "We searched everywhere and found nothing" and
        "we could not see anywhere to search" are different statements, and only the first is a
        finding. Having a name for the first is what stops the alternative -- adding another
        framework because nothing else can be said.
        """
        at_the_bottom = self.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL
        return at_the_bottom and not self.clean_capacity and not self.capacity_undetermined

    def explain(self) -> str:
        stuck = self.next_check
        headline = "BELOW_M0" if self.current is None else self.current.value
        return headline if stuck is None else f"{headline}; {stuck.explain()}"


def _survivors(session: Session) -> list[tuple[PromotionRecord, str]]:
    """Strategies standing at or above `RESEARCH_SURVIVOR` on a claim nothing has refuted.

    The survivor gate is what makes this M1 rather than an opinion: `survivor_objections`
    requires the #7 probes to have run and passed, the production evaluation path, a test valid
    for the dependence structure of the data, and the frozen luck bar cleared. A strategy
    standing above it did those things, and `deployment_role` re-derives the refutation half now
    rather than trusting that it was true when the stage was granted.
    """
    found: list[tuple[PromotionRecord, str]] = []
    for record in StorageRepository(session).list_promotions():
        if stage_rank(Stage(record.stage)) < stage_rank(Stage.RESEARCH_SURVIVOR):
            continue
        if record.hypothesis_id is None:
            continue
        if deployment_role(session, record.strategy_id) is not DeploymentRole.EDGE_CANDIDATE:
            continue
        found.append((record, f"{record.strategy_id} -> {record.hypothesis_id}"))
    return found


def _platform_check(session: Session) -> MilestoneCheck:
    memory = ResearchMemory(session)
    refutations = [
        record
        for record in memory.recall("", kinds=[RecordKind.FINDING])
        if record.verdict is Verdict.REFUTED
    ]
    registrations = HypothesisRegistry(session).list_registrations()
    evidence: list[str] = []
    blocking: list[str] = []
    if refutations:
        evidence.append(f"{len(refutations)} refutations recorded")
    else:
        # A platform that has never refuted anything has not demonstrated it can. Volume of
        # positive results is the opposite of evidence here.
        blocking.append(
            "research memory holds no refutation, so nothing shows this platform can reject a "
            "hypothesis rather than accumulate them"
        )
    if registrations:
        evidence.append(f"{len(registrations)} frozen registrations")
    return MilestoneCheck(
        level=Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL,
        evidence=tuple(evidence),
        blocking=tuple(blocking),
        unassessed=(),
    )


def _candidate_check(survivors: Sequence[tuple[PromotionRecord, str]]) -> MilestoneCheck:
    if not survivors:
        return MilestoneCheck(
            level=Milestone.M1_CANDIDATE_EDGE_DETECTED,
            evidence=(),
            blocking=(
                "no strategy stands at RESEARCH_SURVIVOR or above on an unrefuted frozen claim; "
                "a candidate edge is a registration that passed its own test, not a promising "
                "backtest",
            ),
            unassessed=(),
        )
    return MilestoneCheck(
        level=Milestone.M1_CANDIDATE_EDGE_DETECTED,
        evidence=tuple(label for _, label in survivors),
        blocking=(),
        unassessed=(),
    )


def _generalises_check(
    session: Session, survivors: Sequence[tuple[PromotionRecord, str]]
) -> MilestoneCheck:
    """Whether a surviving mechanism was shown to work beyond the thing it was found on.

    Two durable facts have to agree, and neither is an opinion:

    1. **The registered universe spans more than one instrument.** `compile_experiment` adds
       `cross-asset-generality` to the probe plan exactly then, and `survivor_objections` refuses
       a survivor whose planned probes did not all run and pass. So a multi-instrument claim
       standing above `RESEARCH_SURVIVOR` has cleared cross-asset generality. A single-instrument
       claim has not and cannot: generality is not a property one asset can demonstrate.
    2. **Research memory holds a `SURVIVED` finding for that claim.** The cycle that ran the
       robustness work filed it. Requiring both means neither a plan nor a record alone is enough.

    *Recorded limitation.* Probe results are not stored in a queryable table, so (1) reads the
    probe requirement the universe implies rather than the probe list that ran. If a second
    experiment compiler ever appears, this inference stops holding, and
    `test_a_multi_instrument_registration_implies_the_generality_probe` is what fails.
    """
    if not survivors:
        return MilestoneCheck(
            level=Milestone.M2_EDGE_GENERALIZES,
            evidence=(),
            blocking=("no candidate edge to generalise",),
            unassessed=(),
        )
    registry = HypothesisRegistry(session)
    memory = ResearchMemory(session)
    evidence: list[str] = []
    blocking: list[str] = []
    for record, label in survivors:
        assert record.hypothesis_id is not None
        registration = registry.get(record.hypothesis_id, record.hypothesis_version or 1)
        if registration is None:
            blocking.append(f"{label}: the bound registration is not in the registry")
            continue
        if len(registration.draft.universe) < 2:
            blocking.append(
                f"{label}: the claim covers a single instrument "
                f"({registration.draft.universe[0]}), so cross-asset generality was never in its "
                "probe plan and cannot have been shown"
            )
            continue
        survived = [
            item
            for item in memory.findings_for(record.hypothesis_id)
            if item.kind is RecordKind.FINDING and item.verdict is Verdict.SURVIVED
        ]
        if not survived:
            blocking.append(
                f"{label}: no durable SURVIVED finding records the mechanism holding up under "
                "its robustness challenges"
            )
            continue
        evidence.append(
            f"{label} over {len(registration.draft.universe)} instruments, "
            + ", ".join(item.record_id for item in survived)
        )
    return MilestoneCheck(
        level=Milestone.M2_EDGE_GENERALIZES,
        evidence=tuple(evidence),
        blocking=tuple(blocking) if not evidence else (),
        unassessed=(),
    )


def _forward_observed_check(
    session: Session, survivors: Sequence[tuple[PromotionRecord, str]]
) -> MilestoneCheck:
    """Authentic forward sessions on an edge candidate, and enough of them to have a floor.

    Deliberately *not* an economic verdict. M3 says forward data exists and is being collected
    against a live claim; M4 is where it has to mean something. Splitting them is what stops a
    thin, noisy, favourable stretch reading as support.
    """
    if not survivors:
        return MilestoneCheck(
            level=Milestone.M3_FORWARD_EDGE_OBSERVED,
            evidence=(),
            blocking=("no candidate edge is accumulating forward evidence",),
            unassessed=(),
        )
    evidence: list[str] = []
    blocking: list[str] = []
    for record, label in survivors:
        observations = observe_forward_days(
            session,
            strategy_id=record.strategy_id,
            strategy_version=record.strategy_version,
            configuration_hash=record.configuration_hash,
        )
        days, trades = count_forward_days(
            observations,
            strategy_version=record.strategy_version,
            configuration_hash=record.configuration_hash,
        )
        if days < MINIMUM_FORWARD_DAYS or trades < MINIMUM_FORWARD_TRADES:
            blocking.append(
                f"{label}: {days}/{MINIMUM_FORWARD_DAYS} authentic forward days and "
                f"{trades}/{MINIMUM_FORWARD_TRADES} trades"
            )
            continue
        evidence.append(f"{label}: {days} forward days, {trades} trades")
    return MilestoneCheck(
        level=Milestone.M3_FORWARD_EDGE_OBSERVED,
        evidence=tuple(evidence),
        blocking=tuple(blocking) if not evidence else (),
        unassessed=(),
    )


def _forward_supported_check(session: Session) -> MilestoneCheck:
    """`PAPER_QUALIFIED` under #47, plus the capacity question #51 asks and nothing answers.

    The `unassessed` entry is permanent until #51 lands, and that is the point. The paper account
    holds $100. An effect that survives at $100 of notional and dies at $10,000 is not an edge
    worth the machinery, and no amount of forward evidence at the smaller size distinguishes the
    two. Reporting M4 without it would be exactly the surrogate-objective failure this module
    exists to prevent.
    """
    qualified = [
        record.strategy_id
        for record in StorageRepository(session).list_promotions()
        if stage_rank(Stage(record.stage)) >= stage_rank(Stage.PAPER_QUALIFIED)
    ]
    blocking: list[str] = []
    if not qualified:
        blocking.append(
            "no strategy has reached PAPER_QUALIFIED, which under #47 needs a measured, "
            "benchmark-relative, adequately powered positive result against a ratified mandate"
        )
    return MilestoneCheck(
        level=Milestone.M4_FORWARD_EDGE_SUPPORTED,
        evidence=tuple(qualified),
        blocking=tuple(blocking),
        unassessed=(
            "intended-capital execution capacity (#51): the paper account holds $100 and the "
            "mandate's range reaches $10,000; nothing yet measures whether an effect survives "
            "at the size it would be traded at",
        ),
    )


def _live_review_check(session: Session) -> MilestoneCheck:
    eligible = [
        record.strategy_id
        for record in StorageRepository(session).list_promotions()
        if Stage(record.stage) is Stage.LIVE_REVIEW_ELIGIBLE
    ]
    return MilestoneCheck(
        level=Milestone.M5_HUMAN_LIVE_REVIEW_ELIGIBLE,
        evidence=tuple(eligible),
        blocking=(
            ()
            if eligible
            else ("no strategy is eligible for human review; this is the terminal stage and "
                  "reaching it is still not authorisation to trade real capital",)
        ),
        unassessed=(),
    )


def assess_milestone(session: Session, *, as_of: date) -> MilestoneReport:
    """Where the project's economic thesis stands, derived from durable state alone.

    Nothing here reads a prose status file, a version number or a count of closed issues. Those
    are the things that move when engineering happens, and #52 exists because they were the only
    numbers anything reported.
    """
    survivors = _survivors(session)
    checks = (
        _platform_check(session),
        _candidate_check(survivors),
        _generalises_check(session, survivors),
        _forward_observed_check(session, survivors),
        _forward_supported_check(session),
        _live_review_check(session),
    )
    inventory = capacity_inventory(session, as_of=as_of)
    clean = tuple(
        row.observation_identity
        for row in inventory
        if row.usable_for_confirmation and row.status is not CapacityStatus.EXHAUSTED
    )
    unavailable = tuple(
        row.observation_identity
        for row in inventory
        if row.status in {CapacityStatus.UNSUPPORTED, CapacityStatus.QUALITY_BLOCKED}
    )
    # An inventory that answered nothing is the only genuinely indeterminate case. Treating
    # `UNSUPPORTED` as indeterminate too would make the conclusion unreachable for as long as
    # any capability is unimplemented, which is its own dishonesty in the opposite direction:
    # a system that can never say "no edge" always has a reason to keep going.
    undetermined = () if inventory else ("no sources were inventoried at all",)
    return MilestoneReport(
        as_of=as_of,
        checks=checks,
        candidate_edges=tuple(label for _, label in survivors),
        forward_supported=checks[4].evidence,
        clean_capacity=clean,
        capacity_undetermined=undetermined,
        capacity_unavailable=unavailable,
    )


__all__ = [
    "LADDER",
    "Milestone",
    "MilestoneCheck",
    "MilestoneReport",
    "assess_milestone",
]
