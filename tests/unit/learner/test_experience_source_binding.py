"""New learning and authority modules must be covered by execution source hashes."""

from importlib import import_module
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "module,name,file",
    [
        ("experiment_paper", "pooled_code_hash", "experiment_switch.py"),
        ("experiment_paper", "pooled_code_hash", "experience_continuity.py"),
        ("exploration_paper", "exploration_code_hash", "experience_consideration.py"),
    ],
)
def test_execution_binding_detects_new_source_drift(monkeypatch, module, name, file):
    code_hash = getattr(import_module("quantbot.learner." + module), name)
    before = code_hash()
    original = Path.read_bytes
    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda path: b"synthetic source drift" if path.name == file else original(path),
    )
    assert code_hash() != before
