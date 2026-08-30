"""Which rule the runtime is actually running under (#82).

Slices 3 and 4 built a version chain and made it durable, and every test for them passed while
the runtime went on constructing `PortfolioVetoPolicy()` from the shipped defaults. Applying a
change produced a correct, fully audited history that changed nothing about what the system did.

That is the fifth time this project has found a control that is correct in isolation and never
reached in production (#75 classification, #74 calendar, #58 dependence, #56 detector, this). So
the tests here are mostly about reachability rather than about the resolver's arithmetic.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.console.control import apply_proposal, initial_version
from quantbot.risk.policy_versions import (
    PolicySource,
    PolicyUnreadable,
    active_policy_version,
    recorded_policy_version,
)
from quantbot.risk.vetoes import PortfolioVetoPolicy
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)
SOURCE = Path("src/quantbot/risk/policy_versions.py")


@pytest.fixture
def repository(tmp_path: Path):
    database = Database.create(tmp_path / "policy.db")
    try:
        with database.transaction() as session:
            yield StorageRepository(session)
    finally:
        database.close()


def proposal(base: str, **overrides: object):
    from quantbot.console.control import ConfigProposal

    fields: dict[str, object] = {
        "base_identity": base,
        "updates": {"correlation_threshold": "0.85"},
        "actor": "hutch",
        "reason": "wider clusters after re-measuring",
    }
    fields.update(overrides)
    return ConfigProposal(**fields)  # type: ignore[arg-type]


# --- reachability ---------------------------------------------------------------------------


def test_the_runtime_resolves_the_recorded_rule_rather_than_the_shipped_defaults(
    repository,
) -> None:
    """The whole point of the slice. Without this the version chain is decorative."""
    first = active_policy_version(repository, now=NOW)[0]
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)
    repository.record_policy_version(second.as_row())

    resolved, source = active_policy_version(repository, now=NOW)

    assert source is PolicySource.RECORDED
    assert resolved.identity == second.identity
    assert resolved.policy().correlation_threshold == Decimal("0.85")
    assert resolved.policy().correlation_threshold != (
        PortfolioVetoPolicy().correlation_threshold
    ), "the applied change is what the runtime would enforce, not the default"


def test_the_composition_root_passes_the_resolved_policy_to_the_runners() -> None:
    """Asserted structurally, because the failure it guards against is silent.

    A runner constructed without `veto_policy` falls back to `PortfolioVetoPolicy()`, which is
    exactly the bug this slice fixes and which no assertion about the resolver would catch. If a
    future edit drops the argument, the system quietly returns to enforcing the defaults while
    the history keeps recording changes nobody is running.
    """
    tree = ast.parse(Path("src/quantbot/runtime.py").read_text(encoding="utf-8"))
    constructed = {
        node.func.id: {keyword.arg for keyword in node.keywords}
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"MarketDataSync", "AdaptiveMomentumRunner"}
    }

    assert set(constructed) == {"MarketDataSync", "AdaptiveMomentumRunner"}
    for name, keywords in constructed.items():
        assert "veto_policy" in keywords, f"{name} would fall back to the shipped defaults"


def test_the_runtime_can_resolve_its_rule_without_the_console_package() -> None:
    """`quantbot.operations` imports this package, so the record must not sit behind the console.

    If it did, stripping the web console -- or breaking one of its imports -- would stop the
    trading loop from being able to answer which rule it is running under. That is the wrong
    dependency direction for a surface whose whole purpose is to be optional.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not [name for name in imported if "console" in name]


# --- bootstrapping is a record, not a default -------------------------------------------------


def test_an_empty_store_records_the_shipped_defaults_as_version_one(repository) -> None:
    """Otherwise the history starts at whatever the first console edit happened to be.

    Every refusal recorded before that edit would then cite an identity the history cannot
    explain, which is the same unattributable-evidence failure the append-only design exists to
    prevent -- just moved to the beginning.
    """
    version, source = active_policy_version(repository, now=NOW)

    assert source is PolicySource.BOOTSTRAPPED
    assert version.version == 1
    assert version.supersedes is None
    assert version.identity == PortfolioVetoPolicy().identity
    assert version.authority.value == "PROJECT_DEFAULT", "never rendered as an operator's choice"
    assert repository.policy_version_head() is not None, "it is recorded, not just returned"


def test_bootstrapping_happens_once_and_is_not_repeated(repository) -> None:
    """A second call must read what the first wrote rather than appending another version 1."""
    first, first_source = active_policy_version(repository, now=NOW)
    second, second_source = active_policy_version(repository, now=NOW)

    assert first_source is PolicySource.BOOTSTRAPPED
    assert second_source is PolicySource.RECORDED
    assert first.identity == second.identity
    assert len(repository.list_policy_versions()) == 1


# --- an unreadable rule is fatal ---------------------------------------------------------------


def test_a_recorded_policy_that_cannot_be_rebuilt_refuses_to_run(repository) -> None:
    """Falling back to the defaults would enforce a rule nobody chose while a different one sits
    recorded, and every refusal produced would cite an identity that does not match what was
    enforced. A missed session is the cheaper failure.
    """
    first = initial_version(now=NOW)
    repository.record_policy_version({**first.as_row(), "body": '{"correlation_threshold": 99}'})

    with pytest.raises(PolicyUnreadable, match="refusing to run under an unknown policy"):
        active_policy_version(repository, now=NOW)


def test_the_fatal_path_names_the_version_it_could_not_rebuild(repository) -> None:
    """An operator cannot fix a corrupted row the message does not identify."""
    first = initial_version(now=NOW)
    repository.record_policy_version({**first.as_row(), "body": "not json at all"})

    with pytest.raises(PolicyUnreadable, match=first.identity):
        active_policy_version(repository, now=NOW)


# --- the read-only variant ----------------------------------------------------------------------


def test_reading_the_rule_never_writes_one(repository) -> None:
    """The console is read-only by construction; a display that wrote a row to answer a GET would
    break that for the sake of a label.
    """
    assert recorded_policy_version(repository) is None
    assert repository.list_policy_versions() == []

    active_policy_version(repository, now=NOW)
    assert recorded_policy_version(repository) is not None
    assert len(repository.list_policy_versions()) == 1


def test_a_rollback_restores_the_rule_even_though_the_version_row_is_new(repository) -> None:
    """The identity a refusal cites is the *policy's*, and a rollback genuinely restores it.

    Two refusals either side of a rollback were measured under identical thresholds, so they are
    legitimately comparable and share a policy identity. The version rows are what distinguish
    when each was in force -- that is the distinction the two identities exist to draw.
    """
    from quantbot.console.control import rollback_to

    first = active_policy_version(repository, now=NOW)[0]
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)
    repository.record_policy_version(second.as_row())
    third = rollback_to(first, head=second, actor="hutch", reason="reverting", now=NOW)
    repository.record_policy_version(third.as_row())

    resolved = active_policy_version(repository, now=NOW)[0]

    assert resolved.version == 3
    assert resolved.identity != first.identity, "a different version"
    assert resolved.policy().identity == first.policy().identity, "the same rule"
