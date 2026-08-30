"""What counts as having reproduced an experiment (#48).

Built before the execution capability on purpose. The comparison rule decides what counts as
reproduction, and writing it after seeing a rerun's output would be choosing the tolerance that
makes the result agree -- the same failure #57 avoids by freezing an execution policy before
computing a counterfactual.

The tests are mostly about the split between fields that decide what the experiment found and
fields that merely describe the run, because getting that wrong in either direction breaks the
check: too strict and every rerun fails on a timestamp, too loose and a changed result passes.
"""

from __future__ import annotations

from decimal import Decimal

from quantbot.research.manifest import ResourceUsage
from quantbot.research.reproduction_compare import (
    DECIDING_FIELDS,
    INCIDENTAL_FIELDS,
    FieldClass,
    compare_manifests,
)
from tests.unit.research.test_reproducibility import NOW, manifest


def test_an_identical_rerun_reproduces() -> None:
    """The baseline. Without this passing, nothing else means anything."""
    comparison = compare_manifests(manifest(), manifest())

    assert comparison.reproduced
    assert comparison.disagreements == ()
    assert "REPRODUCED" in comparison.explain()


def test_a_changed_result_is_not_a_reproduction() -> None:
    """The case the whole thing exists for."""
    comparison = compare_manifests(
        manifest(), manifest(results={"FULL_STRATEGY": {"total_return": "0.13"}})
    )

    assert not comparison.reproduced
    assert [item.field for item in comparison.disagreements] == ["results"]
    assert "0.12" in comparison.disagreements[0].original
    assert "0.13" in comparison.disagreements[0].rerun


def test_there_is_no_numeric_tolerance() -> None:
    """A backtest over frozen inputs through a named path is deterministic.

    A result differing in the last decimal has not "nearly reproduced" -- something changed, and
    a tolerance would hide which. If real non-determinism is ever found, the honest response is
    to record the component as unreproducible, as #123 does for model calls, not to widen a band
    until it fits.
    """
    comparison = compare_manifests(
        manifest(), manifest(results={"FULL_STRATEGY": {"total_return": "0.120000001"}})
    )

    assert not comparison.reproduced


def test_timing_and_resources_do_not_fail_a_comparison() -> None:
    """A rerun that took four seconds longer has still reproduced the result.

    Failing on these would make every genuine reproduction look like a failure, and a check that
    cries wolf is one somebody deletes.
    """
    slower = manifest(
        resources=ResourceUsage(
            started_at=NOW,
            finished_at=NOW,
            peak_memory_mb=2048,
            cpu_seconds=Decimal("612.5"),
        )
    )
    comparison = compare_manifests(manifest(), slower)

    assert comparison.reproduced, "resource usage does not decide what was found"
    assert [item.field for item in comparison.drift] == ["resources"]
    assert comparison.drift[0].field_class is FieldClass.INCIDENTAL


def test_the_data_it_ran_on_decides() -> None:
    """Same code, same plan, different data is a different experiment wearing the same id."""
    comparison = compare_manifests(manifest(), manifest(data_hash="a-different-hash"))

    assert not comparison.reproduced
    assert comparison.disagreements[0].field == "data_hash"


def test_the_registration_it_was_frozen_against_decides() -> None:
    """A confirmatory result is a claim about a specific pre-registered hypothesis.

    Reproducing the numbers under a different registration reproduces an arithmetic, not a
    finding.
    """
    comparison = compare_manifests(manifest(), manifest(registration_hash="b" * 64))

    assert not comparison.reproduced
    assert comparison.disagreements[0].field == "registration_hash"


def test_consuming_model_calls_is_a_disagreement() -> None:
    """A rerun that used a model where the original did not has not reproduced it.

    #122 measured why: a model's answer depends on its preceding call history, which nothing
    records -- so the rerun did something else that happened to arrive nearby.
    """
    comparison = compare_manifests(manifest(), manifest(model_calls=("mistral:7b@0",)))

    assert not comparison.reproduced
    assert comparison.disagreements[0].field == "model_calls"
    assert comparison.disagreements[0].field_class is FieldClass.DECIDING


def test_a_reordered_mapping_is_not_a_change() -> None:
    """Key ordering is not a property of the experiment.

    Reporting it as one would fill the comparison with differences nobody can act on, which is
    how a check gets ignored rather than read.
    """
    forward = manifest(costs={"slippage_bps": "1.1", "commission_per_order": "0"})
    reversed_order = manifest(costs={"commission_per_order": "0", "slippage_bps": "1.1"})

    assert compare_manifests(forward, reversed_order).reproduced


def test_every_field_is_classified() -> None:
    """A field belonging to neither set is treated as deciding, and this asserts the sets
    actually cover the model.

    The default is the safe direction -- a field added later that nobody classified is more
    likely to describe the finding than the weather -- but relying on the default silently would
    mean nobody ever noticed a new field needed a decision.
    """
    from quantbot.research.manifest import ExperimentManifest

    classified = DECIDING_FIELDS | INCIDENTAL_FIELDS
    unclassified = set(ExperimentManifest.model_fields) - classified

    assert not unclassified, (
        f"{sorted(unclassified)} are classified by default rather than deliberately; decide "
        "whether each decides what the experiment found"
    )


def test_the_two_classes_do_not_overlap() -> None:
    """A field cannot both decide and not decide."""
    assert not (DECIDING_FIELDS & INCIDENTAL_FIELDS)


def test_the_explanation_names_what_moved() -> None:
    """A verdict an operator cannot act on is a verdict they will not act on."""
    comparison = compare_manifests(
        manifest(), manifest(results={"FULL_STRATEGY": {"total_return": "0.99"}})
    )

    explained = comparison.explain()

    assert "NOT_REPRODUCED" in explained
    assert "results" in explained
    assert "0.99" in explained


def test_an_unclassified_field_is_treated_as_deciding() -> None:
    """The safe direction, asserted directly rather than inferred.

    `test_every_field_is_classified` keeps the sets covering the model, which makes this branch
    unreachable through `compare_manifests` -- so a mutation flipping the default came back green
    with every other test passing. An unreachable branch asserted only indirectly is one nobody
    can show still works, which is why `classify` is a function.

    The cost of being wrong here is asymmetric: treating a new field as incidental would let a
    changed result pass silently, and treating it as deciding merely makes a comparison refuse
    until somebody classifies it.
    """
    from quantbot.research.reproduction_compare import classify

    assert classify("a_field_nobody_has_classified_yet") is FieldClass.DECIDING
    assert classify("results") is FieldClass.DECIDING
    assert classify("resources") is FieldClass.INCIDENTAL
