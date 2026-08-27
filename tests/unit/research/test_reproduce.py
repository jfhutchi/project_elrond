"""Whether a frozen bundle could be re-executed, and every way that answer could flatter (#48).

The defect this module was written for is not subtle: every reproducible manifest named
`quantbot reproduce-experiment`, and no such command existed. The tests below are about the
narrower thing that replaced it not quietly becoming the same kind of claim.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.manifest import (
    CodeProvenance,
    EnvironmentFingerprint,
    ExecutionPath,
    ExperimentManifest,
    ReproductionInput,
    write_experiment_manifest,
)
from quantbot.research.reproduce import (
    OUTSTANDING,
    ManifestNotFound,
    assess_reproducibility,
    find_manifest,
)
from tests.unit.research.test_reproducibility import manifest as base_manifest

NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
LOCK = "a" * 64
INPUT_BYTES = b"print('hello')"


def manifest(**overrides: object) -> ExperimentManifest:
    """A confirmatory bundle, reusing the reproducibility fixture rather than rebuilding it.

    Rebuilt fixtures drift from the model they describe, and a manifest fixture that drifts is
    one asserting things about a shape production no longer produces.
    """
    inputs = overrides.pop(
        "reproduction_inputs",
        (
            ReproductionInput(
                name="strategy.py",
                content_hash=hashlib.sha256(INPUT_BYTES).hexdigest(),
                bytes=len(INPUT_BYTES),
            ),
        ),
    )
    command = overrides.pop(
        "reproduction_command", ("quantbot", "reproduce-experiment", "--experiment", "exp-1")
    )
    fields: dict[str, object] = {
        "experiment_id": "exp-1",
        "environment": EnvironmentFingerprint(
            python_version="3.11.15",
            platform="linux",
            dependency_lock_hash=LOCK,
            host_class="ci",
        ),
        "reproduction_command": command,
        "reproduction_inputs": inputs,
    }
    fields.update(overrides)
    return base_manifest(**fields)


def bundle(root: Path, **overrides: object) -> tuple[Path, ExperimentManifest]:
    """A manifest on disk with its inputs directory beside it, the way the runner writes one."""
    written = manifest(**overrides)
    path = root / f"H-2026-001-v1-{written.manifest_hash[:16]}.json"
    write_experiment_manifest(path, written)
    inputs = root / f"{written.experiment_id}.inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    for recorded in written.reproduction_inputs:
        (inputs / recorded.name).write_bytes(INPUT_BYTES)
    return path, written


def test_a_bundle_with_every_input_intact_is_attemptable(tmp_path: Path) -> None:
    """The clean case, and the assertion that keeps it honest: nothing ran."""
    path, written = bundle(tmp_path)

    readiness = assess_reproducibility(path, written, current_lock_hash=LOCK)

    assert readiness.can_attempt
    assert readiness.blocking == ()
    assert [check.name for check in readiness.inputs] == ["strategy.py"]
    assert all(check.intact for check in readiness.inputs)
    # The whole point of the field. A clean report must not read as a completed reproduction.
    assert readiness.performed is False
    assert readiness.outstanding == OUTSTANDING
    assert "no rerun performed" in readiness.explain()


def test_a_missing_input_blocks_rather_than_being_fetched(tmp_path: Path) -> None:
    """#48's fail-closed rule: a snapshot id is only useful if the bytes are still retrievable."""
    path, written = bundle(tmp_path)
    (tmp_path / f"{written.experiment_id}.inputs" / "strategy.py").unlink()

    readiness = assess_reproducibility(path, written, current_lock_hash=LOCK)

    assert not readiness.can_attempt
    assert any("no longer beside the bundle" in item for item in readiness.blocking)
    assert not readiness.inputs[0].present


def test_an_edited_input_blocks_even_though_the_file_is_there(tmp_path: Path) -> None:
    """Presence is not integrity, and the accidental-local-state failure looks like presence.

    The hash is compared against the bytes the runner wrote, recorded in the manifest at run
    time. Checking only that a file exists would pass for a directory somebody has since edited,
    which is precisely the state that makes a false edge survive a rerun.
    """
    path, written = bundle(tmp_path)
    (tmp_path / f"{written.experiment_id}.inputs" / "strategy.py").write_bytes(b"print('other')")

    readiness = assess_reproducibility(path, written, current_lock_hash=LOCK)

    assert not readiness.can_attempt
    assert readiness.inputs[0].present, "the file is there"
    assert not readiness.inputs[0].intact, "and it is not what ran"
    assert any("drifted from the manifest" in item for item in readiness.blocking)


def test_a_dirty_original_without_a_preserved_patch_can_never_be_reproduced(
    tmp_path: Path,
) -> None:
    """`git_commit=<sha>, dirty=true` does not describe code that was never committed.

    #48 states this as a rule and it is the strongest false claim available in a bundle: a
    result asserting reproducibility from a working tree nobody kept. It blocks permanently
    rather than warning.
    """
    path, written = bundle(
        tmp_path,
        code=CodeProvenance(
            git_commit="abc1234",
            dirty=True,
            configuration_hash="cfg-hash",
            execution_path=ExecutionPath.PRODUCTION_ENGINE,
        ),
    )

    readiness = assess_reproducibility(path, written, current_lock_hash=LOCK)

    assert not readiness.can_attempt
    assert any("dirty tree" in item and "impossible" in item for item in readiness.blocking)

    # With the patch preserved it becomes drift rather than a wall: the code exists somewhere.
    patch = tmp_path / "working-tree.patch"
    patch.write_text("diff --git a/x b/x\n", encoding="utf-8")
    with_patch = assess_reproducibility(
        path, written, current_lock_hash=LOCK, preserved_patch=patch
    )
    assert with_patch.can_attempt
    assert any("dirty" in item for item in with_patch.drift)


