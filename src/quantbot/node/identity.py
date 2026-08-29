"""Who this node is, and where its mutable state lives (#38).

Two things every other part of the node system needs before anything else exists: a stable
identifier that survives reboots, and a directory that is not the repository working tree.

**The identifier is a generated UUID, not a hardware fingerprint.** MAC addresses change with a
docking station, hostnames change when somebody renames a laptop, and disk serials tie an
identity to hardware that may be replaced under it. A UUID written once and read thereafter is
stable for the reason that matters -- nothing about the machine can invalidate it -- and it
carries no information about the operator's hardware into any record that leaves the node.

**Losing the file means a new node, and that is the honest answer.** A node whose identity file
is gone cannot prove it is the node that registered earlier. Silently re-deriving the same ID
from the hostname would let a rebuilt machine inherit another machine's history, including its
forward trading observations.

## Why state lives outside the repository

The defaults in `.env.example` are relative paths -- `quantbot.db`, `reports`, `quantbot.lock`
-- which resolve against the working directory. That is workable for one checkout on one
machine and wrong the moment there are two: the ledger follows whichever directory the process
happened to start in, and a `git clean` is a short walk from the durable store.

`QUANTBOT_STATE_DIR` names the volume that holds mutable state. Every existing environment
variable keeps priority when it is set, so this changes nothing for an installation that
already works; it supplies a coherent default for one that does not exist yet.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from quantbot.node.policy import NodeRole

#: Names the directory holding databases, logs, datasets and the node identity.
STATE_DIR_ENV = "QUANTBOT_STATE_DIR"

#: The identity file. Small, human-readable, and safe to inspect: it holds no credential.
NODE_FILE = "node.json"

_SCHEMA_VERSION = 1


class NodeIdentityError(RuntimeError):
    """Raised when the identity file exists but cannot be trusted."""


@dataclass(frozen=True, slots=True)
class StateLayout:
    """Where each kind of mutable state belongs under the state directory.

    Reconciled with what this repository already writes rather than imposed on it. `reports`
    keeps its existing name because `QUANTBOT_REPORTS_DIR` and the weekly report writer already
    use it; renaming it to `exports` would break a working convention to satisfy a preference.
    There is no `docker` directory because nothing in this project is containerised, and
    creating one would advertise a capability that does not exist.
    """

    root: Path

    @property
    def databases(self) -> Path:
        """The durable ledger. Its own directory so a backup can copy one thing."""
        return self.root / "databases"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def datasets(self) -> Path:
        """Historical market data. The largest and most re-downloadable thing here."""
        return self.root / "datasets"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def telemetry(self) -> Path:
        return self.root / "telemetry"

    @property
    def run(self) -> Path:
        """Lock files and the identity file: small, node-local, never synced or backed up."""
        return self.root / "run"

    def all_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.databases,
            self.logs,
            self.reports,
            self.datasets,
            self.models,
            self.telemetry,
            self.run,
        )

    def create(self) -> tuple[Path, ...]:
        """Create every directory that is missing. Idempotent, and never removes anything."""
        created: list[Path] = []
        for directory in self.all_directories():
            if not directory.exists():
                directory.mkdir(parents=True, exist_ok=True)
                created.append(directory)
        return tuple(created)


def default_state_dir() -> Path:
    """The platform-appropriate location, used when `QUANTBOT_STATE_DIR` is unset.

    Conservative on purpose. An installer may point this at a large data volume -- which is the
    right answer on a machine with a second disk -- but a default that assumed one would fail
    on every machine that has only C:.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "quantbot"
        return Path.home() / "AppData" / "Local" / "quantbot"
    else:
        # `else` rather than a fallthrough: mypy narrows `sys.platform`, so anything after a
        # branch that always returns reads as unreachable instead of as the other platform.
        xdg = os.environ.get("XDG_STATE_HOME")
        if xdg:
            return Path(xdg) / "quantbot"
        return Path.home() / ".local" / "state" / "quantbot"


def resolve_state_dir(override: Path | str | None = None) -> Path:
    """An explicit argument wins, then the environment, then the platform default."""
    if override is not None:
        return Path(override).expanduser()
    configured = os.environ.get(STATE_DIR_ENV)
    if configured and configured.strip():
        return Path(configured.strip()).expanduser()
    return default_state_dir()


