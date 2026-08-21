"""Run a compiled experiment, and make forgetting to spend the holdout impossible.

`compile_experiment` produced an `ExperimentPlan` that nothing executed, and `ExperimentManifest`
was a type nothing in `src/` constructed. So every gate upstream of this point validated a claim
that no caller ever made (#8, #18).

The design question this module answers is #32. `HypothesisRegistry.consume()` marks a protected
window spent, and a runner that reads the window without calling it leaves the reservation to
lapse -- after which the range reads as untouched and a later hypothesis is told the holdout is
clean. Nothing raises. The data was inspected and the record says it was not.

A convention would not fix that, because the failure is an omission and omissions do not announce
themselves. So access is routed through the consumption instead:

* `ProtectedAccess` is the only thing a measurement receives, and it carries the windows it was
  granted over.
* It has no public constructor. `ExperimentRunner` mints one, and only after `consume()` has
  succeeded for every protected window the plan names.
* A measurement that wants protected data has to accept the token, so a runner that skipped
  consumption has nothing to hand it.

Forgetting is therefore a type error rather than a silent contamination.

Consumption happens *before* the measurement runs, never after. An experiment that reads the
holdout and then crashes has still read it, and recording consumption on success would make
failing a way to look at data for free.

The manifest is written the same way, and for the same reason (#18). `run()` used to return the
bundle and leave persisting it to the caller, which is the identical shape of omission: correct,
optional, and therefore absent under time pressure. A runner is now constructed with the
directory its manifests live in, so there is no way to obtain a confirmatory outcome while
quietly dropping the record of how it was reached.
"""

from __future__ import annotations

import time
import tracemalloc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Protocol

from quantbot.research.builder import ExperimentOutcome, ExperimentPlan, OutcomeVerdict
from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExperimentManifest,
    ExperimentPeriod,
    ResourceUsage,
    content_id,
    write_experiment_manifest,
)
from quantbot.research.registry import DataRole, HypothesisRegistry, Registration


class ExperimentRunError(RuntimeError):
    """Raised when a run cannot proceed honestly. Distinct from the experiment failing."""


@dataclass(frozen=True, slots=True)
class ProtectedAccess:
    """Proof that the windows a measurement is about to read have been marked spent.

    Deliberately unconstructible outside this module: `_issued` is a private token that only
    `ExperimentRunner` holds. Without that, a caller could build the evidence of consumption
    without the consumption, which is the defect this type exists to prevent rather than a
    theoretical concern -- it is exactly the shape of every other finding in `REFUTED.md`.
    """

    hypothesis_id: str
    version: int
    #: (dataset, role) pairs actually consumed, in the order they were spent.
    windows: tuple[tuple[str, DataRole], ...]
    consumed_at: datetime
    _issued: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._issued is not _ISSUE_TOKEN:
            raise ExperimentRunError(
                "ProtectedAccess cannot be constructed directly; it is evidence that a window "
                "was spent and manufacturing it would make that evidence worthless"
            )
        if not self.windows:
            raise ExperimentRunError("protected access over no window is not access")


_ISSUE_TOKEN = object()


@dataclass(frozen=True, slots=True)
class Measured:
    """What a measurement reports. Deliberately not an `ExperimentOutcome`.

    The measurement says what it found; the runner decides what that is allowed to be called.
    Letting a measurement return its own verdict would put `SURVIVED` in the gift of the code
    being judged.
    """

    effect: Decimal | None
    test_statistic: Decimal | None = None
    confidence_low: Decimal | None = None
    confidence_high: Decimal | None = None
    #: Probes that actually ran. The runner checks this against the plan rather than trusting it.
    probes_run: tuple[str, ...] = ()
    #: Probes that ran and objected.
    probes_failed: tuple[str, ...] = ()
    diagnostics: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""


class Measurement(Protocol):
    """The experiment itself. Receives proof of consumption, never raw window identifiers."""

    def __call__(self, plan: ExperimentPlan, access: ProtectedAccess) -> Measured: ...