def test_a_patch_path_that_does_not_exist_is_not_a_preserved_patch(tmp_path: Path) -> None:
    """Naming a patch is not keeping one, and the fail-open version of this is one `if` away."""
    path, written = bundle(
        tmp_path,
        code=CodeProvenance(
            git_commit="abc1234",
            dirty=True,
            configuration_hash="cfg-hash",
            execution_path=ExecutionPath.PRODUCTION_ENGINE,
        ),
    )

    readiness = assess_reproducibility(
        path, written, current_lock_hash=LOCK, preserved_patch=tmp_path / "absent.patch"
    )

    assert not readiness.can_attempt


def test_a_dependency_lock_difference_is_drift_and_never_silent(tmp_path: Path) -> None:
    """It does not block on its own -- a lock can move without touching what a result used.

    But it is reported. The failure #48 lists is "a dependency behaving differently despite the
    same high-level configuration", and a difference nobody is told about is how that survives.
    """
    path, written = bundle(tmp_path)

    readiness = assess_reproducibility(path, written, current_lock_hash="b" * 64)

    assert readiness.can_attempt, "a moved lock is not proof the result is unrecoverable"
    assert any("dependency lock differs" in item for item in readiness.drift)


def test_a_bundle_that_never_claimed_reproducibility_says_so(tmp_path: Path) -> None:
    """Absence of a command is a fact about the bundle, not a fault, and is reported as one."""
    path, written = bundle(tmp_path, reproduction_command=(), reproduction_inputs=())

    readiness = assess_reproducibility(path, written, current_lock_hash=LOCK)

    assert not readiness.can_attempt
    assert any("never claimed to be" in item for item in readiness.blocking)


def test_a_command_without_recorded_inputs_cannot_be_written_at_all() -> None:
    """The two halves are one claim, refused at construction rather than caught downstream.

    A command with no recorded inputs is what the runner used to write: an instruction to run
    something whose inputs nobody could check. Making the shape unrepresentable is the fix; a
    validator that merely warned would leave the old bundles' claim available.
    """
    with pytest.raises(ValueError, match="same claim"):
        manifest(reproduction_inputs=())
    with pytest.raises(ValueError, match="same claim"):
        manifest(reproduction_command=())


def test_the_bundle_is_found_by_reading_manifests_not_by_guessing_filenames(
    tmp_path: Path,
) -> None:
    """The filename is content-addressed on the manifest hash, which is not the experiment id.

    Deriving a path from the id would be a guess that happens to be wrong, and it would break
    the moment a directory is moved or a file renamed.
    """
    path, written = bundle(tmp_path)
    (tmp_path / "not-a-manifest.json").write_text("{]", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("unrelated", encoding="utf-8")

    found_path, found = find_manifest(tmp_path, "exp-1")

    assert found_path == path
    assert found.manifest_hash == written.manifest_hash

    with pytest.raises(ManifestNotFound, match="exp-absent"):
        find_manifest(tmp_path, "exp-absent")


def test_the_command_runs_end_to_end_and_refuses_to_claim_a_reproduction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#48 asks that `reproduction_command` be populated only after the command is exercised.

    This is that exercise: the argv the manifest itself records, run through the real CLI, on a
    real bundle. It is also the assertion that the command does not overstate -- `ok` is about
    recoverable inputs, `reproduction_performed` is false, and the outstanding capabilities are
    listed by name so a clean result cannot be read as a reproduction.
    """
    from io import StringIO

    from quantbot.cli import main

    # The CLI opens a database for every command, and `QUANTBOT_DB_PATH` is read from the ambient
    # environment. Without this the test reaches into whatever store the operator's shell points
    # at -- a live one, on the machine this runs on.
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))

    path, written = bundle(tmp_path)
    assert written.reproduction_command[:2] == ("quantbot", "reproduce-experiment")

    output = StringIO()
    code = main(
        ["reproduce-experiment", "--experiment", "exp-1", "--manifests", str(tmp_path)],
        output=output,
    )
    payload = json.loads(output.getvalue())

    assert code == 0
    assert payload["ok"] is True
    assert payload["reproduction_performed"] is False
    assert payload["experiment_id"] == "exp-1"
    assert payload["manifest"] == str(path)
    assert payload["inputs"] == [{"name": "strategy.py", "present": True, "intact": True}]
    assert len(payload["outstanding_capabilities"]) == len(OUTSTANDING)
    assert "#48 remains open" in payload["note"]


def test_the_command_reports_a_missing_bundle_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator asking about an id that is not there needs an answer, not a traceback."""
    from io import StringIO

    from quantbot.cli import main

    monkeypatch.setenv("QUANTBOT_DB_PATH", str(tmp_path / "quantbot.db"))

    output = StringIO()
    code = main(
        ["reproduce-experiment", "--experiment", "nope", "--manifests", str(tmp_path)],
        output=output,
    )
    payload = json.loads(output.getvalue())

    # Both, not either. `code != 0 or ok is False` would pass on a command that exited zero
    # while reporting failure, or exited nonzero while claiming success -- and the point of this
    # test is that the two agree.
    assert code == 2
    assert payload["ok"] is False
    assert "nope" in payload["error"]
    assert payload["reproduction_performed"] is False
