"""Composition checks for the independent position-protection process (#157)."""

from __future__ import annotations

import inspect
from pathlib import Path

import quantbot.runtime as runtime


def test_protection_runtime_uses_a_distinct_liveness_lock(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("QUANTBOT_LOCK_PATH", str(tmp_path / "quantbot.lock"))

    paths = runtime.runtime_paths()

    assert paths.protection_lock == tmp_path / "quantbot.protection.lock"
    assert len({paths.lock, paths.daemon_lock, paths.protection_lock}) == 3


def test_protection_command_is_wired_to_the_independent_runner() -> None:
    source = inspect.getsource(runtime._protection_daemon)

    assert "runtime.protector()" in source
    assert "PositionProtectionDaemonRunner(" in source
    assert "lock_path=runtime.paths.protection_lock" in source


def test_runtime_protector_uses_only_the_authoritative_execution_boundary() -> None:
    source = inspect.getsource(runtime.Runtime.protector)

    for forbidden in (
        "self.strategy",
        "self.research",
        "self.model",
        "self.broker.submit_order",
    ):
        assert forbidden not in source
    assert "submitter=self.coordinator" in source
