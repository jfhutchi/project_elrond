"""Whether a frozen experiment could be re-executed, answered before anything claims it was (#48).

Every reproducible bundle this runner writes names a command:

    quantbot reproduce-experiment --experiment <id>

**That command did not exist.** The manifest instructed a reader to run something no CLI
implemented, which is the shape of claim this project keeps finding — an assertion with no
mechanism behind it, sitting inside the evidence record whose whole authority is that its claims
are mechanical.

This module implements what can honestly be implemented today, and refuses to be mistaken for the
rest. It answers:

> Can every immutable input this manifest needs still be materialised, and is the code it names
> recoverable?

and it reports, in the same object, that **no rerun was performed**.

**Why not the reproducer itself.** #48's first acceptance criterion is that one real historical
confirmatory experiment can be rerun from its manifest. There is no such manifest: nothing outside
an integration test has ever produced one, which is consistent with the project standing at M0 and
having no confirmatory experiment to its name. A reproducer built now could only ever be exercised
against a synthetic fixture, and `CLAUDE.md` is explicit that a feature nobody has exercised is a
feature nobody may claim. So this is the readiness half — real, exercisable, and named for what it
does.

**Fails closed, in three directions.**

* A recorded input that is missing, altered or truncated blocks. The hash is compared against the
  bytes the runner wrote, not against whatever is beside the bundle now — an input directory that
  drifted is exactly the accidental local state #48 is about.
* A dirty original tree with no preserved patch blocks, permanently. `git_commit=<sha>, dirty=true`
  does not describe code that was never committed, and treating it as sufficient would let a
  result claim reproducibility from a working tree nobody kept.
* A dependency-lock difference is reported as drift rather than as a pass. It does not block on its
  own — a lock can change without touching the code a result depended on — but it is never silent.

**What it deliberately does not do**: resolve dataset snapshots back to bytes, reconstruct the
compiled plan, run anything, or compare results. Those are #48's remaining minimum behaviours and
they are listed by name in `outstanding`, so a reader of a clean readiness report cannot mistake it
for a reproduction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from quantbot.research.manifest import ExperimentManifest
from quantbot.research.reproducibility import check_invariants

#: The minimum behaviours #48 asks for that this command does not implement. Named individually
#: rather than summarised, so a reader can tell which parts of "reproducible" are still missing
#: instead of inferring it from an absence.
OUTSTANDING: tuple[str, ...] = (
    "resolve the dataset snapshots named by the manifest back to their exact bytes",
    "reconstruct the compiled experiment plan from the registration",
    "execute it through the authoritative path the manifest names",
    "produce a new manifest and compare it to the original",
    "replay a frozen external-worker artifact rather than re-running its inference",
)


@dataclass(frozen=True, slots=True)
class InputCheck:
    """One recorded reproduction input, and whether it is still exactly what was written."""

    name: str
    expected_hash: str
    #: `None` when the file is absent, which is a different fact from a hash that disagrees.
    actual_hash: str | None

    @property
    def present(self) -> bool:
        return self.actual_hash is not None

    @property
    def intact(self) -> bool:
        return self.actual_hash == self.expected_hash

    def explain(self) -> str:
        if not self.present:
            return f"{self.name}: missing"
        if not self.intact:
            return f"{self.name}: content changed since the run"
        return f"{self.name}: intact"


@dataclass(frozen=True, slots=True)
class ReproductionReadiness:
    """What stands between a frozen bundle and an attempt to re-execute it.

    `performed` is a field rather than an omission. A report that merely never mentions running
    the experiment reads, to a hurried reader, exactly like one that ran it and found no
    differences — and that reader is the one deciding whether to trust the evidence.
    """

    experiment_id: str
    manifest_path: Path
    manifest_hash: str
    #: Always false. This command reports whether a rerun could be attempted; it never attempts.
    performed: bool
    inputs: tuple[InputCheck, ...]
    blocking: tuple[str, ...]
    #: Differences that do not block but must not be silent.
    drift: tuple[str, ...]
    outstanding: tuple[str, ...]

    @property
    def can_attempt(self) -> bool:
        """Whether every input and the code itself are still recoverable.

        Not "this reproduces". Nothing here ran, and `outstanding` says what would have to exist
        before anything could.
        """
        return not self.blocking

    def explain(self) -> str:
        headline = "INPUTS_RECOVERABLE" if self.can_attempt else "NOT_REPRODUCIBLE"
        parts = [f"{self.experiment_id}: {headline}"]
        parts.extend(self.blocking)
        parts.extend(f"drift: {item}" for item in self.drift)
        parts.append(f"no rerun performed; {len(self.outstanding)} capabilities outstanding (#48)")
        return "; ".join(parts)


class ManifestNotFound(FileNotFoundError):
    """Raised when no bundle in the directory carries the requested experiment id."""


def _manifests_in(directory: Path) -> Iterator[tuple[Path, ExperimentManifest]]:
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            yield path, ExperimentManifest.model_validate(payload)
        except (OSError, ValueError):
            # A file that is not a manifest is not an error here. This directory is addressed by
            # experiment id, and refusing to answer because something unrelated sits beside the
            # bundle would make the command fail for a reason the caller cannot act on.
            continue


def find_manifest(directory: Path, experiment_id: str) -> tuple[Path, ExperimentManifest]:
    """The bundle carrying this experiment id, by reading manifests rather than guessing names.

    The filename is content-addressed on the *manifest* hash, and `experiment_id` is a different
    hash, so the id cannot be turned into a path. Reading is the only correct lookup, and it is
    also the one that keeps working when a directory is moved or a file renamed.
    """
    for path, manifest in _manifests_in(directory):
        if manifest.experiment_id == experiment_id:
            return path, manifest
    raise ManifestNotFound(f"no manifest in {directory} carries experiment id {experiment_id!r}")


def _check_inputs(manifest: ExperimentManifest, inputs_directory: Path) -> tuple[InputCheck, ...]:
    checks: list[InputCheck] = []
    for recorded in manifest.reproduction_inputs:
        candidate = inputs_directory / recorded.name
        actual: str | None = None
        if candidate.is_file():
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        checks.append(
            InputCheck(
                name=recorded.name,
                expected_hash=recorded.content_hash,
                actual_hash=actual,
            )
        )
    return tuple(checks)


def assess_reproducibility(
    manifest_path: Path,
    manifest: ExperimentManifest,
    *,
    current_lock_hash: str | None = None,
    preserved_patch: Path | None = None,
) -> ReproductionReadiness:
    """Report what would stop this bundle being re-executed, without re-executing it.

    `current_lock_hash` and `preserved_patch` are passed in rather than discovered, for the same
    reason every other component here takes its clock as an argument: a function that reads the
    machine it happens to be running on cannot be tested about what it would say on another one.
    """
    inputs_directory = manifest_path.parent / f"{manifest.experiment_id}.inputs"
    checks = _check_inputs(manifest, inputs_directory)
    blocking: list[str] = []
    drift: list[str] = []

    if not manifest.reproduction_command:
        blocking.append(
            "the bundle records no reproduction command, so it never claimed to be "
            "re-executable; this is a statement about the bundle rather than a fault in it"
        )
    for check in checks:
        if not check.present:
            blocking.append(f"recorded input {check.name!r} is no longer beside the bundle")
        elif not check.intact:
            blocking.append(
                f"recorded input {check.name!r} does not match the bytes the run wrote; the "
                "directory has drifted from the manifest that describes it"
            )

    if manifest.code is not None and manifest.code.dirty:
        if preserved_patch is None or not preserved_patch.is_file():
            # Permanent, not a warning. A commit does not describe code that was never
            # committed, and calling this reproducible would be the strongest false claim
            # available in the whole bundle.
            blocking.append(
                f"the original ran on a dirty tree at {manifest.code.git_commit} with no "
                "preserved patch, so the code that produced this result was never recorded "
                "anywhere and exact reproduction is impossible"
            )
        else:
            drift.append(f"the original tree was dirty; reproduction needs {preserved_patch}")

    if current_lock_hash is not None and manifest.environment is not None:
        recorded = manifest.environment.dependency_lock_hash
        if recorded != current_lock_hash:
            drift.append(
                f"dependency lock differs: recorded {recorded[:16]}, current "
                f"{current_lock_hash[:16]}"
            )

    report = check_invariants(manifest)
    blocking.extend(
        f"manifest invariant violated: {violation.invariant} ({violation.detail})"
        for violation in report.violations
    )
    drift.extend(f"invariant not checked: {item}" for item in report.skipped)

    return ReproductionReadiness(
        experiment_id=manifest.experiment_id,
        manifest_path=manifest_path,
        manifest_hash=manifest.manifest_hash,
        performed=False,
        inputs=checks,
        blocking=tuple(blocking),
        drift=tuple(drift),
        outstanding=OUTSTANDING,
    )


__all__ = [
    "OUTSTANDING",
    "InputCheck",
    "ManifestNotFound",
    "ReproductionReadiness",
    "assess_reproducibility",
    "find_manifest",
]
