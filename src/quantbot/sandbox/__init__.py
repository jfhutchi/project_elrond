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

__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "FORBIDDEN_HOSTS",
    "Artifact",
    "SandboxError",
    "SandboxPaths",
    "SandboxPolicy",
    "SandboxResult",
    "SandboxRunner",
    "StaticCheckError",
    "StaticViolation",
    "enforce",
    "scan",
]
