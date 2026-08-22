"""Capture what produced a result, and attack the result with what software can prove (#18).

Three jobs, all of them drawn from defects this project actually paid for.

**Capture.** Git commit *and* dirty flag, interpreter, platform class, dependency-lock hash.
A dirty tree means the commit does not describe what ran, so it is recorded rather than assumed
clean.

**Compare.** `REFUTED.md` records that three of this project's analysis errors came from a
harness that was not connected to the code under test, and the tell was byte-identical output
across runs that should have differed. So the comparison answers both directions: why are these
two runs different when they claim to be the same experiment, and — the one that would have
caught those three — why are they identical when the inputs changed?

**Attack.** None of the recorded defects was caught by the code looking wrong; each was caught
by the number being implausible. The invariants here encode the implausibilities that already
happened: a wrong statistical test for the dependence structure, costs that improve returns,
forced liquidation that creates wealth, and a significance claim that does not clear the bar it
was judged against.

`check_invariants` reports what it *could not* check as well as what failed. An invariant that
silently skips because the result did not report the figure it needs is indistinguishable from
one that passed, and this project has been burned by exactly that shape of false assurance.
"""

from __future__ import annotations

import hashlib
import platform
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from quantbot.research.manifest import (
    VALID_FOR,
    EnvironmentFingerprint,
    ExperimentManifest,
)


@dataclass(frozen=True, slots=True)
class Difference:
    """One reason two manifests are not the same experiment."""

    field: str
    left: str
    right: str
    #: Set when the difference means the runs cannot be compared at all, rather than merely
    #: differing in something recorded.
    material: bool = True

    def __str__(self) -> str:
        return f"{self.field}: {self.left} != {self.right}"


@dataclass(frozen=True, slots=True)
class Violation:
    """One invariant a result breaks. Named so the failure is not merely 'suspicious'."""

    invariant: str
    detail: str


@dataclass(frozen=True, slots=True)
class InvariantReport:
    """What was checked, what failed, and what could not be checked at all.

    `skipped` is not decoration. A check that quietly does nothing because the result did not
    report the figure it needs looks exactly like a check that passed.
    """

    violations: tuple[Violation, ...]
    checked: tuple[str, ...]
    skipped: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.violations


def capture_environment(
    *,
    host_class: str,
    lock_file: Path | None = None,
    random_seed: int | None = None,
    deterministic: bool = True,
) -> EnvironmentFingerprint:
    """Fingerprint the interpreter and dependency set, without identifying the machine."""
    lock = lock_file or Path("uv.lock")
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest() if lock.exists() else "no-lock-file"
    return EnvironmentFingerprint(
        python_version=sys.version.split()[0],
        platform=f"{platform.system()}-{platform.machine()}",
        dependency_lock_hash=lock_hash,
        host_class=host_class,
        random_seed=random_seed,
        deterministic=deterministic,
    )


