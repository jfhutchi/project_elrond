"""A node's identity survives re-installation and never silently changes (#38)."""

from __future__ import annotations

from pathlib import Path

import pytest

from quantbot.node.identity import (
    STATE_DIR_ENV,
    NodeIdentityError,
    StateLayout,
    assign_role,
    default_state_dir,
    ensure_identity,
    identity_path,
    load_identity,
    resolve_state_dir,
)
from quantbot.node.policy import NodeRole


def test_a_second_install_confirms_the_node_rather_than_reissuing_it(tmp_path: Path) -> None:
    """Idempotency, asserted on the identifier rather than on the call not raising.

    Running the installer twice must be safe. A second run that minted a new ID would orphan
    everything the node had registered, and the process would look successful while doing it.
    """
    first, created_first = ensure_identity(tmp_path, role=NodeRole.WORKER, hostname="omen")
    second, created_second = ensure_identity(tmp_path, role=NodeRole.WORKER, hostname="omen")

    assert created_first is True
    assert created_second is False
    assert first.node_id == second.node_id
    assert first.created_at == second.created_at


def test_reinstalling_with_a_different_role_does_not_repurpose_the_node(tmp_path: Path) -> None:
    """The role argument is ignored once an identity exists.

    A coordinator quietly becoming a worker because someone re-ran the installer with a
    different flag would strand the ledger it was holding. Changing a role is `assign_role`,
    which is explicit and preserves the identifier.
    """
    ensure_identity(tmp_path, role=NodeRole.COORDINATOR, hostname="omen")

    reloaded, created = ensure_identity(tmp_path, role=NodeRole.WATCHDOG, hostname="omen")

    assert created is False
    assert reloaded.role is NodeRole.COORDINATOR, "an install must not reassign a live role"


def test_assigning_a_role_keeps_the_identifier(tmp_path: Path) -> None:
    """A machine changing jobs is the same node, and detaching it from its history is a loss."""
    original, _ = ensure_identity(tmp_path, role=NodeRole.WORKER, hostname="omen")

    updated = assign_role(tmp_path, NodeRole.COORDINATOR)

    assert updated.node_id == original.node_id
    assert updated.created_at == original.created_at
    assert updated.role is NodeRole.COORDINATOR
    assert load_identity(tmp_path) == updated, "the change is durable, not in-memory only"


def test_assigning_a_role_to_an_uninitialised_node_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NodeIdentityError, match="no node identity"):
        assign_role(tmp_path, NodeRole.WORKER)


def test_a_corrupt_identity_file_raises_and_is_left_untouched(tmp_path: Path) -> None:
    """Refusing to overwrite is the protection, so the assertion is that the bytes survive.

    A handler that replaced an unreadable file would mint a new ID and destroy the only record
    connecting this machine to what it registered. Failing loudly leaves the operator something
    to recover; failing quietly does not.
    """
    layout = StateLayout(tmp_path)
    layout.run.mkdir(parents=True)
    path = identity_path(tmp_path)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(NodeIdentityError, match="could not be read"):
        load_identity(tmp_path)
    with pytest.raises(NodeIdentityError):
        ensure_identity(tmp_path, role=NodeRole.WORKER, hostname="omen")

    assert path.read_text(encoding="utf-8") == "{not json", "the file must not be replaced"


def test_an_identity_naming_an_unknown_role_is_refused(tmp_path: Path) -> None:
    """A role this build does not have is a version mismatch, not something to guess at."""
    layout = StateLayout(tmp_path)
    layout.run.mkdir(parents=True)
    identity_path(tmp_path).write_text(
        '{"node_id": "abc", "role": "quantum-worker", "created_at": "2026-08-28T00:00:00+00:00"}',
        encoding="utf-8",
    )

    with pytest.raises(NodeIdentityError, match="not a valid identity"):
        load_identity(tmp_path)


def test_creating_the_layout_is_idempotent_and_additive(tmp_path: Path) -> None:
    """Re-running the installer must not disturb a populated state directory."""
    layout = StateLayout(tmp_path)

    # The root already exists here, as it does on a machine with a chosen data volume, so it
    # is correctly absent from what this call created.
    created = layout.create()
    assert set(created) == set(layout.all_directories()) - {layout.root}
    assert all(directory.is_dir() for directory in layout.all_directories())

    ledger = layout.databases / "quantbot.db"
    ledger.write_text("durable", encoding="utf-8")

    again = layout.create()
    assert again == (), "nothing was missing the second time"
    assert ledger.read_text(encoding="utf-8") == "durable"


def test_the_layout_keeps_the_existing_reports_convention(tmp_path: Path) -> None:
    """`QUANTBOT_REPORTS_DIR` and the weekly writer already use this name.

    Renaming it to `exports` to match a fresh preference would break a working convention for
    no measured benefit, and there is no `docker` directory because nothing here is
    containerised.
    """
    layout = StateLayout(tmp_path)

    assert layout.reports.name == "reports"
    assert layout.databases.name == "databases"
    assert not any(path.name == "docker" for path in layout.all_directories())


def test_state_dir_resolution_prefers_the_explicit_argument_then_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(STATE_DIR_ENV, str(tmp_path / "from-env"))
    assert resolve_state_dir() == tmp_path / "from-env"
    assert resolve_state_dir(tmp_path / "explicit") == tmp_path / "explicit"

    monkeypatch.delenv(STATE_DIR_ENV, raising=False)
    assert resolve_state_dir() == default_state_dir()

    # An empty variable is not a configured value; falling through beats writing to the root.
    monkeypatch.setenv(STATE_DIR_ENV, "   ")
    assert resolve_state_dir() == default_state_dir()


def test_the_default_state_dir_is_outside_any_repository_checkout() -> None:
    """The point of the state directory: a `git clean` must not be near the durable ledger."""
    default = default_state_dir()

    assert default.is_absolute()
    assert "quantbot" in default.parts[-1]
