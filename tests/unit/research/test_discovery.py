"""What a generator may look at, what its search costs, and what its stories are worth."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from quantbot.research.discovery import (
    DISCOVERY_ROLES,
    Candidate,
    ContaminatedGenerator,
    DiscoveryMode,
    ModeYield,
    admissible,
    exploratory_only,
    plausibility_credit,
    total_search_cost,
)
from quantbot.research.registry import DataRole, WindowConsumption
from quantbot.research.sources import EpistemicStatus, EvidenceBasis

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)

UNTOUCHED = WindowConsumption(
    dataset="global-country-indices-daily", status="UNTOUCHED", trials=0, consumers=()
)
EXHAUSTED = WindowConsumption(
    dataset="sip-us-equities-daily",
    status="EXHAUSTED",
    trials=68,
    consumers=("cycles 2-10",),
)


def candidate(**overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "candidate_id": "C-1",
        "mode": DiscoveryMode.LITERATURE_GAP,
        "question": "Does implied-volatility skew predict cross-sectional equity returns?",
        "family_id": "options-derived",
        "overlooked": "Skew is studied as a risk measure, rarely as a cross-sectional signal.",
        "why_unpriced": "Options data is expensive, so few retail systems use it.",
        "expected_direction": "High-skew names underperform over the following month.",
        "horizon_observations": 21,
        "universe": ("SPY", "QQQ"),
        "features": ("iv_skew_30d",),
        "required_datasets": ("options-implied-daily",),
        "point_in_time_available": True,
        "inspected_roles": frozenset({DataRole.DISCOVERY}),
        "search_cardinality": 1,
        "confounders": ("skew correlates with realised volatility",),
        "simplest_refutation": "Sort on skew and measure the top-minus-bottom spread.",
        "basis": EvidenceBasis(
            citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE
        ),
        "estimated_trials": 1,
        "proposed_by": "literature-gap-miner",
        "proposed_at": NOW,
    }
    fields.update(overrides)
    return Candidate(**fields)


def test_a_generator_that_read_protected_data_has_already_spent_it() -> None:
    """There is no confirmatory test left: the answer was seen before the question was frozen."""
    with pytest.raises(ContaminatedGenerator) as error:
        candidate(inspected_roles=frozenset({DataRole.DISCOVERY, DataRole.PROTECTED_EVALUATION}))
    assert "cannot be tested on data that produced it" in str(error.value)

    with pytest.raises(ContaminatedGenerator):
        candidate(inspected_roles=frozenset({DataRole.FORWARD_PAPER}))

    # Discovery and validation are what a generator may read.
    assert DISCOVERY_ROLES == frozenset({DataRole.DISCOVERY, DataRole.VALIDATION})
    assert candidate(inspected_roles=DISCOVERY_ROLES).inspected_protected_data is False


def test_a_search_reports_what_it_examined_not_what_it_returned() -> None:
    """A miner that evaluates 900 and proposes 3 spent 900."""
    mined = [
        candidate(
            candidate_id=f"C-{index}",
            mode=DiscoveryMode.DATA_ANOMALY,
            search_cardinality=300,
        )
        for index in range(3)
    ]
    assert total_search_cost(mined) == 900

    # And a mode claiming a search without naming the data it searched is refused.
    with pytest.raises(ValueError, match="without naming the data it searched"):
        candidate(
            mode=DiscoveryMode.LITERATURE_GAP,
            search_cardinality=50,
            inspected_roles=frozenset(),
        )


def test_anomaly_mining_must_declare_the_data_it_read() -> None:
    with pytest.raises(ValueError, match="declare the roles it read"):
        candidate(mode=DiscoveryMode.DATA_ANOMALY, inspected_roles=frozenset())


def test_a_generated_story_is_worth_nothing_as_evidence_in_any_mode() -> None:
    """Analogy may propose. It may not support."""
    assert plausibility_credit(DiscoveryMode.CROSS_DOMAIN_ANALOGY) == Decimal("0")
    assert all(plausibility_credit(mode) == Decimal("0") for mode in DiscoveryMode)

    # And an analogical candidate still has to carry a refutation like any other.
    analogy = candidate(mode=DiscoveryMode.CROSS_DOMAIN_ANALOGY)
    assert analogy.simplest_refutation


def test_a_candidate_that_names_prior_work_must_say_what_is_different() -> None:
    """Momentum returned null on three universes because each had a plausible excuse."""
    with pytest.raises(ValueError, match="materially different"):
        candidate(mode=DiscoveryMode.MUTATION, prior_work=("H-2026-001 v1",))

    mutation = candidate(
        mode=DiscoveryMode.MUTATION,
        prior_work=("H-2026-001 v1",),
        why_different="a different asset class with independent microstructure",
    )
    assert mutation.why_different


def test_an_exhausted_window_makes_a_candidate_exploratory_however_good_it_looks() -> None:
    """Cycles 2-10 consumed every out-of-sample window on 2016-2026 US equities."""
    assert exploratory_only([EXHAUSTED]) is True
    assert exploratory_only([UNTOUCHED]) is False
    assert exploratory_only([UNTOUCHED, EXHAUSTED]) is True

    # Authentic forward evidence is the one thing that changes the answer.
    assert exploratory_only([EXHAUSTED], forward_evidence=True) is False


def test_admission_is_cheap_and_happens_before_the_expensive_gates() -> None:
    ok, reason = admissible(candidate(), consumption=[UNTOUCHED])
    assert ok
    assert reason == "admissible"

    blocked, why = admissible(candidate(), consumption=[EXHAUSTED])
    assert not blocked
    assert "can only ever be exploratory" in why

    unavailable, missing = admissible(
        candidate(point_in_time_available=False), consumption=[UNTOUCHED]
    )
    assert not unavailable
    assert "point-in-time available" in missing


def test_a_mode_is_judged_on_what_reaches_registration_not_on_volume() -> None:
    """Optimising for candidate count against a fixed dataset destroys the dataset."""
    prolific = ModeYield(
        mode=DiscoveryMode.DATA_ANOMALY,
        proposed=100,
        registered=1,
        underpowered=60,
        critic_rejected=30,
        duplicate=9,
    )
    careful = ModeYield(
        mode=DiscoveryMode.LITERATURE_GAP, proposed=10, registered=6, underpowered=2, duplicate=2
    )

    assert prolific.registration_rate == Decimal("0.0100")
    assert careful.registration_rate == Decimal("0.6000")
    # A hundred candidates is not better than ten.
    assert prolific.proposed > careful.proposed
    assert prolific.registration_rate < careful.registration_rate

    assert prolific.throttled(minimum_rate=Decimal("0.1"))
    assert not careful.throttled(minimum_rate=Decimal("0.1"))

    # A mode is not throttled before there is enough of it to judge.
    early = ModeYield(mode=DiscoveryMode.EVENT_CHAIN, proposed=3, registered=0, duplicate=3)
    assert not early.throttled(minimum_rate=Decimal("0.1"))
    assert early.registration_rate == Decimal("0")


def test_a_mode_cannot_report_more_outcomes_than_it_proposed() -> None:
    with pytest.raises(ValueError, match="more outcomes than candidates"):
        ModeYield(mode=DiscoveryMode.MUTATION, proposed=2, registered=3)


def test_a_candidate_without_a_refutation_is_not_a_candidate() -> None:
    with pytest.raises(ValueError):
        candidate(simplest_refutation="   ")
    with pytest.raises(ValueError):
        candidate(confounders=())
