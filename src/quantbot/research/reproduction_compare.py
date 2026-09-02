"""What it means for a rerun to have reproduced an experiment (#48).

`reproduce.py` lists five capabilities that do not exist yet, and this is the fourth: comparing a
rerun's manifest against the original. It is built **before** the execution capability on
purpose.

The comparison rule decides what counts as reproduction. Writing it after seeing a rerun's output
would be choosing the tolerance that makes the result agree -- the same failure #57 avoids by
freezing an execution policy before computing any counterfactual, and the same one #5 avoids by
registering a hypothesis before measuring it. A reproducibility check whose threshold was picked
once the answer was visible is not a check.

**Two classes of field, and the split is the substance.**

*Deciding* fields determine what the experiment concluded: its results, the data it ran on, the
code that ran, the registration it was frozen against, its costs, its statistical plan. These
must match **exactly**. No numeric tolerance -- see below.

*Incidental* fields describe the run rather than the finding: when it happened, how long it took,
how much memory it used, which host. These are reported as drift and never fail a comparison,
because a rerun that took four seconds longer has still reproduced the result.

**There is deliberately no numeric tolerance.** A backtest over frozen inputs through a named
execution path is deterministic; if it were not, the manifest's own claim to describe a
reproducible run would be false. So a result that differs in the last decimal has not "nearly
reproduced" -- something changed, and a tolerance would hide which. If a genuine source of
non-determinism is ever found, the honest response is to record it as an unreproducible component
(as #123 does for model calls), not to widen a band until it fits.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from quantbot.research.manifest import ExperimentManifest


class FieldClass(StrEnum):
    """Whether a field decides what the experiment found, or merely describes the run."""

    DECIDING = "DECIDING"
    INCIDENTAL = "INCIDENTAL"


#: Fields that determine what the experiment concluded. A difference in any of these means the
#: rerun did not reproduce the original, whatever else agreed.
#:
#: `model_calls` is here rather than among the incidentals, and it is worth saying why: a rerun
#: that consumed model completions when the original did not has not reproduced it, it has done
#: something else that happened to arrive nearby. #122 measured why -- a model's answer depends on
#: its preceding call history, which nothing records.
DECIDING_FIELDS: frozenset[str] = frozenset(
    {
        "experiment_id",
        "git_commit",
        "data_hash",
        "configuration_hash",
        "costs",
        "periods",
        "walk_forward_splits",
        "holdout",
        "neighborhood_grid",
        "ablations",
        "results",
        "mode",
        "hypothesis_id",
        "hypothesis_version",
        "registration_hash",
        "hypothesis_graph_hash",
        "minimum_detectable_effect",
        "power_verdict",
        "statistics",
        "datasets",
        "model_calls",
        # Surfaced by `test_every_field_is_classified` rather than chosen up front, which is what
        # that test is for. All three decide:
        #
        # `features` is what was computed from the data; a different feature set is a different
        # experiment. `models` names what produced any model-derived input, and #122 established
        # that a model's answer is not a function of its recorded inputs. `workers` records which
        # external engine ran, and a result from a different worker version is a different result
        # however similar the number.
        "features",
        "models",
        "workers",
    }
)

#: Fields that describe the run rather than the finding. Reported, never fatal.
INCIDENTAL_FIELDS: frozenset[str] = frozenset(
    {
        "resources",
        "environment",
        "code",
        "reproduction_command",
        "reproduction_inputs",
    }
)


class Difference(BaseModel):
    """One field that did not match, rendered so a reader can see what moved."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str
    field_class: FieldClass
    original: str
    rerun: str


class ReproductionComparison(BaseModel):
    """Whether a rerun reproduced an original, and everything that differed either way."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str
    #: Deciding fields that disagree. Non-empty means the rerun did not reproduce the original.
    disagreements: tuple[Difference, ...] = ()
    #: Incidental fields that differ. Expected, and recorded so a reader can see the run varied
    #: in ways that do not bear on the finding.
    drift: tuple[Difference, ...] = ()

    @property
    def reproduced(self) -> bool:
        """Exact agreement on everything that decides what the experiment found."""
        return not self.disagreements

    def explain(self) -> str:
        headline = "REPRODUCED" if self.reproduced else "NOT_REPRODUCED"
        parts = [f"{self.experiment_id}: {headline}"]
        parts.extend(
            f"{item.field}: {item.original} -> {item.rerun}" for item in self.disagreements
        )
        if self.drift:
            parts.append(f"{len(self.drift)} incidental difference(s)")
        return "; ".join(parts)


def _render(value: Any) -> str:
    """A stable string for comparison and display.

    Sorted where the value is a mapping, so a reordered dict is not reported as a change. The
    ordering of keys is not a property of the experiment, and reporting it as one would fill a
    comparison with differences nobody can act on -- which is how a check gets ignored.
    """
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{key}={_render(value[key])}" for key in sorted(value)) + "}"
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_render(item) for item in value) + "]"
    return str(value)


def classify(field: str) -> FieldClass:
    """Whether a field decides what the experiment found.

    Anything unclassified is **deciding**. That is the safe direction: a field added later that
    nobody classified is more likely to describe the finding than the weather, and the cost of
    being wrong is a comparison that refuses rather than one that wrongly agrees.

    Split out so the default is testable. `test_every_field_is_classified` keeps the sets
    covering the model, which means the default branch is currently unreachable through
    `compare_manifests` -- and an unreachable branch asserted only indirectly is one nobody can
    show still works.
    """
    return FieldClass.INCIDENTAL if field in INCIDENTAL_FIELDS else FieldClass.DECIDING


def compare_manifests(
    original: ExperimentManifest, rerun: ExperimentManifest
) -> ReproductionComparison:
    """Compare a rerun against the manifest it claims to reproduce.

    Classification is `classify`, which treats anything unknown as deciding.
    """
    disagreements: list[Difference] = []
    drift: list[Difference] = []

    # Both arguments are the same validated model, so a field cannot be present on one and
    # absent from the other. An earlier version carried an `unmatched` list for that case;
    # mutation testing showed it could never fire, and an unreachable guard is what the next
    # reader trusts. Comparing raw payloads would make the case real, and would be a different
    # design with its own argument.
    for name in sorted(type(original).model_fields):
        left = _render(getattr(original, name))
        right = _render(getattr(rerun, name))
        if left == right:
            continue

        classification = classify(name)
        difference = Difference(
            field=name, field_class=classification, original=left, rerun=right
        )
        if classification is FieldClass.DECIDING:
            disagreements.append(difference)
        else:
            drift.append(difference)

    return ReproductionComparison(
        experiment_id=original.experiment_id,
        disagreements=tuple(disagreements),
        drift=tuple(drift),
    )


__all__ = [
    "DECIDING_FIELDS",
    "INCIDENTAL_FIELDS",
    "Difference",
    "classify",
    "FieldClass",
    "ReproductionComparison",
    "compare_manifests",
]
