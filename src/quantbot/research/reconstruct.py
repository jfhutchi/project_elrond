"""Rebuild an experiment's plan from the registration a manifest names (#48).

The second of #48's five capabilities: *reconstruct the compiled experiment plan from the
registration*. Every piece already existed and nothing joined them.

`HypothesisRegistry.get` returns a `Registration` from `document_json`, which holds the whole
canonical registration rather than the columns promoted for querying. `compile_experiment` turns
one into an `ExperimentPlan` and is entirely mechanical -- no model, no measurement, so a
reconstruction is deterministic in a way the scouting and critique stages are not. It had no
caller in `src/`.

**The load-bearing check is the registration hash, not the id.**

A manifest names `hypothesis_id`, `hypothesis_version` and `registration_hash`. The first two
locate a row; only the third says it is the same registration. Registrations are append-only and
a new version is a new row, so id-and-version *should* be sufficient -- but "should" is the word
that makes a reproduction check worthless. If the stored registration's content hash differs from
the one the manifest recorded, the manifest describes something this store no longer holds, and
recompiling would produce a plan for a different experiment while looking entirely successful.

That is the specific way a reproducibility check gets silently inverted: it would report a plan,
the plan would be internally consistent, and it would answer a question nobody asked.

**Refusal is the answer, not an error to route around.** `compile_experiment` already refuses an
uneconomic registration or a missing dataset, and those refusals are carried through rather than
caught. A registration that cannot be compiled today could not have been run then either, and
reporting that is more useful than a plan assembled by relaxing the thing that refused.

This reconstructs a plan. It does not execute one -- that is capability 3, and it is the hard one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.orm import Session

from quantbot.research.builder import CompilationRefused, ExperimentPlan, compile_experiment
from quantbot.research.manifest import DatasetSnapshot, ExperimentManifest
from quantbot.research.registry import HypothesisRegistry, Registration


class ReconstructionOutcome(StrEnum):
    """Why a plan could or could not be rebuilt."""

    #: The registration was found, its hash matched, and the plan compiled.
    RECONSTRUCTED = "RECONSTRUCTED"
    #: No registration under that id and version. A store that never held it, or a different one.
    REGISTRATION_ABSENT = "REGISTRATION_ABSENT"
    #: A registration is there and it is not the one the manifest was frozen against.
    REGISTRATION_CHANGED = "REGISTRATION_CHANGED"
    #: The manifest claims no registration at all, so there is nothing to rebuild from.
    #: Exploratory manifests are legitimately in this state and are not reproducible in this
    #: sense -- which is a fact about the experiment, not a failure of this function.
    NOT_REGISTERED = "NOT_REGISTERED"
    #: The registration exists and matches, and compilation refused it. Carried through.
    COMPILATION_REFUSED = "COMPILATION_REFUSED"


@dataclass(frozen=True, slots=True)
class Reconstruction:
    """A rebuilt plan, or the reason there is not one."""

    experiment_id: str
    outcome: ReconstructionOutcome
    plan: ExperimentPlan | None = None
    registration: Registration | None = None
    detail: str = ""

    @property
    def reconstructed(self) -> bool:
        return self.outcome is ReconstructionOutcome.RECONSTRUCTED

    def explain(self) -> str:
        if self.reconstructed:
            return f"{self.experiment_id}: RECONSTRUCTED"
        return f"{self.experiment_id}: {self.outcome.value} -- {self.detail}"


def reconstruct_plan(
    session: Session,
    manifest: ExperimentManifest,
    *,
    datasets: Sequence[DatasetSnapshot] | None = None,
) -> Reconstruction:
    """Rebuild the plan a manifest's registration compiles to, or say why it cannot be.

    `datasets` defaults to the snapshots the manifest itself recorded. Passing them explicitly is
    for the caller who has *resolved* those snapshots against a store -- `dataset_snapshots`
    does that for bars -- so the plan is rebuilt from data something confirmed still exists,
    rather than from the manifest's own description of it.

    That distinction matters more than it looks. Compiling from the manifest's copy proves the
    registration and the manifest agree. Compiling from resolved snapshots proves the data is
    still there too, and only the second is a step toward re-running anything.
    """
    # All three checked together because the manifest validates them as a unit: a confirmatory
    # experiment must record every one, an exploratory experiment may claim none. Checking only
    # the id would leave the hash optional to the type checker and, more to the point, would let
    # a manifest naming a registration it cannot identify reach the comparison below.
    if (
        manifest.hypothesis_id is None
        or manifest.hypothesis_version is None
        or manifest.registration_hash is None
    ):
        return Reconstruction(
            experiment_id=manifest.experiment_id,
            outcome=ReconstructionOutcome.NOT_REGISTERED,
            detail=(
                "the manifest names no registration; an exploratory run has no frozen plan to "
                "rebuild, which is a fact about the experiment rather than a missing capability"
            ),
        )

    registration = HypothesisRegistry(session).get(
        manifest.hypothesis_id, manifest.hypothesis_version
    )
    if registration is None:
        return Reconstruction(
            experiment_id=manifest.experiment_id,
            outcome=ReconstructionOutcome.REGISTRATION_ABSENT,
            detail=(
                f"no registration {manifest.hypothesis_id} v{manifest.hypothesis_version} in "
                "this store, so the plan cannot be rebuilt from it"
            ),
        )

    # The check the whole function turns on. Id and version locate a row; only the hash says it
    # is the same registration the manifest was frozen against.
    if registration.content_hash != manifest.registration_hash:
        return Reconstruction(
            experiment_id=manifest.experiment_id,
            outcome=ReconstructionOutcome.REGISTRATION_CHANGED,
            registration=registration,
            detail=(
                f"the manifest was frozen against {manifest.registration_hash[:12]} and this "
                f"store holds {registration.content_hash[:12]} under the same id and version; "
                "recompiling would produce a plan for a different experiment"
            ),
        )

    try:
        plan = compile_experiment(
            registration, datasets=tuple(datasets if datasets is not None else manifest.datasets)
        )
    except CompilationRefused as refusal:
        return Reconstruction(
            experiment_id=manifest.experiment_id,
            outcome=ReconstructionOutcome.COMPILATION_REFUSED,
            registration=registration,
            detail=str(refusal),
        )

    return Reconstruction(
        experiment_id=manifest.experiment_id,
        outcome=ReconstructionOutcome.RECONSTRUCTED,
        plan=plan,
        registration=registration,
    )


__all__ = [
    "Reconstruction",
    "ReconstructionOutcome",
    "reconstruct_plan",
]
