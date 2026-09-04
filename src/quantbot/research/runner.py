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

import hashlib
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Protocol

from quantbot.research.artifact_testing import ArtifactOutcome, ArtifactOutcomeVerdict
from quantbot.research.builder import ExperimentOutcome, ExperimentPlan, OutcomeVerdict
from quantbot.research.manifest import (
    CodeProvenance,
    DatasetSnapshot,
    EnvironmentFingerprint,
    ExperimentManifest,
    ExperimentPeriod,
    ReproductionInput,
    ResourceUsage,
    content_id,
    write_experiment_manifest,
)
from quantbot.research.memory import RecordKind, ResearchRecord, Verdict
from quantbot.research.power import supports_registered_direction
from quantbot.research.registry import DataRole, HypothesisRegistry, Registration
from quantbot.research.validation_campaign import (
    CampaignOutcome,
    CampaignVerdict,
    ScenarioMeasured,
    grade_campaign,
)


def _refuse_bad_input_name(name: str) -> None:
    """A reproduction input is a plain filename, not a path.

    The names come from a measurement, and for a generated experiment the measurement is
    downstream of model-authored code -- a name like `../../.env` would let it choose where the
    runner writes. Refused rather than sanitised: a silently rewritten filename is one nobody
    can match back to the manifest that references it.
    """
    if "/" in name or "\\" in name or Path(name).name != name or not name.strip():
        raise ExperimentRunError(
            f"reproduction input {name!r} is not a plain filename; a measurement does "
            "not choose where the runner writes"
        )


def _describe_inputs(inputs: Mapping[str, str] | None) -> tuple[ReproductionInput, ...]:
    """Hash what a rerun will need, from the content in memory rather than from disk.

    Ordered by name so the manifest hash does not depend on dict iteration order, which would
    make two identical runs produce two different bundles.
    """
    if not inputs:
        return ()
    described: list[ReproductionInput] = []
    for name in sorted(inputs):
        _refuse_bad_input_name(name)
        encoded = inputs[name].encode("utf-8")
        described.append(
            ReproductionInput(
                name=name,
                content_hash=hashlib.sha256(encoded).hexdigest(),
                bytes=len(encoded),
            )
        )
    return tuple(described)


#: How a campaign verdict lands on the primary outcome when the primary already survived. Not a
#: general translation: it is only ever read in the downgrade direction, so `SURVIVED -> SURVIVED`
#: means "the gate did not object", never "the gate produced support".
_CAMPAIGN_VERDICTS: dict[CampaignVerdict, OutcomeVerdict] = {
    CampaignVerdict.SURVIVED: OutcomeVerdict.SURVIVED,
    CampaignVerdict.REFUTED: OutcomeVerdict.REFUTED,
    CampaignVerdict.UNDERPOWERED: OutcomeVerdict.UNDERPOWERED,
    CampaignVerdict.INCONCLUSIVE: OutcomeVerdict.INCONCLUSIVE,
}


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
    #: Files a rerun needs, by name. For a generated experiment this is the model-authored
    #: source: the manifest records its digest, and a digest identifies code without being able
    #: to run it. Written beside the bundle so the bundle is self-contained -- reproduction that
    #: depends on somebody having kept a file elsewhere is reproduction on trust.
    reproduction_inputs: Mapping[str, str] = field(default_factory=dict)
    #: Probes that actually ran. The runner checks this against the plan rather than trusting it.
    probes_run: tuple[str, ...] = ()
    #: Probes that ran and objected.
    probes_failed: tuple[str, ...] = ()
    diagnostics: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""
    #: Numeric results for preregistered artifact tests. Verdicts are derived by the runner.
    artifact_measurements: tuple[ArtifactMeasured, ...] = ()
    #: Numeric results for the registration's frozen validation scenarios (#159). Keyed by
    #: frozen `scenario_id`, and `ScenarioMeasured` carries no verdict field at all -- a worker
    #: reports what it measured and the runner decides what that is allowed to be called.
    scenario_measurements: tuple[ScenarioMeasured, ...] = ()