@dataclass(frozen=True, slots=True)
class NodeIdentity:
    """A node's durable identity and its assigned role.

    `role` is configuration, not a measurement. Nothing here consults the hardware: a node is
    what it has been told to be, and `quantbot.node.policy.role_is_supportable` is the separate
    question of whether the machine can carry it.
    """

    node_id: str
    role: NodeRole
    created_at: datetime
    #: The hostname when the identity was created, kept as a human label only. A node that has
    #: been renamed keeps its identity; this field going stale is expected, not a fault.
    hostname_at_creation: str

    def summary(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "created_at": self.created_at.isoformat(),
            "hostname_at_creation": self.hostname_at_creation,
        }


def identity_path(state_dir: Path) -> Path:
    return StateLayout(state_dir).run / NODE_FILE


def load_identity(state_dir: Path) -> NodeIdentity | None:
    """Read the identity, or `None` when this node has never been initialised.

    A malformed file raises rather than being replaced. Overwriting it would mint a new node ID
    and silently orphan whatever the old one registered, which is precisely the history loss
    the generated-identifier design exists to make visible.
    """
    path = identity_path(state_dir)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise NodeIdentityError(
            f"{path} exists but could not be read ({type(error).__name__}). Refusing to replace "
            "it: a new identity would orphan whatever this node previously registered."
        ) from error
    if not isinstance(payload, dict):
        raise NodeIdentityError(f"{path} does not contain an object")
    try:
        return NodeIdentity(
            node_id=str(payload["node_id"]),
            role=NodeRole(payload["role"]),
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            hostname_at_creation=str(payload.get("hostname_at_creation", "")),
        )
    except (KeyError, ValueError) as error:
        raise NodeIdentityError(f"{path} is not a valid identity: {error}") from error


def ensure_identity(
    state_dir: Path,
    *,
    role: NodeRole,
    hostname: str,
    now: datetime | None = None,
) -> tuple[NodeIdentity, bool]:
    """Load this node's identity, creating one if it has none.

    Returns the identity and whether it was created by this call, so an installer can tell
    "registered a new node" from "confirmed an existing one" without inspecting the filesystem
    itself.

    **The role argument is ignored when an identity already exists.** Re-running the installer
    must not silently repurpose a node -- a machine quietly changing from coordinator to worker
    would strand the ledger it was holding. Changing a role is `assign_role`, which is explicit.
    """
    existing = load_identity(state_dir)
    if existing is not None:
        return existing, False

    layout = StateLayout(state_dir)
    layout.run.mkdir(parents=True, exist_ok=True)
    identity = NodeIdentity(
        node_id=str(uuid.uuid4()),
        role=role,
        created_at=now or datetime.now(UTC),
        hostname_at_creation=hostname,
    )
    _write_identity(layout, identity, exclusive=True)
    # A concurrent installer may have won the exclusive create. Whoever wrote first owns the
    # identity; returning theirs is correct and returning ours would leave two IDs in play.
    reloaded = load_identity(state_dir)
    if reloaded is None:  # pragma: no cover - only if the file vanished mid-call
        return identity, True
    return reloaded, reloaded.node_id == identity.node_id


def assign_role(state_dir: Path, role: NodeRole) -> NodeIdentity:
    """Change this node's role, preserving its identity.

    The node ID survives deliberately: a machine moving from worker to coordinator is the same
    node with a different job, and reissuing its ID would detach it from its own history.
    """
    existing = load_identity(state_dir)
    if existing is None:
        raise NodeIdentityError(
            f"no node identity under {state_dir}; initialise the node before assigning a role"
        )
    updated = NodeIdentity(
        node_id=existing.node_id,
        role=role,
        created_at=existing.created_at,
        hostname_at_creation=existing.hostname_at_creation,
    )
    _write_identity(StateLayout(state_dir), updated, exclusive=False)
    return updated


def _write_identity(layout: StateLayout, identity: NodeIdentity, *, exclusive: bool) -> None:
    """Write the identity file, atomically where the platform allows it.

    `exclusive` guards first creation: two installers racing must not both mint an ID, and the
    loser reads the winner's file rather than overwriting it.
    """
    layout.run.mkdir(parents=True, exist_ok=True)
    path = layout.run / NODE_FILE
    payload = json.dumps(
        {"schema_version": _SCHEMA_VERSION, **identity.summary()}, indent=2, sort_keys=True
    )
    if exclusive:
        try:
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(payload + "\n")
        except FileExistsError:
            return
        return
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload + "\n", encoding="utf-8")
    os.replace(temporary, path)


__all__ = [
    "NODE_FILE",
    "STATE_DIR_ENV",
    "NodeIdentity",
    "NodeIdentityError",
    "StateLayout",
    "assign_role",
    "default_state_dir",
    "ensure_identity",
    "identity_path",
    "load_identity",
    "resolve_state_dir",
]
