"""Which backend a research caller gets, and what it must never be handed (#21).

These run on every platform, including the Linux CI runner where neither backend exists. That is
the point: the rule under test is not "Windows picks Windows", it is that a host with no isolation
gets an exception rather than a quieter sandbox. A selector that degraded to `SandboxRunner` would
defeat the `os_enforced` check `mine_for_anomalies` and `codegen` both wrote deliberately, and it
would do it silently.
"""

from __future__ import annotations

import sys

import pytest

import quantbot.sandbox as sandbox
from quantbot.sandbox import (
    NoIsolatedBackend,
    SandboxPolicy,
    SandboxRunner,
    WindowsSandbox,
    WindowsUnavailable,
    WslSandbox,
    WslUnavailable,
    select_isolated_backend,
)


@pytest.fixture
def availability(monkeypatch: pytest.MonkeyPatch):
    """Drive the selector's two probes without needing either backend on the test host."""

    def _set(*, wsl: bool, windows: bool) -> None:
        monkeypatch.setattr(sandbox, "wsl_backend_available", lambda: wsl)
        monkeypatch.setattr(sandbox, "windows_backend_available", lambda: windows)

    return _set


def test_no_isolated_backend_raises_rather_than_degrading(availability) -> None:
    availability(wsl=False, windows=False)

    with pytest.raises(NoIsolatedBackend) as raised:
        select_isolated_backend()

    # The message has to say why there is no fallback, because the next person to hit this will
    # be tempted to add one.
    assert "SandboxRunner is deliberately not a fallback" in str(raised.value)


def test_wsl_is_preferred_when_both_are_available(availability) -> None:
    """Unchanged behaviour on every host that already had a working distribution."""
    availability(wsl=True, windows=True)

    assert isinstance(select_isolated_backend(), WslSandbox)


def test_the_windows_backend_is_chosen_when_wsl_is_absent(availability) -> None:
    availability(wsl=False, windows=True)

    if sys.platform != "win32":
        # Off Windows the backend refuses to construct at all, which is still not a degradation.
        with pytest.raises(WindowsUnavailable):
            select_isolated_backend()
        return

    assert isinstance(select_isolated_backend(), WindowsSandbox)


@pytest.mark.parametrize(
    ("wsl", "windows"),
    [(False, False), (False, True), (True, False), (True, True)],
)
def test_the_selector_never_returns_an_unenforced_backend(availability, wsl, windows) -> None:
    """The invariant, across every combination of what a host happens to have.

    Asserted as `os_enforced is True` rather than as a type check, because a future backend should
    have to satisfy the property rather than be added to a list here.
    """
    availability(wsl=wsl, windows=windows)

    try:
        selected = select_isolated_backend()
    except (NoIsolatedBackend, WindowsUnavailable, WslUnavailable):
        return  # refusing is always an acceptable answer; returning the wrong thing is not

    assert selected.os_enforced is True
    assert not isinstance(selected, SandboxRunner)


def test_the_unenforced_runner_still_says_so() -> None:
    """`SandboxRunner` must keep reporting False, or every gate above becomes decoration."""
    assert SandboxRunner.os_enforced is False
    assert SandboxRunner().os_enforced is False


def test_both_os_enforced_backends_refuse_a_network_enabled_policy() -> None:
    """Neither can express the policy's allowlist, so both fail closed rather than opening egress.

    Runs everywhere: both refusals happen in the constructor, before either backend touches the
    host, so this holds on a machine that has neither WSL nor an AppContainer.
    """
    policy = SandboxPolicy(wall_clock_seconds=30.0, network_enabled=True)

    with pytest.raises(WslUnavailable, match="does not offer network access"):
        WslSandbox(policy)

    with pytest.raises(WindowsUnavailable, match="does not offer network access"):
        WindowsSandbox(policy)


def test_the_wsl_backend_still_declares_itself_os_enforced() -> None:
    """Guards the contract this work was not allowed to change while adding a second backend."""
    assert WslSandbox.os_enforced is True
    assert WindowsSandbox.os_enforced is True


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal under test is the non-Windows one")
def test_the_windows_backend_refuses_to_construct_off_windows() -> None:
    with pytest.raises(WindowsUnavailable, match="requires Windows"):
        WindowsSandbox()