@dataclass(frozen=True, slots=True)
class ExperimentDesign:
    """Everything about the experiment that must be fixed before the result is visible.

    Grouped rather than passed loose because that is the property they share: a walk-forward
    split, a robustness neighbourhood or an ablation chosen after seeing the headline number is
    not a check, it is a search. `ExperimentManifest` requires all of them and this runner will
    not synthesise any -- recording a walk-forward that never ran, or an empty grid, would make
    the bundle assert robustness testing that did not happen.

    These belong on `ExperimentPlan`, which is compiled before the run and hashed. Taking them
    here is the weaker arrangement and a deliberate interim: moving them requires changing the
    compiler's signature, which is a separate change with its own callers to update.
    """

    walk_forward_splits: tuple[ExperimentPeriod, ...]
    #: Parameter sensitivity actually swept, e.g. {"trend_period": (150, 200, 250)}.
    neighborhood_grid: Mapping[str, tuple[int, ...]]
    #: Components switched off to see whether the result survives without them.
    ablations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.walk_forward_splits:
            raise ExperimentRunError(
                "a confirmatory run must declare its walk-forward splits; they are part of the "
                "design and cannot be chosen after the result is visible"
            )
        if not self.neighborhood_grid or any(
            not values for values in self.neighborhood_grid.values()
        ):
            raise ExperimentRunError(
                "a confirmatory run must declare the parameter neighbourhood it swept; an empty "
                "grid records robustness testing that did not happen"
            )
        if len(self.ablations) != len(set(self.ablations)):
            raise ExperimentRunError("ablations must be unique")


@dataclass(frozen=True, slots=True)
class RunResult:
    outcome: ExperimentOutcome
    manifest: ExperimentManifest
    #: Where the bundle was written. Present because it was written, not as an intention.
    manifest_path: Path


