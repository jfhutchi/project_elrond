"""The stage that spends something non-renewable, and the ways it must not (#3, #7, #5)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantbot.research.critic import CriticVerdict, Critique, Objection, Severity
from quantbot.research.director import ResearchTask, TaskState
from quantbot.research.power import (
    ComparisonStructure,
    EconomicProfile,
    EffectSpecification,
    Estimand,
)
from quantbot.research.registry import (
    DataRole,
    DataWindow,
    HypothesisDraft,
    HypothesisRegistry,
    SearchOrigin,
)
from quantbot.research.sources import EpistemicStatus, EvidenceBasis
from quantbot.research.stages import critique_stage
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DATASET = "fred-macro-daily"


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "critique.db")
    yield db
    db.close()


def draft() -> HypothesisDraft:
    return HypothesisDraft(
        hypothesis_id="H-CRIT-1",
        family_id="revisions",
        question="Does revision dispersion precede index drawdowns?",
        prediction="Annualised Sharpe is at least 1.0.",
        null_hypothesis="Dispersion carries no information.",
        falsified_if="The Sharpe fails to clear the luck bar.",
        universe=("SPY",),
        features=("revision_dispersion",),
        target="next-month maximum drawdown",
        windows=(
            DataWindow(
                dataset=DATASET,
                snapshot="2026-08-21",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(1990, 1, 2),
                end=date(2026, 8, 14),
            ),
        ),
        primary_estimand="annualised Sharpe against a null of zero",
        effect=EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("2.0"),
            minimum_practical=Decimal("1.0"),
            justification="Large enough that 9000 observations can resolve it.",
            comparison=ComparisonStructure.SINGLE_SAMPLE,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        available_observations=9000,
        search_cardinality=1,
        search_origin=SearchOrigin.NO_DATA_DEPENDENT_SEARCH,
        confounders=("dispersion rises in volatile regimes",),
        basis=EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE),
        proposed_by="claude-opus-5",
    )


class ScriptedCritic:
    def __init__(self, name: str, verdict: CriticVerdict, *, blocking: bool = False) -> None:
        self.name = name
        self._verdict = verdict
        self._blocking = blocking

    def review(self, hypothesis_id: str, version: int, *, now: datetime) -> Critique:
        # `Critique` requires a REVISE or REJECT to name what is wrong -- a verdict against
        # something with no objection behind it is a vote. So anything other than PROCEED
        # carries one, and `blocking` decides how severe.
        needs_objection = self._blocking or self._verdict is not CriticVerdict.PROCEED
        objections = (
            (
                Objection(
                    check="economic mechanism plausibility",
                    severity=Severity.BLOCKING if self._blocking else Severity.ADVISORY,
                    finding="no reason is given why this should be unpriced",
                    evidence={"source": "model-judgment"},
                ),
            )
            if needs_objection
            else ()
        )
        return Critique(
            hypothesis_id=hypothesis_id,
            hypothesis_version=version,
            critic=self.name,
            verdict=self._verdict,
            reasons=(f"{self.name} says {self._verdict.value}",),
            objections=objections,
            unassessed=(f"{self.name}-abstained",),
            produced_at=now,
        )


def task(state: TaskState = TaskState.CRITIQUE) -> ResearchTask:
    return ResearchTask(
        task_id="T-CRIT-1",
        state=state,
        question="Does revision dispersion precede index drawdowns?",
        family_id="revisions",
        created_at=NOW,
        updated_at=NOW,
    )


def test_a_critics_rejection_blocks_and_never_refutes(database: Database) -> None:
    """`REFUTED` means measured and disproven. A critic saying it looks wrong is neither.

    The state machine makes `REFUTED` terminal, so a handler that could put a task there on a
    critic's say-so would permanently retire ideas on the strength of a review. This is the
    same error class as a crashed cycle stage being recorded as a refutation.
    """
    with database.transaction() as session:
        handler = critique_stage(
            [ScriptedCritic("harsh", CriticVerdict.REJECT)],
            HypothesisRegistry(session),
            lambda _task: draft(),
        )

        result = handler(task())

    assert result.next_state is TaskState.BLOCKED
    assert result.next_state is not TaskState.REFUTED
    assert "rejected on review" in result.reason


def test_a_revise_verdict_sends_the_task_back_rather_than_ending_it(database: Database) -> None:
    """The revision loop working: a fixable problem is not a dead end."""
    with database.transaction() as session:
        handler = critique_stage(
            [ScriptedCritic("careful", CriticVerdict.REVISE)],
            HypothesisRegistry(session),
            lambda _task: draft(),
        )

        result = handler(task())

    assert result.next_state is TaskState.PROPOSED
    assert "revision required" in result.reason


def test_a_surviving_hypothesis_is_registered_with_its_critique_frozen_in(
    database: Database,
) -> None:
    """`register()` requires the critique because a review revisable after the result is not one.

    Both critics' objections survive into the frozen record. Picking one review would discard
    the other -- and if the deterministic one were dropped, every computed objection with it.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        handler = critique_stage(
            [
                ScriptedCritic("deterministic", CriticVerdict.PROCEED),
                ScriptedCritic("model", CriticVerdict.PROCEED),
            ],
            registry,
            lambda _task: draft(),
        )

        result = handler(task())
        stored = registry.get("H-CRIT-1", 1)

    assert result.next_state is TaskState.REGISTERED
    assert stored is not None
    assert stored.critique.critic == "deterministic + model"
    assert len(stored.critique.reasons) == 2