def capture_git_state(repository: Path | None = None) -> tuple[str, bool]:
    """Return the commit and whether the tree was dirty. A dirty tree is recorded, not hidden."""
    root = repository or Path.cwd()
    commit = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain")
    return commit or "unknown", bool(status)


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def compare(left: ExperimentManifest, right: ExperimentManifest) -> list[Difference]:
    """Explain why two runs are not the same experiment, in both directions.

    The reverse direction is the one that matters most here: identical results from different
    inputs mean the harness was not connected to the thing that changed.
    """
    differences: list[Difference] = []
    for field in (
        "configuration_hash",
        "data_hash",
        "git_commit",
        "mode",
        "registration_hash",
        "hypothesis_id",
        "hypothesis_version",
        "minimum_detectable_effect",
        "power_verdict",
    ):
        before, after = getattr(left, field), getattr(right, field)
        if before != after:
            differences.append(Difference(field, str(before), str(after)))

    differences.extend(_compare_models(left, right, "code"))
    differences.extend(_compare_models(left, right, "environment"))
    differences.extend(_compare_models(left, right, "statistics"))

    left_datasets = {(snapshot.dataset, snapshot.role): snapshot for snapshot in left.datasets}
    right_datasets = {(snapshot.dataset, snapshot.role): snapshot for snapshot in right.datasets}
    for key in sorted(set(left_datasets) | set(right_datasets), key=str):
        before_snapshot = left_datasets.get(key)
        after_snapshot = right_datasets.get(key)
        if before_snapshot != after_snapshot:
            differences.append(
                Difference(
                    f"datasets[{key[0]}/{key[1]}]",
                    "absent" if before_snapshot is None else before_snapshot.snapshot,
                    "absent" if after_snapshot is None else after_snapshot.snapshot,
                )
            )

    differences.extend(_compare_workers(left, right))

    if left.results != right.results:
        differences.append(Difference("results", "differ", "differ"))
    elif left.inputs_hash != right.inputs_hash:
        # The harness-not-connected signature. Three of this project's recorded analysis
        # errors looked exactly like this and were spotted only because a human noticed.
        differences.append(
            Difference(
                "results",
                "identical",
                "identical despite different inputs -- the run may not exercise what changed",
            )
        )
    return differences


def _compare_workers(left: ExperimentManifest, right: ExperimentManifest) -> list[Difference]:
    """Explain how two runs' external workers differ (#18).

    Without this a manifest whose Kronos checkpoint changed, or whose semantic analysis ran over
    a revised source bundle, compared as identical -- the two runs would claim to be the same
    experiment while one of them had been re-inferred under different weights.

    Workers are keyed by name rather than position: reordering the list is not a change, and a
    worker appearing or disappearing is.
    """
    differences: list[Difference] = []
    before = {worker.name: worker for worker in left.workers}
    after = {worker.name: worker for worker in right.workers}
    for name in sorted(set(before) | set(after)):
        left_worker, right_worker = before.get(name), after.get(name)
        if left_worker == right_worker:
            continue
        if left_worker is None or right_worker is None:
            differences.append(
                Difference(
                    f"workers[{name}]",
                    "absent" if left_worker is None else "present",
                    "absent" if right_worker is None else "present",
                )
            )
            continue
        if left_worker.fingerprint != right_worker.fingerprint:
            # Named separately from the fields below so a reader sees the conclusion -- these
            # are not the same inference -- before the list of what moved.
            differences.append(
                Difference(
                    f"workers[{name}].fingerprint",
                    left_worker.fingerprint,
                    right_worker.fingerprint,
                )
            )
        for key in sorted(set(left_worker.configuration) | set(right_worker.configuration)):
            first = left_worker.configuration.get(key, "absent")
            second = right_worker.configuration.get(key, "absent")
            if first != second:
                differences.append(Difference(f"workers[{name}].{key}", first, second))
        for field in ("artifact_id", "input_hash", "as_of", "search_cardinality"):
            first_value = getattr(left_worker, field)
            second_value = getattr(right_worker, field)
            if first_value != second_value:
                differences.append(
                    Difference(f"workers[{name}].{field}", str(first_value), str(second_value))
                )
    return differences


def _compare_models(
    left: ExperimentManifest, right: ExperimentManifest, field: str
) -> list[Difference]:
    before = getattr(left, field)
    after = getattr(right, field)
    if before == after:
        return []
    if before is None or after is None:
        return [
            Difference(
                field,
                "absent" if before is None else "present",
                "absent" if after is None else "present",
            )
        ]
    return [
        Difference(f"{field}.{name}", str(getattr(before, name)), str(getattr(after, name)))
        for name in type(before).model_fields
        if getattr(before, name) != getattr(after, name)
    ]


