"""What a result bundle has to prove about itself, and what it has to refuse to hide.

The invariants here are not hypothetical. Each one encodes a defect that already happened in
this project and was caught by the number being implausible rather than by the code looking
wrong; `REFUTED.md` records that asymmetry as the single most useful thing to know when reading
any figure in the repository.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExecutionPath,
    ExperimentManifest,
    ExperimentPeriod,
    ResourceUsage,
    StatisticalPlan,
    StatisticalTest,
    WorkerProvenance,
    redact,
)
from quantbot.research.reproducibility import (
    capture_environment,
    capture_git_state,
    check_invariants,
    compare,
)

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)
DIGEST = "a" * 64


def plan(**overrides: object) -> StatisticalPlan:
    fields: dict[str, object] = {
        "test": StatisticalTest.PAIRED_T,
        "comparison": "PAIRED",
        "primary_estimand": "annualised Sharpe difference against SPY",
        "cumulative_trials": 69,
        "luck_threshold_z": Decimal("2.910019"),
        "minimum_detectable_effect": Decimal("0.920229"),
        "variance_inflation": Decimal("1"),
        "multiple_testing_correction": "expected maximum over cumulative trials",
    }
    fields.update(overrides)
    return StatisticalPlan(**fields)


def manifest(**overrides: object) -> ExperimentManifest:
    fields: dict[str, object] = {
        "experiment_id": "experiment-1",
        "git_commit": "abc123",
        "data_hash": "data-hash",
        "configuration_hash": "config-hash",
        "costs": {"slippage_bps": "1.1", "commission_per_order": "0"},
        "periods": (
            ExperimentPeriod(name="train", start=date(2016, 1, 4), end=date(2022, 12, 30)),
        ),
        "walk_forward_splits": (
            ExperimentPeriod(name="walk-1", start=date(2016, 1, 4), end=date(2018, 12, 31)),
        ),
        "holdout": ExperimentPeriod(name="holdout", start=date(2023, 1, 3), end=date(2026, 8, 18)),
        "neighborhood_grid": {"trend_period": (190, 200, 210)},
        "ablations": ("without_market_regime",),
        "results": {"FULL_STRATEGY": {"total_return": "0.12"}},
        "mode": "CONFIRMATORY",
        "hypothesis_id": "H-2026-001",
        "hypothesis_version": 1,
        "registration_hash": DIGEST,
        "minimum_detectable_effect": "0.920229",
        "power_verdict": "POWERED",
        "code": CodeProvenance(
            git_commit="abc123",
            dirty=False,
            configuration_hash="config-hash",
            execution_path=ExecutionPath.PRODUCTION_ENGINE,
        ),
        "environment": EnvironmentFingerprint(
            python_version="3.11.15",
            platform="Windows-AMD64",
            dependency_lock_hash="lock-hash",
            host_class="operator-workstation",
            random_seed=7,
        ),
        "datasets": (
            DatasetSnapshot(
                dataset="sip-us-equities-daily",
                snapshot="2026-08-18",
                role="PROTECTED_EVALUATION",
                start=date(2016, 1, 4),
                end=date(2026, 8, 18),
                content_hash="bars-hash",
                observations=2669,
            ),
        ),
        "statistics": plan(),
        "resources": ResourceUsage(
            started_at=NOW,
            finished_at=NOW,
            peak_memory_mb=512,
            cpu_seconds=Decimal("41.5"),
        ),
    }
    fields.update(overrides)
    return ExperimentManifest(**fields)


def exploratory(**overrides: object) -> ExperimentManifest:
    """A bundle making no evidential claim, so it needs no registration."""
    fields: dict[str, object] = {
        "mode": "EXPLORATORY",
        "hypothesis_id": None,
        "hypothesis_version": None,
        "registration_hash": None,
        "minimum_detectable_effect": None,
        "power_verdict": None,
    }
    fields.update(overrides)
    return manifest(**fields)


def test_a_confirmatory_bundle_must_say_what_produced_it() -> None:
    """A number cited as evidence has to name its code, environment, data and test."""
    for missing in ("code", "environment", "statistics", "resources"):
        with pytest.raises(ValueError, match="code, environment, statistical"):
            manifest(**{missing: None})
    with pytest.raises(ValueError, match="dataset snapshots"):
        manifest(datasets=())


def test_a_wrong_statistical_test_for_the_dependence_structure_is_flagged() -> None:
    """Cycle 11's actual error, made mechanical.

    A one-sample test on paired data put the overnight premium at t=2.74; paired, it is 0.63.
    The metric alone reproduces that error perfectly, so the test is what has to be recorded.
    """
    report = check_invariants(manifest(statistics=plan(test=StatisticalTest.ONE_SAMPLE_T)))
    assert not report.ok
    assert [violation.invariant for violation in report.violations] == [
        "statistical-test-matches-dependence"
    ]
    assert "SINGLE_SAMPLE" in report.violations[0].detail

    # And the same cycle's other one: two Sharpes of the same asset compared as independent
    # samples, where Jobson-Korkie-Memmel gives z=1.01.
    welch = check_invariants(manifest(statistics=plan(test=StatisticalTest.WELCH_T)))
    assert not welch.ok
    assert check_invariants(
        manifest(statistics=plan(test=StatisticalTest.JOBSON_KORKIE_MEMMEL))
    ).ok


def test_costs_that_improve_a_return_are_flagged() -> None:
    report = check_invariants(
        manifest(results={"LEVERED": {"gross_return": "0.10", "net_return": "0.12"}})
    )
    assert [violation.invariant for violation in report.violations] == [
        "costs-never-improve-returns"
    ]
    assert "costs-never-improve-returns" in report.checked


def test_forced_liquidation_creating_wealth_is_flagged() -> None:
    """Cycle 12 produced exactly this by comparing rebalance frequencies it had not matched."""
    report = check_invariants(
        manifest(
            results={
                "MARGIN": {
                    "terminal_wealth": "954.00",
                    "terminal_wealth_with_forced_liquidation": "2475.00",
                }
            }
        )
    )
    assert [violation.invariant for violation in report.violations] == [
        "forced-liquidation-never-creates-wealth"
    ]


def test_a_significance_claim_below_its_own_frozen_bar_is_flagged() -> None:
    report = check_invariants(
        manifest(results={"OVERNIGHT": {"test_statistic": "2.74", "significant": "true"}})
    )
    assert [violation.invariant for violation in report.violations] == [
        "significance-clears-the-frozen-bar"
    ]
    assert "2.910019" in report.violations[0].detail


def test_an_invariant_that_could_not_run_is_reported_as_skipped_not_passed() -> None:
    """A check that silently does nothing looks exactly like a check that passed."""
    report = check_invariants(manifest())
    assert report.ok
    assert "costs-never-improve-returns" in report.skipped
    assert "forced-liquidation-never-creates-wealth" in report.skipped
    assert "significance-clears-the-frozen-bar" in report.skipped
    # The one it could run on this bundle, it did.
    assert "statistical-test-matches-dependence" in report.checked
    assert set(report.checked).isdisjoint(report.skipped)


def test_identical_results_from_different_inputs_are_reported_as_a_difference() -> None:
    """The harness-not-connected signature.

    Three of this project's recorded analysis errors were a runner that ignored the config
    under test, and the tell was byte-identical output across runs that should have differed.
    """
    before = manifest()
    after = manifest(configuration_hash="a-genuinely-different-config")
    reasons = [str(difference) for difference in compare(before, after)]

    assert any("configuration_hash" in reason for reason in reasons)
    assert any("may not exercise what changed" in reason for reason in reasons)


def test_a_changed_dataset_vintage_is_named_even_when_the_range_is_identical() -> None:
    """Silent data revision is what makes an old conclusion unfalsifiable."""
    revised = manifest(
        datasets=(
            DatasetSnapshot(
                dataset="sip-us-equities-daily",
                snapshot="2026-09-01",
                role="PROTECTED_EVALUATION",
                start=date(2016, 1, 4),
                end=date(2026, 8, 18),
                content_hash="different-bars-hash",
                observations=2669,
            ),
        )
    )
    reasons = [str(difference) for difference in compare(manifest(), revised)]
    assert any("2026-08-18 != 2026-09-01" in reason for reason in reasons)


def test_two_runs_of_the_same_experiment_compare_equal() -> None:
    assert compare(manifest(), manifest()) == []


def test_a_manifest_refuses_to_carry_a_credential() -> None:
    """Redaction that is merely intended is redaction that eventually fails silently."""
    with pytest.raises(ValueError, match="must not carry credentials"):
        manifest(features={"alpaca_key": "PKTEST1234567890ABCDEF"})
    with pytest.raises(ValueError, match="must not carry credentials"):
        manifest(results={"FULL_STRATEGY": {"note": "Bearer abcdefghijklmnopqrstuv"}})

    assert redact({"alpaca_key": "PKTEST1234567890ABCDEF", "feed": "sip"}) == {
        "alpaca_key": "[REDACTED]",
        "feed": "sip",
    }


def test_the_inputs_hash_ignores_the_result_but_not_what_produced_it() -> None:
    baseline = manifest()
    assert baseline.inputs_hash == manifest(results={"OTHER": {"total_return": "9.9"}}).inputs_hash
    assert baseline.inputs_hash != manifest(configuration_hash="changed").inputs_hash
    assert baseline.inputs_hash != manifest(statistics=plan(cumulative_trials=500)).inputs_hash


def test_a_worker_that_will_not_disclose_its_search_is_exploratory_only() -> None:
    assert WorkerProvenance(name="rd-agent", version="0.4").exploratory_only
    disclosed = WorkerProvenance(name="rd-agent", version="0.4", search_cardinality=40)
    assert not disclosed.exploratory_only


def test_the_environment_fingerprint_and_git_state_come_from_the_real_machine() -> None:
    fingerprint = capture_environment(host_class="test", lock_file=Path("uv.lock"))
    assert fingerprint.python_version.startswith("3.")
    assert fingerprint.dependency_lock_hash != "no-lock-file"
    assert len(fingerprint.dependency_lock_hash) == 64

    commit, dirty = capture_git_state(Path.cwd())
    assert len(commit) == 40 or commit == "unknown"
    assert isinstance(dirty, bool)


def test_an_exploratory_bundle_still_writes_immutably(tmp_path: Path) -> None:
    from quantbot.research.manifest import write_experiment_manifest

    bundle = exploratory()
    destination = tmp_path / "bundle.json"
    assert write_experiment_manifest(destination, bundle) is True
    assert write_experiment_manifest(destination, bundle) is False
    conflicting = bundle.model_copy(update={"results": {"FULL_STRATEGY": {"x": "1"}}})
    with pytest.raises(FileExistsError, match="different data"):
        write_experiment_manifest(destination, conflicting)


def test_an_experiment_is_exploratory_until_it_names_a_registration() -> None:
    """The default is the weaker claim, so a forgotten field cannot pass as evidence (#5)."""
    assert exploratory().mode == "EXPLORATORY"


def test_a_confirmatory_experiment_must_name_the_registration_it_was_frozen_against() -> None:
    for missing in (
        "hypothesis_id",
        "hypothesis_version",
        "registration_hash",
        "minimum_detectable_effect",
        "power_verdict",
    ):
        with pytest.raises(ValueError, match="registration hash"):
            manifest(**{missing: None})

    with pytest.raises(ValueError, match="SHA-256"):
        manifest(registration_hash="not-a-digest")

    # A null result reads "no effect larger than X" only because X travels with it (#19).
    assert manifest().minimum_detectable_effect == "0.920229"


def test_an_exploratory_experiment_cannot_borrow_a_registration() -> None:
    with pytest.raises(ValueError, match="cannot claim a registration"):
        exploratory(registration_hash=DIGEST)
    with pytest.raises(ValueError, match="cannot claim a registration"):
        exploratory(power_verdict="OVERRIDDEN")