@dataclass(frozen=True, slots=True)
class ArtifactMeasured:
    """Numbers produced for one preregistered component or interaction contrast."""

    test_id: str
    effect: Decimal | None
    test_statistic: Decimal | None = None
    confidence_low: Decimal | None = None
    confidence_high: Decimal | None = None
    probes_run: tuple[str, ...] = ()
    probes_failed: tuple[str, ...] = ()


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
        registered_graph_hash = (
            None if registration.draft.graph is None else registration.draft.graph.graph_id
        )
        if plan.hypothesis_graph_hash != registered_graph_hash:
            raise ExperimentRunError(
                f"plan names component graph {plan.hypothesis_graph_hash} but registration "
                f"{registration.content_hash} froze {registered_graph_hash}; graph provenance "
                "cannot be supplied or changed independently of the registered claim"
            )
        for artifact_test in plan.artifact_tests:
            persisted = self._registry.get_artifact_test(artifact_test.test_id)
            if persisted is None or persisted.content_hash != artifact_test.content_hash:
                raise ExperimentRunError(
                    f"artifact test {artifact_test.test_id} is not the exact preregistered design"
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
                artifact_outcomes=self._grade_artifacts(plan, Measured(effect=None)),
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
        outcome = self._grade(plan, registration, measured, now=now)
        return self._record(
            plan,
            registration,
            outcome,
            code,
            environment,
            applied_costs,
            design,
            resources,
            measured.reproduction_inputs,
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

    def _grade_validation_campaign(
        self, registration: Registration, measured: Measured
    ) -> CampaignOutcome | None:
        """Grade the campaign the *registration* froze, using #215's grader unchanged.

        **The campaign is read from the frozen registration, never accepted from the caller.**
        That is the binding: there is no parameter through which campaign B could be offered in
        place of registered campaign A after results are visible. `run()` separately refuses a
        registration whose `content_hash` does not match the plan, and the campaign is inside
        that hash, so the substitution is closed twice.

        Scenario measurements are matched by frozen `scenario_id`, so a result computed for one
        scenario cannot satisfy another. Missing, duplicated and unknown ids are integrity
        defects `grade_campaign` already derives; none of that logic is restated here, because a
        second copy of the rules is how the two come to disagree.

        Supplying scenario results for a registration that froze no campaign is refused rather
        than ignored. Silently dropping them would let a worker believe hostile validation had
        been graded when nothing was.
        """
        campaign = registration.draft.validation_campaign
        if campaign is None:
            if measured.scenario_measurements:
                raise ExperimentRunError(
                    "the measurement reported validation scenarios, but "
                    f"{registration.draft.hypothesis_id} v{registration.draft.version} froze no "
                    "validation campaign; results for a campaign nobody registered are not "
                    "hostile validation"
                )
            return None
        return grade_campaign(
            campaign,
            tuple(measured.scenario_measurements),
            luck_threshold_z=registration.power.luck_threshold_z,
        )

    def _grade(
        self,
        plan: ExperimentPlan,
        registration: Registration,
        measured: Measured,
        *,
        now: datetime,
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
                artifact_outcomes=self._grade_artifacts(plan, measured),
                validation_outcome=self._grade_validation_campaign(registration, measured),
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
        elif supports_registered_direction(statistic, threshold):
            verdict = OutcomeVerdict.SURVIVED
            detail = f"t={statistic} cleared the {threshold} luck bar"
        elif statistic <= -threshold:
            # Signed, not `abs()`. Every registration claims the effect *exceeds* its null, so a
            # large negative statistic is overwhelming evidence against the preregistered
            # direction -- and the previous `abs(statistic) >= threshold` called it SURVIVED.
            # Separated from the ordinary miss below because the two are different findings and
            # a later reader deciding whether to keep pursuing the mechanism needs to tell them
            # apart: one says "no signal", this says "signal, pointing the other way".
            verdict = OutcomeVerdict.REFUTED
            detail = (
                f"t={statistic} is significant in the direction opposite to the registered "
                f"claim, which is evidence against it rather than for it; the {threshold} luck "
                "bar is one-sided because the registration is"
            )
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

        campaign = self._grade_validation_campaign(registration, measured)
        # **A hostile gate subtracts; it never adds.** The campaign is consulted only where the
        # primary result already stands, so it can block a survivor and can never turn a refuted,
        # underpowered or unreadable primary into one. Applying it symmetrically would make a
        # favourable set of scenarios a second route to support -- exactly the "another way to
        # get to yes" this project keeps having to close.
        if campaign is not None and verdict is OutcomeVerdict.SURVIVED:
            downgraded = _CAMPAIGN_VERDICTS[campaign.verdict]
            if downgraded is not OutcomeVerdict.SURVIVED:
                detail = (
                    f"validation campaign {campaign.verdict.value.lower()}: {campaign.detail}"
                )
            verdict = downgraded

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
            artifact_outcomes=self._grade_artifacts(plan, measured),
            validation_outcome=campaign,
            detail=detail,
            completed_at=now,
        )

    @staticmethod
    def _grade_artifacts(
        plan: ExperimentPlan, measured: Measured
    ) -> tuple[ArtifactOutcome, ...]:
        """Grade only preregistered numeric contrasts; missing results stay unassessed."""
        supplied: dict[str, ArtifactMeasured] = {}
        duplicates: set[str] = set()
        for reported in measured.artifact_measurements:
            if reported.test_id in supplied:
                duplicates.add(reported.test_id)
                continue
            supplied[reported.test_id] = reported

        outcomes: list[ArtifactOutcome] = []
        for test in plan.artifact_tests:
            result = supplied.get(test.test_id)
            if test.test_id in duplicates:
                verdict = ArtifactOutcomeVerdict.UNASSESSED
                detail = "the artifact contrast reported duplicate, ambiguous results"
            elif result is None:
                verdict = ArtifactOutcomeVerdict.UNASSESSED
                detail = "the preregistered artifact contrast reported no result"
            elif result.test_statistic is None:
                verdict = ArtifactOutcomeVerdict.UNASSESSED
                detail = "the artifact contrast reported no test statistic"
            else:
                missing = [probe for probe in plan.probes if probe not in result.probes_run]
                if missing:
                    verdict = ArtifactOutcomeVerdict.UNASSESSED
                    detail = "artifact probes did not run: " + ", ".join(missing)
                elif test.power.override is not None:
                    verdict = ArtifactOutcomeVerdict.UNDERPOWERED
                    detail = (
                        "the operator accepted an underpowered artifact test; its measured "
                        "number cannot become confirmatory evidence"
                    )
                elif result.probes_failed:
                    verdict = ArtifactOutcomeVerdict.REFUTED
                    detail = "artifact probes objected: " + ", ".join(result.probes_failed)
                elif supports_registered_direction(
                    result.test_statistic, test.power.luck_threshold_z
                ):
                    verdict = ArtifactOutcomeVerdict.SURVIVED
                    detail = (
                        f"t={result.test_statistic} cleared the artifact test's frozen "
                        f"{test.power.luck_threshold_z} luck bar"
                    )
                elif result.test_statistic <= -test.power.luck_threshold_z:
                    # The same one-sided rule as the primary claim, for the same reason. A
                    # component contrast is registered as "this component contributes", so a
                    # large negative statistic says it *costs*, which is a refutation.
                    verdict = ArtifactOutcomeVerdict.REFUTED
                    detail = (
                        f"t={result.test_statistic} is significant in the direction opposite to "
                        "the registered artifact claim, which is evidence against it rather "
                        f"than for it; the {test.power.luck_threshold_z} bar is one-sided "
                        "because the registration is"
                    )
                else:
                    verdict = ArtifactOutcomeVerdict.REFUTED
                    detail = (
                        f"t={result.test_statistic} did not clear the artifact test's frozen "
                        f"{test.power.luck_threshold_z} luck bar"
                    )
            outcomes.append(
                ArtifactOutcome(
                    test_id=test.test_id,
                    test_registration_hash=test.content_hash,
                    artifact_kind=test.draft.artifact_kind,
                    artifact_content_id=test.draft.artifact_content_id,
                    verdict=verdict,
                    effect="" if result is None or result.effect is None else str(result.effect),
                    confidence_low=(
                        ""
                        if result is None or result.confidence_low is None
                        else str(result.confidence_low)
                    ),
                    confidence_high=(
                        ""
                        if result is None or result.confidence_high is None
                        else str(result.confidence_high)
                    ),
                    test_statistic=(
                        ""
                        if result is None or result.test_statistic is None
                        else str(result.test_statistic)
                    ),
                    probes_run=() if result is None else result.probes_run,
                    probes_failed=() if result is None else result.probes_failed,
                    detail=detail,
                )
            )
        return tuple(outcomes)

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
        measured_inputs: Mapping[str, str] | None = None,
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
        # Described before anything is written, so the manifest records the bytes the runner is
        # about to produce rather than whatever is on disk afterwards. A name the measurement
        # chose badly is refused here, before it can reach either the manifest or the filesystem.
        recorded_inputs = _describe_inputs(measured_inputs)
        manifest = self._manifest(
            plan,
            registration,
            outcome,
            code,
            environment,
            applied_costs,
            design,
            resources,
            inputs=recorded_inputs,
        )
        destination = self._manifests / (
            f"{plan.hypothesis_id}-v{plan.hypothesis_version}-{manifest.manifest_hash[:16]}.json"
        )
        write_experiment_manifest(destination, manifest)
        # Named by experiment id, matching what `reproduction_command` tells a reader to ask
        # for, so the command and the directory cannot drift apart.
        self._write_inputs(self._manifests / f"{manifest.experiment_id}.inputs", measured_inputs)
        return RunResult(outcome=outcome, manifest=manifest, manifest_path=destination)

    @staticmethod
    def _write_inputs(directory: Path, inputs: Mapping[str, str] | None) -> None:
        """Write the files a rerun needs, in a directory named for the bundle.

        Names were already refused by `_describe_inputs` before the manifest was built; the
        check is repeated here rather than assumed, because this method is what touches the
        filesystem and a guard that lives only in its caller is one call site away from absent.
        """
        if not inputs:
            return
        directory.mkdir(parents=True, exist_ok=True)
        for name, content in inputs.items():
            _refuse_bad_input_name(name)
            (directory / name).write_text(content, encoding="utf-8")

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
        inputs: tuple[ReproductionInput, ...] = (),
    ) -> ExperimentManifest:
        """Build the provenance bundle (#18), as CONFIRMATORY because a plan is registered.

        `mode` defaults to EXPLORATORY everywhere else on purpose -- a run that forgets to
        declare one must not pass as evidence. Here the registration is in hand, so the stronger
        claim is the true one.
        """
        window = _plan_window(plan.datasets)
        experiment_id = content_id(
            {
                "hypothesis": plan.hypothesis_id,
                "version": plan.hypothesis_version,
                "registration": plan.registration_hash,
                "completed_at": outcome.completed_at.isoformat(),
                "artifact_tests": [test.content_hash for test in plan.artifact_tests],
            }
        )
        return ExperimentManifest(
            experiment_id=experiment_id,
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
                # Diagnostics were computed, attached to the outcome, and dropped here. For a
                # generated experiment they carry the code digest and the model that wrote it,
                # so discarding them meant the bundle could not say what produced the number.
                # Namespaced rather than merged into `primary`, so a diagnostic key can never
                # collide with a result key and quietly overwrite a verdict.
                **{f"diagnostic:{key}": {"value": value}
                   for key, value in sorted(outcome.diagnostics.items())},
                **{
                    f"artifact:{artifact.test_id}": {
                        "test_registration_hash": artifact.test_registration_hash,
                        "artifact_kind": artifact.artifact_kind.value,
                        "artifact_content_id": artifact.artifact_content_id,
                        "verdict": artifact.verdict.value,
                        "effect": artifact.effect,
                        "confidence_low": artifact.confidence_low,
                        "confidence_high": artifact.confidence_high,
                        "test_statistic": artifact.test_statistic,
                        "probes_run": ", ".join(artifact.probes_run),
                        "probes_failed": ", ".join(artifact.probes_failed),
                        "detail": artifact.detail,
                    }
                    for artifact in outcome.artifact_outcomes
                },
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
            hypothesis_graph_hash=plan.hypothesis_graph_hash,
            minimum_detectable_effect=str(registration.power.minimum_detectable_effect),
            power_verdict="OVERRIDDEN" if registration.power.override else "POWERED",
            datasets=tuple(plan.datasets),
            statistics=plan.statistics,
            code=code,
            environment=environment,
            resources=resources,
            # Honest now that the bundle carries what a rerun needs. It stayed empty for
            # weeks on purpose: `verify-manifest` checks invariants and is not a re-execution,
            # and a command here that does not reproduce the result reads as reproducibility
            # somebody verified.
            #
            # Keyed on the experiment id rather than the manifest path, for two reasons. The
            # path is derived from the manifest hash, so putting it inside the manifest would
            # change the hash that named it -- the field cannot describe its own location. And
            # an id survives the directory being moved, which a path does not.
            reproduction_command=(
                ("quantbot", "reproduce-experiment", "--experiment", experiment_id)
                if inputs
                else ()
            ),
            reproduction_inputs=inputs,
        )


#: How a run's verdict becomes something memory may hold. `FAILED` is absent on purpose and is
#: handled separately below: an experiment that did not complete produced no evidence about the
#: hypothesis, and mapping it onto any research verdict would turn a crash into a finding.
_VERDICTS: Mapping[OutcomeVerdict, Verdict] = {
    OutcomeVerdict.SURVIVED: Verdict.SURVIVED,
    OutcomeVerdict.REFUTED: Verdict.REFUTED,
    OutcomeVerdict.UNDERPOWERED: Verdict.UNDERPOWERED,
}


def record_from_run(
    result: RunResult,
    *,
    record_id: str,
    subject: str,
    source: str,
    now: datetime,
) -> ResearchRecord:
    """Turn a completed run into a memory record that names the bundle behind it (#18, #6).

    `ResearchRecord.evidence_hash` has always been documented as "manifest hash of the bundle
    evidencing this" and nothing populated it, so every stored conclusion was a sentence with no
    route back to the run that produced it. A finding you cannot trace to a manifest is a
    narrative, and this project's whole premise is that it should not be storing those.

    Two outcomes deliberately do not become findings:

    * `FAILED` -- the experiment did not complete, so it produced no evidence about the
      hypothesis. It is recorded as an `ANALYSIS_DEFECT`, because this project's own error rate
      is evidence about the project even when it says nothing about the market.
    * `INCONCLUSIVE` -- it ran, but something about the run means the number cannot be read.
      Also a defect rather than a finding, for the same reason: a verdict here would be a claim
      the run explicitly declined to make.

    The statement is assembled from the outcome's own fields rather than written. Prose composed
    here would be a second account of the result, free to drift from the manifest it cites.
    """
    outcome = result.outcome
    plan = outcome.plan
    verdict = _VERDICTS.get(outcome.verdict)
    detail = {
        "verdict": outcome.verdict.value,
        "manifest_path": str(result.manifest_path),
        "probes_run": ", ".join(outcome.probes_run),
        "probes_failed": ", ".join(outcome.probes_failed),
    }
    if outcome.effect is not None:
        detail["effect"] = str(outcome.effect)
    if outcome.test_statistic is not None:
        detail["test_statistic"] = str(outcome.test_statistic)

    if verdict is None:
        return ResearchRecord(
            record_id=record_id,
            kind=RecordKind.ANALYSIS_DEFECT,
            subject=subject,
            statement=(
                f"{plan.hypothesis_id} v{plan.hypothesis_version} produced no readable result: "
                f"{outcome.detail}"
            ),
            hypothesis_id=plan.hypothesis_id,
            hypothesis_version=plan.hypothesis_version,
            evidence_hash=result.manifest.manifest_hash,
            source=source,
            recorded_at=now,
            detail=detail,
        )

    return ResearchRecord(
        record_id=record_id,
        kind=RecordKind.FINDING,
        subject=subject,
        statement=(
            f"{plan.hypothesis_id} v{plan.hypothesis_version}: {outcome.verdict.value}"
            f"{'' if outcome.detail == '' else f' -- {outcome.detail}'}"
        ),
        verdict=verdict,
        hypothesis_id=plan.hypothesis_id,
        hypothesis_version=plan.hypothesis_version,
        evidence_hash=result.manifest.manifest_hash,
        source=source,
        recorded_at=now,
        detail=detail,
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
    "ArtifactMeasured",
    "ExperimentDesign",
    "ExperimentRunError",
    "ExperimentRunner",
    "Measured",
    "Measurement",
    "ProtectedAccess",
    "RunResult",
    "record_from_run",
]
