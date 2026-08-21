"""Discovery to registration: what carries across, and what must not be invented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantbot.research.bridge import CandidateNotRegistrable, draft_from_candidate
from quantbot.research.discovery import Candidate, DiscoveryMode
from quantbot.research.power import (
    ComparisonStructure,
    EconomicProfile,
    EffectSpecification,
    Estimand,
)
from quantbot.research.registry import DataRole, DataWindow, SearchOrigin, WindowConsumption
from quantbot.research.sources import EpistemicStatus, EvidenceBasis

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
DATASET = "sip-us-equities-daily"


def candidate(**overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "candidate_id": "C-1",
        "mode": DiscoveryMode.LITERATURE_GAP,
        "question": "Does revision dispersion predict index drawdowns?",
        "family_id": "revisions",
        "overlooked": "studied per-name, rarely aggregated",
        "why_unpriced": "aggregation needs data most index traders lack",
        "expected_direction": "higher dispersion precedes larger drawdowns",
        "horizon_observations": 21,
        "universe": ("SPY",),
        "features": ("revision_dispersion",),
        "required_datasets": (DATASET,),
        "point_in_time_available": True,
        "inspected_roles": frozenset(),
        "search_cardinality": 1,
        "confounders": ("dispersion rises in volatile regimes",),
        "simplest_refutation": "regress drawdown on dispersion; no relation refutes it",
        "basis": EvidenceBasis(citations=(), status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE),
        "estimated_trials": 1,
        "proposed_by": "claude-opus-5",
        "proposed_at": NOW,
    }
    fields.update(overrides)
    return Candidate(**fields)


def effect() -> EffectSpecification:
    return EffectSpecification(
        estimand=Estimand.SHARPE,
        expected=Decimal("2.0"),
        minimum_practical=Decimal("1.0"),
        justification="Large enough that the available sample can resolve it.",
        comparison=ComparisonStructure.SINGLE_SAMPLE,
        economics=EconomicProfile(
            annual_rebalances=12,
            expected_annual_volatility_bps=1500,
            round_trip_cost_bps=Decimal("1.1"),
        ),
    )


def window(dataset: str = DATASET) -> DataWindow:
    return DataWindow(
        dataset=dataset,
        snapshot="2026-08-21",
        role=DataRole.PROTECTED_EVALUATION,
        start=date(2016, 1, 4),
        end=date(2026, 8, 14),
    )


def test_a_candidate_becomes_a_registrable_draft() -> None:
    """The link that did not exist: a generated proposal can now reach the gates."""
    draft = draft_from_candidate(
        candidate(),
        effect=effect(),
        windows=[window()],
        available_observations=2669,
        hypothesis_id="H-FROM-C1",
    )

    assert draft.hypothesis_id == "H-FROM-C1"
    assert draft.question == "Does revision dispersion predict index drawdowns?"
    assert draft.universe == ("SPY",)
    assert draft.falsified_if.startswith("regress drawdown")
    assert draft.basis.status is EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE


def test_the_search_cardinality_is_carried_not_restated() -> None:
    """#23: if the registrar declared this, a search of 500 could arrive as a search of 1.

    The burden has to follow the candidate. An operator attestation names who stands behind the
    count, because an agent's unattested self-report is the origin the registry refuses -- and
    discovery is an agent.
    """
    mined = candidate(
        mode=DiscoveryMode.DATA_ANOMALY,
        inspected_roles=frozenset({DataRole.DISCOVERY}),
        search_cardinality=500,
    )

    draft = draft_from_candidate(
        mined,
        effect=effect(),
        windows=[window()],
        available_observations=2669,
        hypothesis_id="H-MINED",
        attested_by="hutch",
    )

    assert draft.search_cardinality == 500, "the count the candidate reported, not one"
    assert draft.search_origin is SearchOrigin.OPERATOR_ATTESTED
    assert draft.search_attested_by == "hutch"


def test_a_searched_candidate_without_an_attestation_is_refused() -> None:
    """An agent may not self-report a trial count, whatever route it arrives by."""
    mined = candidate(
        mode=DiscoveryMode.DATA_ANOMALY,
        inspected_roles=frozenset({DataRole.DISCOVERY}),
        search_cardinality=500,
    )

    with pytest.raises(CandidateNotRegistrable, match="operator attestation"):
        draft_from_candidate(
            mined,
            effect=effect(),
            windows=[window()],
            available_observations=2669,
            hypothesis_id="H-MINED",
        )


def test_a_candidate_that_needs_unavailable_data_never_reaches_the_power_gate() -> None:
    """Admissibility is the cheap gate and runs first.

    A candidate whose data was not point-in-time available cannot support a confirmatory answer,
    so it should not cost the compute the power gate and the critic spend.
    """
    with pytest.raises(CandidateNotRegistrable, match="not point-in-time available"):
        draft_from_candidate(
            candidate(point_in_time_available=False),
            effect=effect(),
            windows=[window()],
            available_observations=2669,
            hypothesis_id="H-1",
        )


def test_an_exhausted_window_makes_a_candidate_exploratory_only() -> None:
    """Every requested window spent means this can never be confirmatory."""
    exhausted = WindowConsumption(
        dataset=DATASET, status="EXHAUSTED", trials=40, consumers=("H-old",)
    )

    with pytest.raises(CandidateNotRegistrable, match="exhausted"):
        draft_from_candidate(
            candidate(),
            effect=effect(),
            windows=[window()],
            available_observations=2669,
            hypothesis_id="H-1",
            consumption=[exhausted],
        )


def test_windows_are_required_rather_than_derived_from_the_dataset_names() -> None:
    """A dataset without a range says nothing about whether the sample can resolve anything.

    The candidate names `required_datasets` and no spans. Inventing a span here would put a
    fabricated sample size in front of the power gate, which is the one input it exists to check.
    """
    with pytest.raises(CandidateNotRegistrable, match="no window spans"):
        draft_from_candidate(
            candidate(),
            effect=effect(),
            windows=[],
            available_observations=2669,
            hypothesis_id="H-1",
        )


def test_registering_against_a_subset_of_the_required_data_is_refused() -> None:
    """It would freeze a different question from the one the candidate asked."""
    with pytest.raises(CandidateNotRegistrable, match="no window covers it"):
        draft_from_candidate(
            candidate(required_datasets=(DATASET, "fred-macro-monthly")),
            effect=effect(),
            windows=[window()],
            available_observations=2669,
            hypothesis_id="H-1",
        )


def test_the_effect_size_is_never_derived_from_the_candidate() -> None:
    """The conversion is deliberately not total.

    A candidate says what to test; an effect specification says what would count as worth acting
    on. Discovery can claim a direction and cannot claim a magnitude, and deriving one from its
    prose would put a fabricated number into the input the power gate exists to check.
    """
    import inspect

    signature = inspect.signature(draft_from_candidate)
    assert signature.parameters["effect"].default is inspect.Parameter.empty
    assert signature.parameters["windows"].default is inspect.Parameter.empty
    assert signature.parameters["available_observations"].default is inspect.Parameter.empty

    # And the direction survives as the prediction, so the candidate's own claim is not lost.
    draft = draft_from_candidate(
        candidate(),
        effect=effect(),
        windows=[window()],
        available_observations=2669,
        hypothesis_id="H-1",
    )
    assert draft.prediction == "higher dispersion precedes larger drawdowns"
