"""Test-shell selection must honor the selected engine's actual policy."""

from types import SimpleNamespace

import pytest

from tests import windows_shell


@pytest.fixture(autouse=True)
def reset_cache():
    windows_shell.permitted_powershell.cache_clear()
    yield
    windows_shell.permitted_powershell.cache_clear()


@pytest.mark.parametrize("policy", ["Restricted", "AllSigned", "Bypass"])
def test_refused_policy_does_not_retry_another_engine(monkeypatch, policy):
    lookups = []
    queries = []

    def which(name):
        lookups.append(name)
        return "existing-pwsh" if name == "pwsh" else "other-engine"

    def query(command, **options):
        queries.append(command)
        return SimpleNamespace(stdout=policy)

    monkeypatch.setattr(windows_shell.shutil, "which", which)
    monkeypatch.setattr(windows_shell.subprocess, "run", query)
    with pytest.raises(RuntimeError, match="POLICY_REFUSES"):
        windows_shell.permitted_powershell()
    assert lookups == ["pwsh"]
    assert queries == [
        ["existing-pwsh", "-NoProfile", "-NonInteractive", "-Command", "Get-ExecutionPolicy"]
    ]


def test_missing_engine_is_a_prerequisite_failure(monkeypatch):
    monkeypatch.setattr(windows_shell.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="UNAVAILABLE"):
        windows_shell.permitted_powershell()


def test_existing_allowed_engine_is_selected_without_override(monkeypatch):
    monkeypatch.setattr(windows_shell.shutil, "which", lambda name: "existing-pwsh")
    queries = []

    def query(command, **options):
        queries.append(command)
        return SimpleNamespace(stdout="RemoteSigned\n")

    monkeypatch.setattr(windows_shell.subprocess, "run", query)
    assert windows_shell.permitted_powershell() == "existing-pwsh"
    assert windows_shell.permitted_powershell() == "existing-pwsh"
    assert len(queries) == 1
    assert queries[0][-1] == "Get-ExecutionPolicy"