class ExperimentRunner:
    """Executes a compiled plan against a registry that owns the protected windows."""

    def __init__(self, registry: HypothesisRegistry, *, manifests: Path) -> None:
        """`manifests` is required, so a runner that does not record cannot be constructed.

        Passing a directory is the whole enforcement. An optional argument would leave the
        default path -- the one people actually take -- as the one that keeps no evidence.
        """
        self._registry = registry
        self._manifests = Path(manifests)

    def run(
        self,
        plan: ExperimentPlan,
        registration: Registration,
        measurement: Measurement,
        *,
        now: datetime,
        code: CodeProvenance,
        environment: EnvironmentFingerprint,
        applied_costs: Mapping[str, str],
        design: ExperimentDesign,
    ) -> RunResult:
        """Spend the holdout, run the measurement, and record what it is allowed to claim.

        `applied_costs` is what the run actually charged, and it is separate from the plan's
        economics on purpose. `compile_experiment` records `round_trip_cost_bps` and
        `annual_rebalances` -- the costs the *hypothesis claimed* it would have to clear --
        while `ExperimentManifest` requires `slippage_bps` and `commission_per_order`, the costs
        the *engine actually applied*. Those are different facts and the two modules had never
        been connected, so nothing had forced them to agree.

        Both go into the manifest under their own keys. A divergence between what a hypothesis
        assumed and what its run charged is exactly the kind of thing that should be visible in
        the record rather than reconciled away here.
        """
        missing_costs = {"slippage_bps", "commission_per_order"} - set(applied_costs)
        if missing_costs:
            raise ExperimentRunError(
                "a run must record the costs it actually applied, not only the ones the "
                f"hypothesis assumed; missing {', '.join(sorted(missing_costs))}"
            )
        if registration.content_hash != plan.registration_hash:
            raise ExperimentRunError(
                f"plan was compiled against registration {plan.registration_hash} but was handed "
                f"{registration.content_hash}; a result frozen against one claim cannot be "
                "reported against another"
            )

        access = self._spend(plan, now=now)

        # Measured, not declared. `ResourceUsage` is provenance, and a fabricated zero would be
        # worse than an approximate real number. `tracemalloc` reports Python-heap allocation
        # only -- it does not see a native library's arena -- and `process_time` excludes time
        # spent blocked on I/O. Both are stated in the field comments rather than left to look
        # more authoritative than they are.
        tracemalloc.start()
        started_at = now
        cpu_before = time.process_time()
        try:
            measured = measurement(plan, access)
        except Exception as error:  # noqa: BLE001 -- a failed run is a result, not an absence
            resources = _usage(started_at, cpu_before, tracemalloc)
            outcome = ExperimentOutcome(
                plan=plan,
                verdict=OutcomeVerdict.FAILED,
                detail=f"{type(error).__name__}: {error}",
                completed_at=now,
            )
            # A failed run is recorded exactly as fully as a successful one. A bundle written
            # only on success would make the archive a record of things that worked, which is
            # the selection bias this project exists to resist.
            return self._record(
                plan, registration, outcome, code, environment, applied_costs, design, resources
            )

        resources = _usage(started_at, cpu_before, tracemalloc)
        outcome = self._grade(plan, measured, now=now)
        return self._record(
            plan, registration, outcome, code, environment, applied_costs, design, resources
        )

    def _spend(self, plan: ExperimentPlan, *, now: datetime) -> ProtectedAccess:
        """Consume every protected window the plan names, before any of it is read."""
        protected = tuple(
            snapshot
            for snapshot in plan.datasets
            if snapshot.role == DataRole.PROTECTED_EVALUATION.value
        )
        if not protected:
            raise ExperimentRunError(
                "a confirmatory plan names no PROTECTED_EVALUATION dataset, so there is no "
                "holdout to spend and nothing here would be evidence"
            )
        spent: list[tuple[str, DataRole]] = []
        for snapshot in protected:
            self._registry.consume(
                plan.hypothesis_id,
                plan.hypothesis_version,
                dataset=snapshot.dataset,
                role=DataRole.PROTECTED_EVALUATION,
                now=now,
            )
            spent.append((snapshot.dataset, DataRole.PROTECTED_EVALUATION))
        return ProtectedAccess(
            hypothesis_id=plan.hypothesis_id,
            version=plan.hypothesis_version,
            windows=tuple(spent),
            consumed_at=now,
            _issued=_ISSUE_TOKEN,
        )

    def _grade(
        self, plan: ExperimentPlan, measured: Measured, *, now: datetime
    ) -> ExperimentOutcome:
        """Decide what the measurement is allowed to be called.

        The measurement reports numbers; the verdict is derived here from the plan's own bar. A
        run cannot nominate itself a survivor.
        """
        missing = [probe for probe in plan.probes if probe not in measured.probes_run]
        if missing:
            return ExperimentOutcome(
                plan=plan,
                verdict=OutcomeVerdict.INCONCLUSIVE,
                effect=measured.effect,
                test_statistic=measured.test_statistic,
                probes_run=tuple(measured.probes_run),
                probes_failed=tuple(measured.probes_failed),
                diagnostics=dict(measured.diagnostics),
                detail=(
                    "probes required by the plan did not run, so the number cannot be read as "
                    f"evidence either way: {', '.join(missing)}"
                ),
                completed_at=now,
            )

        threshold = plan.statistics.luck_threshold_z
        statistic = measured.test_statistic
        if statistic is None:
            verdict = OutcomeVerdict.INCONCLUSIVE
            detail = "the measurement reported no test statistic, so nothing was tested"
        elif measured.probes_failed:
            # A probe objection is disqualifying regardless of the headline number. This is the
            # ordering that matters: a result that clears the bar and fails a probe is refuted,
            # not survived, or the probes would be decorative.
            verdict = OutcomeVerdict.REFUTED
            detail = f"adversarial probes objected: {', '.join(measured.probes_failed)}"
        elif abs(statistic) >= threshold:
            verdict = OutcomeVerdict.SURVIVED
            detail = f"t={statistic} cleared the {threshold} luck bar"
        else:
            verdict = OutcomeVerdict.REFUTED
            detail = f"t={statistic} did not clear the {threshold} luck bar"

        # The measurement's own words are kept, but as a diagnostic rather than as the verdict's
        # explanation. Letting the code being judged write the reason is the same defect as
        # letting it choose the verdict, one field over: "I would like to be a survivor" would
        # have been recorded verbatim as the reason a result was refuted.
        diagnostics = dict(measured.diagnostics)
        if measured.detail:
            diagnostics["measurement_detail"] = measured.detail

        return ExperimentOutcome(
            plan=plan,
            verdict=verdict,
            effect=measured.effect,
            confidence_low=measured.confidence_low,
            confidence_high=measured.confidence_high,
            test_statistic=statistic,
            probes_run=tuple(measured.probes_run),
            probes_failed=tuple(measured.probes_failed),
            diagnostics=diagnostics,
            detail=detail,
            completed_at=now,
        )

    def _record(
        self,
        plan: ExperimentPlan,
        registration: Registration,
        outcome: ExperimentOutcome,
        code: CodeProvenance,
        environment: EnvironmentFingerprint,
        applied_costs: Mapping[str, str],
        design: ExperimentDesign,
        resources: ResourceUsage,
    ) -> RunResult:
        """Build the bundle, write it, and only then hand back the outcome (#18).

        The ordering matters for the same reason consumption precedes measurement: if writing
        fails, the caller gets an exception rather than a usable verdict whose provenance never
        reached disk. A result you can act on and cannot trace is the thing the manifest exists
        to prevent.

        The filename carries the hypothesis, its version and the manifest hash. The hash makes
        the name content-addressed, so re-running an identical experiment lands on the identical
        file and `write_experiment_manifest` accepts the exact repeat, while any difference in
        inputs or results produces a different name instead of a conflict. The hypothesis prefix
        is there for humans reading the directory.
        """
        manifest = self._manifest(
            plan, registration, outcome, code, environment, applied_costs, design, resources
        )
        destination = self._manifests / (
            f"{plan.hypothesis_id}-v{plan.hypothesis_version}-{manifest.manifest_hash[:16]}.json"
        )
        write_experiment_manifest(destination, manifest)
        return RunResult(outcome=outcome, manifest=manifest, manifest_path=destination)

    def _manifest(
        self,
        plan: ExperimentPlan,
        registration: Registration,
        outcome: ExperimentOutcome,
        code: CodeProvenance,
        environment: EnvironmentFingerprint,
        applied_costs: Mapping[str, str],
        design: ExperimentDesign,
        resources: ResourceUsage,
    ) -> ExperimentManifest:
        """Build the provenance bundle (#18), as CONFIRMATORY because a plan is registered.

        `mode` defaults to EXPLORATORY everywhere else on purpose -- a run that forgets to
        declare one must not pass as evidence. Here the registration is in hand, so the stronger
        claim is the true one.
        """
        window = _plan_window(plan.datasets)
        return ExperimentManifest(
            experiment_id=content_id(
                {
                    "hypothesis": plan.hypothesis_id,
                    "version": plan.hypothesis_version,
                    "registration": plan.registration_hash,
                    "completed_at": outcome.completed_at.isoformat(),
                }
            ),
            git_commit=code.git_commit,
            data_hash=content_id([snapshot.content_hash for snapshot in plan.datasets]),
            configuration_hash=plan.registration_hash,
            # Claimed economics first, then what was actually charged. Both are kept.
            costs={**plan.costs, **applied_costs},
            periods=(window,),
            walk_forward_splits=tuple(design.walk_forward_splits),
            holdout=window,
            neighborhood_grid=dict(design.neighborhood_grid),
            ablations=tuple(design.ablations),
            results={
                "primary": {
                    "verdict": outcome.verdict.value,
                    "effect": "" if outcome.effect is None else str(outcome.effect),
                    "test_statistic": (
                        "" if outcome.test_statistic is None else str(outcome.test_statistic)
                    ),
                    "probes_run": ", ".join(outcome.probes_run),
                    "probes_failed": ", ".join(outcome.probes_failed),
                }
            },
            mode="CONFIRMATORY",
            hypothesis_id=plan.hypothesis_id,
            hypothesis_version=plan.hypothesis_version,
            registration_hash=plan.registration_hash,
            minimum_detectable_effect=str(registration.power.minimum_detectable_effect),
            power_verdict="OVERRIDDEN" if registration.power.override else "POWERED",
            datasets=tuple(plan.datasets),
            statistics=plan.statistics,
            code=code,
            environment=environment,
            resources=resources,
        )


def _usage(started_at: datetime, cpu_before: float, tracer: ModuleType) -> ResourceUsage:
    """Close out the measurement window. Approximate, and honest about which approximation."""
    _, peak_bytes = tracer.get_traced_memory()
    tracer.stop()
    return ResourceUsage(
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=time.process_time() - cpu_before),
        peak_memory_mb=int(peak_bytes / (1024 * 1024)),
        cpu_seconds=Decimal(str(round(time.process_time() - cpu_before, 6))),
    )


def _plan_window(datasets: Sequence[DatasetSnapshot]) -> ExperimentPeriod:
    """The span the plan actually covers, taken from its own snapshots rather than declared."""
    return ExperimentPeriod(
        name="protected-evaluation",
        start=min(snapshot.start for snapshot in datasets),
        end=max(snapshot.end for snapshot in datasets),
    )


__all__ = [
    "ExperimentDesign",
    "ExperimentRunError",
    "ExperimentRunner",
    "Measured",
    "Measurement",
    "ProtectedAccess",
    "RunResult",
]
