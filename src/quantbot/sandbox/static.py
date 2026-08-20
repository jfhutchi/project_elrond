"""Reject obviously-unsafe generated code before running it.

This is defence in depth and is documented as such. An AST scan cannot stop a determined
adversary — `getattr(__import__("o"+"s"), "environ")` defeats any name-based check, and `ctypes`
defeats everything. The primary controls are in `policy.py`: the project is not importable, the
environment is empty, and data arrives as files.

What this layer is genuinely good at is catching the far more likely case: generated code that
reaches for a credential or a broker path because that is what the training data does, with no
adversarial intent at all. Failing that fast, with a readable reason, is worth more than the
theoretical bypass costs.

Scans reject rather than sanitise. Rewriting generated code to make it safe would produce a
program the author did not write and nobody reviewed.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

#: Import roots that have no legitimate use inside a research experiment. `quantbot` is listed
#: even though it is already off `sys.path`, so the failure is a clear message instead of an
#: ImportError forty lines into a traceback.
FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "quantbot",  # broker adapters, runtime composition root, order submission
        "dotenv",  # would load .env straight off disk
        "ctypes",  # defeats every in-process guard
        "subprocess",  # a child process inherits nothing useful but escapes the monitor
        "multiprocessing",
        "socketserver",
        "http",
        "ftplib",
        "smtplib",
        "telnetlib",
        "webbrowser",
        "pty",
        "winreg",
    }
)

#: Attribute paths that read the environment or shell out. Checked as dotted suffixes so
#: `os.environ`, `os.getenv` and `os.system` are caught however `os` was aliased at import.
FORBIDDEN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        "environ",
        "getenv",
        "putenv",
        "system",
        "popen",
        "execv",
        "execve",
        "spawnv",
        "fork",
        "setuid",
    }
)

#: Builtins that turn a static scan into a formality.
FORBIDDEN_CALLS: frozenset[str] = frozenset({"eval", "exec", "compile", "__import__", "breakpoint"})

#: Substrings that indicate an attempt at the trading host or the credential file, wherever they
#: appear as literals. Cheap, and catches copy-pasted endpoints.
FORBIDDEN_LITERALS: tuple[str, ...] = (
    "paper-api.alpaca.markets",
    "api.alpaca.markets",
    "broker-api.alpaca.markets",
    "ALPACA_PAPER_API_KEY",
    "ALPACA_PAPER_API_SECRET",
    "ALPACA_LIVE_API_KEY",
    "ALPACA_LIVE_API_SECRET",
    ".env",
)


@dataclass(frozen=True, slots=True)
class StaticViolation:
    """One reason a script was refused. Carries a line so the author can find it."""

    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        return f"line {self.line}: {self.kind} — {self.detail}"


class StaticCheckError(Exception):
    """Raised when generated code is refused before execution."""

    def __init__(self, violations: list[StaticViolation]) -> None:
        self.violations = violations
        joined = "; ".join(str(v) for v in violations)
        super().__init__(f"generated code refused by static policy: {joined}")


def _root_module(name: str) -> str:
    return name.split(".", 1)[0]


def scan(source: str, *, allowed_third_party: tuple[str, ...] = ()) -> list[StaticViolation]:
    """Return every violation in `source`. Empty means the scan found nothing to object to.

    Returns rather than raises so a caller can log all problems at once; generated code tends to
    contain several and fixing them one round-trip at a time is wasteful.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return [StaticViolation(error.lineno or 0, "syntax", str(error.msg))]

    violations: list[StaticViolation] = []
    allowed = set(allowed_third_party)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in FORBIDDEN_IMPORTS:
                    violations.append(
                        StaticViolation(node.lineno, "forbidden-import", alias.name)
                    )
        elif isinstance(node, ast.ImportFrom):
            root = _root_module(node.module or "")
            if root in FORBIDDEN_IMPORTS:
                violations.append(
                    StaticViolation(node.lineno, "forbidden-import", node.module or "?")
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_ATTRIBUTES:
                violations.append(
                    StaticViolation(node.lineno, "forbidden-attribute", node.attr)
                )
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_CALLS:
                violations.append(StaticViolation(node.lineno, "forbidden-call", node.id))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            for needle in FORBIDDEN_LITERALS:
                if needle.lower() in lowered:
                    violations.append(
                        StaticViolation(node.lineno, "forbidden-literal", needle)
                    )

    # Unknown third-party imports are reported so the policy stays explicit about what the
    # sandbox actually ships. Standard-library imports are allowed without enumeration.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Import | ast.ImportFrom):
            continue
        if isinstance(node, ast.Import):
            names = [_root_module(a.name) for a in node.names]
        elif node.level == 0:
            names = [_root_module(node.module or "")]
        else:
            continue  # a relative import cannot reach a third-party package
        for name in names:
            if name and name not in allowed and name in _KNOWN_THIRD_PARTY:
                violations.append(
                    StaticViolation(node.lineno, "unapproved-dependency", name)
                )

    return violations


#: Third-party roots worth naming so an unapproved one is a clear refusal rather than a runtime
#: ImportError. Not exhaustive by design: anything not installed fails at import anyway.
_KNOWN_THIRD_PARTY: frozenset[str] = frozenset(
    {"httpx", "requests", "urllib3", "aiohttp", "boto3", "paramiko", "pydantic", "sqlalchemy"}
)


def enforce(source: str, *, allowed_third_party: tuple[str, ...] = ()) -> None:
    """Raise `StaticCheckError` if `source` violates policy. Never modifies the source."""
    violations = scan(source, allowed_third_party=allowed_third_party)
    if violations:
        raise StaticCheckError(violations)
