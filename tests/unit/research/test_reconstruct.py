"""Rebuilding an experiment's plan from the registration a manifest names (#48).

The second of #48's five capabilities. Every piece existed -- `HypothesisRegistry.get` returns a
`Registration` from `document_json`, `compile_experiment` turns one into a plan and is entirely
mechanical -- and nothing joined them.

Built before anything can execute a plan, for the same reason #144's comparison rule was: a
reconstruction written after seeing a rerun would be shaped to make that rerun agree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.manifest import ExperimentManifest
from quantbot.research.reconstruct import ReconstructionOutcome, reconstruct_plan
from quantbot.research.registry import HypothesisRegistry
from quantbot.storage import Database
from tests.unit.research.test_builder import registration, snapshot
from tests.unit.research.test_reproducibility import manifest

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    with Database.create(tmp_path / "reconstruct.db") as store:
        yield store


def stored_registration(database: Database, **overrides: object):  # type: ignore[no-untyped-def]
    """Insert a registration exactly as the registry does, and hand it back."""
    frozen = registration(**overrides)
    with database.transaction() as session:
        HypothesisRegistry(session)._insert(frozen)  # noqa: SLF001
    return frozen


def manifest_for(frozen, **overrides: object) -> ExperimentManifest:  # type: ignore[no-untyped-def]
    """A fully validated confirmatory manifest naming that registration.

    Built through the shared factory rather than `model_construct`, so these run against a shape
    the model would actually accept -- a manifest assembled past its own validators would let
    this pass on an experiment that could never exist.
    """
    fields: dict[str, object] = {
        "hypothesis_id": frozen.draft.hypothesis_id,
        "hypothesis_version": frozen.draft.version,
        "registration_hash": frozen.content_hash,
        "datasets": (snapshot(),),
    }
    fields.update(overrides)
    return manifest(**fields)


def test_a_matching_registration_rebuilds_the_plan(database: Database) -> None:
    """The capability itself. Without this the rest are refusals with nothing to refuse."""
    frozen = stored_registration(database)

    with database.transaction() as session:
        result = reconstruct_plan(session, manifest_for(frozen))

    assert result.reconstructed, result.explain()
    assert result.outcome is ReconstructionOutcome.RECONSTRUCTED
    assert result.plan is not None
    assert result.registration is not None
    assert result.registration.content_hash == frozen.content_hash


def test_a_changed_registration_under_the_same_id_is_refused(database: Database) -> None:
    """The check the whole function turns on, and the reason it is the hash and not the id.

    Id and version locate a row; only the hash says it is the same registration the manifest was
    frozen against. If this compared ids alone, the store could hold a different registration
    under the same name and the reconstruction would produce a plan for a different experiment
    while looking entirely successful -- a reproducibility check silently inverted.
    """
    stored_registration(database)
    manifest = manifest_for(
        registration(), registration_hash="f" * 64
    )

    with database.transaction() as session:
        result = reconstruct_plan(session, manifest)

    assert not result.reconstructed
    assert result.outcome is ReconstructionOutcome.REGISTRATION_CHANGED
    assert result.plan is None, "no plan is produced from a registration that does not match"
    assert "different experiment" in result.detail


def test_a_missing_registration_is_named_rather_than_guessed(database: Database) -> None:
    """A store that never held it is a different fact from one holding a changed copy."""
    frozen = registration()

    with database.transaction() as session:
        result = reconstruct_plan(session, manifest_for(frozen))

    assert result.outcome is ReconstructionOutcome.REGISTRATION_ABSENT
    assert result.plan is None
    assert frozen.draft.hypothesis_id in result.detail


def test_an_exploratory_manifest_has_no_plan_to_rebuild(database: Database) -> None:
    """Not a failure of this function.

    An exploratory run was never frozen against a registration, so there is nothing to
    reconstruct. Reporting that as a missing capability would put a permanent false negative in
    front of every exploratory manifest.
    """
    exploratory = manifest(
        mode="EXPLORATORY",
        hypothesis_id=None,
        hypothesis_version=None,
        registration_hash=None,
        minimum_detectable_effect=None,
        power_verdict=None,
    )

    with database.transaction() as session:
        result = reconstruct_plan(session, exploratory)

    assert result.outcome is ReconstructionOutcome.NOT_REGISTERED
    assert "exploratory" in result.detail


def test_a_compilation_refusal_is_carried_through(database: Database) -> None:
    """A registration that cannot be compiled today could not have been run then either.

    `compile_experiment` refuses a registration whose declared windows are not supplied. Catching
    that and assembling a plan anyway -- by relaxing whatever refused -- would be the failure this
    whole issue exists to prevent, so the refusal is reported as the answer.
    """
    frozen = stored_registration(database)
    # A dataset the registration never declared. Supplying it is `MISSING_DATASET` from the
    # compiler's point of view, which is what a store holding the wrong vintage would look like.
    wrong = (snapshot(dataset="some-other-dataset"),)

    with database.transaction() as session:
        result = reconstruct_plan(session, manifest_for(frozen), datasets=wrong)

    assert not result.reconstructed
    assert result.outcome is ReconstructionOutcome.COMPILATION_REFUSED
    assert result.plan is None
    assert result.registration is not None, "the registration was found; compilation refused it"
    assert "MISSING_DATASET" in result.detail or "does not supply" in result.detail


def test_resolved_datasets_are_used_when_supplied(database: Database) -> None:
    """Compiling from resolved snapshots is a stronger statement than from the manifest's copy.

    The manifest's copy proves the registration and the manifest agree. Snapshots resolved
    against a store -- which `dataset_snapshots` does for bars -- prove the data is still there,
    and only that is a step toward re-running anything.
    """
    frozen = stored_registration(database)
    # The manifest carries a dataset the registration does not declare, so compiling from *its*
    # copy would refuse. Passing correctly resolved snapshots succeeds -- which is only possible
    # if the override is actually used, rather than the manifest's own list.
    stale = manifest_for(frozen, datasets=(snapshot(dataset="some-other-dataset"),))

    with database.transaction() as session:
        from_manifest = reconstruct_plan(session, stale)
        from_resolved = reconstruct_plan(session, stale, datasets=(snapshot(),))

    assert from_manifest.outcome is ReconstructionOutcome.COMPILATION_REFUSED
    assert from_resolved.reconstructed, from_resolved.explain()
    assert from_resolved.plan is not None


def test_the_explanation_names_the_outcome(database: Database) -> None:
    """A verdict an operator cannot act on is one they will not act on."""
    frozen = registration()

    with database.transaction() as session:
        result = reconstruct_plan(session, manifest_for(frozen))

    assert "REGISTRATION_ABSENT" in result.explain()
    assert "experiment-1" in result.explain()
