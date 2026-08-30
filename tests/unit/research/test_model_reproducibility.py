"""A bundle that consumed model completions cannot claim to be re-executable (#18, #48).

`assess_reproducibility` checked recorded inputs, a dirty tree, the dependency lock and the
manifest invariants. It said nothing about model calls, so an experiment whose result depended on
an LLM reported **zero blockers** -- a clean reproducibility verdict for a bundle nobody can
reproduce.

That was not a wrong check. It was a missing variable, and the measurement recorded in
`REFUTED.md` is what shows it matters: at `temperature=0`, with model, version, endpoint, prompt
hash and parameters all identical, a cold call and a primed call return analyses whose claims
differ by 26-37%. Nothing in a manifest distinguishes those two runs.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from quantbot.research.manifest import EnvironmentFingerprint, ReproductionInput
from quantbot.research.reproduce import assess_reproducibility
from tests.unit.research.test_reproduce import INPUT_BYTES
from tests.unit.research.test_reproducibility import exploratory as exploratory_manifest
from tests.unit.research.test_reproducibility import manifest as confirmatory_manifest


def bundle(tmp_path: Path, *, confirmatory: bool, **overrides: object):
    """Assess one bundle, reusing the existing manifest fixtures rather than rebuilding them.

    A rebuilt manifest fixture drifts from the model it describes and then asserts things about
    a shape production no longer produces. `test_reproducibility` already maintains both a
    confirmatory and an exploratory bundle that satisfy every unrelated validator, so these tests
    are about the model-call check and nothing else.
    """
    build = confirmatory_manifest if confirmatory else exploratory_manifest
    fields: dict[str, object] = {
        "environment": EnvironmentFingerprint(
            python_version="3.11.15",
            platform="linux",
            dependency_lock_hash="a" * 64,
            host_class="ci",
        ),
        "reproduction_command": ("quantbot", "reproduce-experiment", "--experiment", "exp-1"),
        "reproduction_inputs": (
            ReproductionInput(
                name="strategy.py",
                content_hash=hashlib.sha256(INPUT_BYTES).hexdigest(),
                bytes=len(INPUT_BYTES),
            ),
        ),
    }
    fields.update(overrides)
    written = build(**fields)

    path = tmp_path / f"{written.experiment_id}.json"
    inputs = tmp_path / f"{written.experiment_id}.inputs"
    inputs.mkdir(exist_ok=True)
    (inputs / "strategy.py").write_bytes(INPUT_BYTES)
    path.write_text("{}", encoding="utf-8")
    return assess_reproducibility(path, written)


def confirmatory(tmp_path: Path, **overrides: object):
    return bundle(tmp_path, confirmatory=True, **overrides)


def exploratory(tmp_path: Path, **overrides: object):
    return bundle(tmp_path, confirmatory=False, **overrides)


def model_notes(report) -> list[str]:
    return [item for item in [*report.blocking, *report.drift] if "model completion" in item]


def test_a_bundle_declaring_no_model_calls_is_unaffected(tmp_path: Path) -> None:
    """The overwhelming majority of experiments here are deterministic and must stay clean."""
    report = confirmatory(tmp_path)

    assert not model_notes(report)

def test_a_confirmatory_bundle_that_used_a_model_cannot_be_reproduced(tmp_path: Path) -> None:
    """The case this closes.

    A confirmatory result is cited as evidence. Resting one on a component whose answer depends
    on a model's preceding call history -- which no manifest records and no rerun restores --
    makes the reproduction claim false, so it blocks rather than warns.
    """
    report = confirmatory(tmp_path, model_calls=("mistral:7b@0", "mistral:7b@1"))

    offending = [item for item in report.blocking if "model completion" in item]
    assert len(offending) == 1
    assert "2 model completion" in offending[0]
    assert "mistral:7b@0" in offending[0]
    assert "already served" in offending[0], "the reason, not just the verdict"

def test_an_exploratory_bundle_is_warned_rather_than_blocked(tmp_path: Path) -> None:
    """A model may legitimately have suggested the question while the evaluation stayed
    deterministic. Blocking that would refuse a bundle whose numbers are entirely re-derivable.

    Which side a bundle falls on is not inferable from the manifest -- nothing in it says whether
    a model sat in the path that produced the result -- so the declared mode is what decides.
    """
    report = exploratory(tmp_path, model_calls=("mistral:7b@0",))

    assert not [item for item in report.blocking if "model completion" in item]
    assert [item for item in report.drift if "model completion" in item]

def test_the_absence_of_declared_calls_is_never_read_as_a_guarantee(tmp_path: Path) -> None:
    """An empty tuple means the bundle declares no model calls, not that none happened.

    So the check keys on presence and never on absence. A rule that treated an empty tuple as
    proof of determinism would hand a clean verdict to exactly the bundles that forgot to record
    what they used -- turning an omission into a credential.
    """
    clean = exploratory(tmp_path)
    declared = exploratory(tmp_path, model_calls=("mistral:7b@0",))

    assert not model_notes(clean)
    assert len(declared.drift) > len(clean.drift), "presence adds; absence asserts nothing"

def test_many_calls_are_summarised_rather_than_listed_in_full(tmp_path: Path) -> None:
    """A readiness report a person will not read is a report that does not prevent anything."""
    report = exploratory(
        tmp_path, model_calls=tuple(f"mistral:7b@{index}" for index in range(40))
    )

    detail = next(item for item in report.drift if "model completion" in item)
    assert "40 model completion" in detail
    assert detail.count("mistral:7b@") == 3
    assert "\u2026" in detail

def test_the_field_defaults_to_empty_so_existing_bundles_still_load() -> None:
    """Every manifest already written predates this field, and none of them are wrong."""
    assert confirmatory_manifest().model_calls == ()
    assert exploratory_manifest().model_calls == ()


@pytest.mark.parametrize("confirmatory_bundle", [True, False])
def test_the_verdict_names_the_models_either_way(
    tmp_path: Path, confirmatory_bundle: bool
) -> None:
    """Whichever side it lands on, a reader has to be able to tell *what* cannot be reproduced."""
    report = bundle(
        tmp_path, confirmatory=confirmatory_bundle, model_calls=("llama3.1:8b@7",)
    )

    assert any("llama3.1:8b@7" in item for item in model_notes(report))