def test_one_proceed_cannot_carry_a_blocking_objection_past_the_gate(
    database: Database,
) -> None:
    """Agreement does not outvote an objection, and the merge cannot smuggle one through.

    `consensus` takes the most severe verdict, and `Critique` refuses PROCEED alongside a
    blocking objection -- so a merge that kept the objection and the mild verdict would not even
    construct.
    """
    with database.transaction() as session:
        handler = critique_stage(
            [
                ScriptedCritic("agreeable", CriticVerdict.PROCEED),
                ScriptedCritic("model", CriticVerdict.REVISE, blocking=True),
            ],
            HypothesisRegistry(session),
            lambda _task: draft(),
        )

        result = handler(task())

    assert result.next_state is TaskState.PROPOSED, "the objection wins, not the majority"


def test_critics_approving_does_not_entitle_a_hypothesis_to_a_window_it_cannot_have(
    database: Database,
) -> None:
    """A gate the critics do not run still gets to refuse.

    Registering the same hypothesis twice contends for the same protected window. The critics
    have no opinion about that, and their approval must not propagate as an exception.
    """
    with database.transaction() as session:
        registry = HypothesisRegistry(session)
        handler = critique_stage(
            [ScriptedCritic("deterministic", CriticVerdict.PROCEED)],
            registry,
            lambda _task: draft(),
        )
        first = handler(task())
        second = handler(task())

    assert first.next_state is TaskState.REGISTERED
    assert second.next_state is TaskState.BLOCKED
    assert "registration was refused" in second.reason


def test_no_critic_at_all_is_not_approval(database: Database) -> None:
    """The same rule `DeterministicCritic` applies to a run in which no check fired."""
    with database.transaction() as session:
        handler = critique_stage([], HypothesisRegistry(session), lambda _task: draft())

        result = handler(task())

    assert result.next_state is TaskState.BLOCKED
    assert "unreviewed hypothesis is not an approved one" in result.reason


def test_a_task_with_no_draft_cannot_be_reviewed_in_the_abstract(database: Database) -> None:
    with database.transaction() as session:
        handler = critique_stage(
            [ScriptedCritic("deterministic", CriticVerdict.PROCEED)],
            HypothesisRegistry(session),
            lambda _task: None,
        )

        result = handler(task())

    assert result.next_state is TaskState.BLOCKED
    assert "no draft" in result.reason