#: Result keys the invariants read. Present-or-absent decides checked versus skipped.
GROSS = "gross_return"
NET = "net_return"
TERMINAL = "terminal_wealth"
TERMINAL_WITH_LIQUIDATION = "terminal_wealth_with_forced_liquidation"
STATISTIC = "test_statistic"
SIGNIFICANT = "significant"


def check_invariants(manifest: ExperimentManifest) -> InvariantReport:
    """Attack the result with the implausibilities this project has already lived through."""
    violations: list[Violation] = []
    checked: list[str] = []
    skipped: list[str] = []

    plan = manifest.statistics
    if plan is None:
        skipped.append("statistical-test-matches-dependence")
        skipped.append("significance-clears-the-frozen-bar")
    else:
        checked.append("statistical-test-matches-dependence")
        if not plan.test_matches_data:
            violations.append(
                Violation(
                    "statistical-test-matches-dependence",
                    f"{plan.test.value} assumes "
                    f"{'/'.join(sorted(VALID_FOR[plan.test]))} data but the comparison is "
                    f"{plan.comparison}",
                )
            )
        violations.extend(_check_significance(manifest, plan.luck_threshold_z, checked, skipped))

    for name, values in sorted(manifest.results.items()):
        violations.extend(_check_costs(name, values, manifest.costs, checked, skipped))
        violations.extend(_check_liquidation(name, values, checked, skipped))

    return InvariantReport(
        violations=tuple(violations),
        checked=tuple(dict.fromkeys(checked)),
        skipped=tuple(dict.fromkeys(skipped)),
    )


def _check_costs(
    name: str,
    values: dict[str, str],
    costs: dict[str, str],
    checked: list[str],
    skipped: list[str],
) -> list[Violation]:
    """Costs never improve a return. A margin path once appeared to create wealth this way."""
    invariant = "costs-never-improve-returns"
    gross, net = _decimal(values.get(GROSS)), _decimal(values.get(NET))
    slippage = _decimal(costs.get("slippage_bps"))
    if gross is None or net is None or slippage is None:
        skipped.append(invariant)
        return []
    checked.append(invariant)
    if slippage > 0 and net > gross:
        return [
            Violation(
                invariant,
                f"{name}: net {net} exceeds gross {gross} at {slippage}bps of slippage",
            )
        ]
    return []


def _check_liquidation(
    name: str, values: dict[str, str], checked: list[str], skipped: list[str]
) -> list[Violation]:
    """A forced liquidation cannot end richer than the same path without one.

    Cycle 12 produced exactly this, by comparing a monthly-rebalanced margin path against a
    daily-rebalanced baseline. The numbers looked like margin calls creating wealth.
    """
    invariant = "forced-liquidation-never-creates-wealth"
    free = _decimal(values.get(TERMINAL))
    liquidated = _decimal(values.get(TERMINAL_WITH_LIQUIDATION))
    if free is None or liquidated is None:
        skipped.append(invariant)
        return []
    checked.append(invariant)
    if liquidated > free:
        return [
            Violation(
                invariant,
                f"{name}: forced liquidation ends at {liquidated} against {free} without it",
            )
        ]
    return []


def _check_significance(
    manifest: ExperimentManifest,
    threshold: Decimal,
    checked: list[str],
    skipped: list[str],
) -> list[Violation]:
    """A result cannot claim significance below the bar frozen for its own trial count."""
    invariant = "significance-clears-the-frozen-bar"
    violations: list[Violation] = []
    saw_any = False
    for name, values in sorted(manifest.results.items()):
        statistic = _decimal(values.get(STATISTIC))
        claim = values.get(SIGNIFICANT)
        if statistic is None or claim is None:
            continue
        saw_any = True
        if claim.lower() == "true" and abs(statistic) < threshold:
            violations.append(
                Violation(
                    invariant,
                    f"{name}: claims significance at |t|={abs(statistic)} against a "
                    f"{threshold} bar",
                )
            )
    (checked if saw_any else skipped).append(invariant)
    return violations


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    return parsed if parsed.is_finite() else None
