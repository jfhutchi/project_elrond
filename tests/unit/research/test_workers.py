"""What an external miner must disclose before its results can mean anything."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.research.registry import DataRole
from quantbot.research.workers import (
    UnsupportedSemantics,
    WorkerCapability,
    WorkerInput,
    WorkerResult,
    WorkerSpec,
    WorkerTrust,
    empirical_luck_threshold,
    import_result,
    null_calibration_summary,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


def spec(**overrides: object) -> WorkerSpec:
    fields: dict[str, object] = {
        "name": "rd-agent",
        "version": "0.4.1",
        "capabilities": frozenset(
            {WorkerCapability.FACTOR_MINING, WorkerCapability.NULL_CALIBRATION}
        ),
        "configuration_hash": "c" * 64,
        "discloses_search_cardinality": True,
    }
    fields.update(overrides)
    return WorkerSpec(**fields)


def request(**overrides: object) -> WorkerInput:
    fields: dict[str, object] = {
        "mandate": "mine cross-sectional factors on the discovery split",
        "datasets": ("sip-us-equities-daily",),
        "data_roles": frozenset({DataRole.DISCOVERY}),
        "trial_budget": 1000,
    }
    fields.update(overrides)
    return WorkerInput(**fields)


def result(**overrides: object) -> WorkerResult:
    fields: dict[str, object] = {
        "worker": spec(),
        "request": request(),
        "started_at": NOW,
        "finished_at": NOW + timedelta(minutes=40),
        "search_cardinality": 900,
        "metrics": {"sharpe": "1.31", "test_statistic": "2.4"},
        "artifacts": {"factors.parquet": "d" * 64},
    }
    fields.update(overrides)
    return WorkerResult(**fields)


def test_a_worker_that_will_not_disclose_its_search_cannot_produce_evidence() -> None:
    """The best of a thousand skill-free candidates looks excellent."""
    secretive = spec(discloses_search_cardinality=False)
    assert secretive.trust is WorkerTrust.EXPLORATORY_ONLY

    opaque = result(worker=secretive, search_cardinality=None)
    assert opaque.trust is WorkerTrust.EXPLORATORY_ONLY
    assert not opaque.citable_as_confirmatory

    # And a disclosing worker that happens not to report a number this run is equally not
    # confirmatory: the property is the disclosure, not the intent.
    assert result(search_cardinality=None).trust is WorkerTrust.EXPLORATORY_ONLY
    assert result().citable_as_confirmatory


def test_an_undisclosed_search_is_charged_the_whole_budget_it_was_given() -> None:
    """A worker that will not say what it spent must not be cheaper than one that will."""
    assert result(search_cardinality=None).trials_spent == 1000
    assert result(search_cardinality=900).trials_spent == 900


def test_a_search_cannot_exceed_the_budget_it_was_admitted_under() -> None:
    with pytest.raises(ValueError, match="against a budget of"):
        result(search_cardinality=1001)


def test_a_worker_is_never_handed_protected_or_forward_data() -> None:
    """A miner over protected data spends it, whatever it reports afterwards."""
    for role in (DataRole.PROTECTED_EVALUATION, DataRole.FORWARD_PAPER):
        with pytest.raises(ValueError, match="may not be handed"):
            request(data_roles=frozenset({DataRole.DISCOVERY, role}))
    with pytest.raises(ValueError, match="name the data roles"):
        request(data_roles=frozenset())


def test_unsupported_semantics_are_reported_rather_than_approximated() -> None:
    """Running something adjacent and calling it the same thing is the failure mode."""
    with pytest.raises(UnsupportedSemantics, match="rather than run something adjacent"):
        spec().require(WorkerCapability.MODEL_SEARCH)
    spec().require(WorkerCapability.FACTOR_MINING)

    # A run that could not express part of the registration is exploratory however good the
    # number is, because the number is not answering the registered question.
    partial = result(unsupported=("Jobson-Korkie-Memmel Sharpe comparison",))
    assert partial.trust is WorkerTrust.EXPLORATORY_ONLY
    assert not partial.citable_as_confirmatory


def test_an_imported_result_carries_its_trust_and_its_search_with_the_numbers() -> None:
    imported = import_result(result())
    assert imported["worker"] == "rd-agent@0.4.1"
    assert imported["trust"] == "CONFIRMATORY_ELIGIBLE"
    assert imported["search_cardinality"] == "900"
    assert imported["trials_charged"] == "900"
    assert imported["data_roles"] == "DISCOVERY"
    assert imported["metric.sharpe"] == "1.31"
    assert imported["artifact.factors.parquet"] == "d" * 64

    opaque = import_result(
        result(worker=spec(discloses_search_cardinality=False), search_cardinality=None)
    )
    assert opaque["trust"] == "EXPLORATORY_ONLY"
    assert opaque["search_cardinality"] == "undisclosed"
    assert opaque["trials_charged"] == "1000"


def test_a_null_calibration_gives_a_bar_the_procedure_earned() -> None:
    """The most useful thing a miner can do is show what it finds when there is nothing there.

    Best-of-N on pure noise: the maximum of many draws is large, and that is the number a
    result from the same procedure has to beat.
    """
    generator = random.Random(17)
    # 200 runs of a search that picks the best of 50 skill-free candidates.
    null_statistics = [max(generator.gauss(0.0, 1.0) for _ in range(50)) for _ in range(200)]

    bar = empirical_luck_threshold(null_statistics)
    # sqrt(2 ln 50) = 2.80 is the asymptotic expectation; the empirical 95th percentile of the
    # maximum sits above it, which is the point of measuring rather than assuming.
    assert bar > Decimal("2.5")

    summary = null_calibration_summary(null_statistics)
    assert summary["draws"] == Decimal("200")
    assert summary["median"] < summary["p95"] < summary["p99"] <= summary["max"]

    # A single well-behaved statistic does not clear a bar built from best-of-50 noise.
    assert Decimal("2.4") < bar


def test_a_null_too_small_to_describe_its_own_tail_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 20 draws"):
        empirical_luck_threshold([0.5] * 19)
    with pytest.raises(ValueError, match="upper tail"):
        empirical_luck_threshold([0.5] * 40, quantile=0.4)


def test_a_run_cannot_finish_before_it_starts() -> None:
    with pytest.raises(ValueError, match="cannot finish before it starts"):
        result(finished_at=NOW - timedelta(seconds=1))
