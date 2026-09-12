"""Isolation for agent-generated research code.

Tier-1 foundation issue #12. Nothing downstream that generates or executes code is safe without
this, so it lands first.

The boundary is built from absence of capability rather than denial of use: the child cannot
import `quantbot`, its environment is constructed from empty, and research data arrives as files.
See `policy.py` for what is hard-enforced on this platform and what is not.
"""

from quantbot.sandbox.policy import (
    DEFAULT_ALLOWED_HOSTS,
    FORBIDDEN_HOSTS,
    SandboxPaths,
    SandboxPolicy,
)
from quantbot.sandbox.runner import Artifact, SandboxError, SandboxResult, SandboxRunner
from quantbot.sandbox.static import (
    StaticCheckError,
    StaticViolation,
    enforce,
    scan,
)
from quantbot.sandbox.windows import WindowsSandbox, WindowsUnavailable, windows_backend_available
from quantbot.sandbox.wsl import WslSandbox, WslUnavailable, wsl_backend_available


class NoIsolatedBackend(SandboxError):
    """Raised when this host cannot provide OS-enforced isolation at all.

    Distinct from a backend being unavailable: a caller can react to one missing backend by trying
    another, and there is nothing to react to when every backend is gone.
    """


def select_isolated_backend(
    policy: SandboxPolicy | None = None,
) -> WslSandbox | WindowsSandbox:
    """Return an OS-enforced backend for this host, or raise.

    This exists because `mine_for_anomalies` and `codegen` gate on `os_enforced`, and the answer
    to "which backend satisfies that here" was previously a thing each caller guessed. It is now
    one measured decision in one place.

    **It never returns `SandboxRunner`.** That backend is not OS-enforced, and a selector that
    silently degraded to it would defeat the exact check its callers wrote. A host with no
    isolation gets an exception, not a weaker sandbox under the stronger name. `SandboxRunner`
    remains available by direct construction to callers that do not need the guarantee and say so.

    WSL is tried first, not because it is better -- the two are contract-equivalent, and
    `docs/sandbox-contract.md` records where they differ -- but because a host with a working
    distribution has already paid for it, and preferring it keeps behaviour unchanged on every
    machine where the WSL backend was already the one running.
    """
    if wsl_backend_available():
        return WslSandbox(policy)
    if windows_backend_available():
        return WindowsSandbox(policy)
    raise NoIsolatedBackend(
        "no OS-enforced sandbox backend is available on this host: WSL has no usable distribution "
        "and the Windows AppContainer backend could not be established. Generated research code "
        "cannot execute without one, and SandboxRunner is deliberately not a fallback"
    )


__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "FORBIDDEN_HOSTS",
    "Artifact",
    "NoIsolatedBackend",
    "SandboxError",
    "SandboxPaths",
    "SandboxPolicy",
    "SandboxResult",
    "SandboxRunner",
    "StaticCheckError",
    "StaticViolation",
    "WindowsSandbox",
    "WindowsUnavailable",
    "WslSandbox",
    "WslUnavailable",
    "enforce",
    "scan",
    "select_isolated_backend",
    "windows_backend_available",
    "wsl_backend_available",
]
