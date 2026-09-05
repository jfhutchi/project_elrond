"""Local checkout evidence. Never fetch, change branches, or trust the caller's cwd."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

UNKNOWN = "UNKNOWN"


def commit_id(value: str | None) -> str:
    """Only full object IDs are deploy pins; arbitrary environment text is not output."""
    return value.lower() if value and re.fullmatch(r"[0-9a-fA-F]{40}", value) else UNKNOWN


def git_read(repo: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def checkout_evidence(repo: Path, intended: str | None = None) -> dict[str, object]:
    expected = (
        commit_id(intended)
        if intended is not None
        else commit_id(git_read(repo, "rev-parse", "--verify", "origin/main^{commit}"))
    )
    head = commit_id(git_read(repo, "rev-parse", "--verify", "HEAD^{commit}"))
    dirty = git_read(repo, "status", "--porcelain", "--untracked-files=normal")
    return {
        "repo_path": str(repo.resolve()),
        "head": head,
        "branch": git_read(repo, "branch", "--show-current") or UNKNOWN,
        "dirty": UNKNOWN if dirty is None else bool(dirty),
        "intended_commit": expected,
        "intended_source": "explicit"
        if intended is not None
        else "local origin/main (not fetched)",
        "checkout_drift": compare_commit(head, expected, repo),
    }


def compare_commit(recorded: str, intended: str, repo: Path) -> str:
    recorded, intended = commit_id(recorded), commit_id(intended)
    if UNKNOWN in (recorded, intended):
        return UNKNOWN
    if recorded == intended:
        return "MATCH"
    if git_read(repo, "cat-file", "-t", recorded) != "commit":
        return "MISMATCH"
    if git_read(repo, "cat-file", "-t", intended) != "commit":
        return "MISMATCH"
    ancestor = git_read(repo, "merge-base", recorded, intended)
    if ancestor == recorded:
        return "BEHIND"
    if ancestor == intended:
        return "AHEAD"
    return "DIVERGED" if ancestor else "MISMATCH"


def running_checkout_commit() -> str:
    """Identify the installed source tree, never an unrelated current working directory.

    Called once at Runtime construction. A wheel needs an explicit full SHA pin;
    missing/invalid pins remain UNKNOWN. A source checkout takes precedence over a pin.
    """
    root = Path(__file__).resolve().parents[3]
    if not (root / ".git").exists():
        return commit_id(os.environ.get("QUANTBOT_GIT_COMMIT"))
    return commit_id(git_read(root, "rev-parse", "--verify", "HEAD^{commit}"))
