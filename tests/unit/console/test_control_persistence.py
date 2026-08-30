"""The policy history made durable, and the one race the application layer cannot see (#82).

`apply_proposal` already refuses a proposal composed against a stale head, and the tests for it
pass. That check reads the head and then writes a successor, so it is blind to a second process
doing the same thing between the two -- and a console request and a scheduled apply are exactly
two processes. The unique index on `supersedes` is therefore not belt-and-braces over the
in-process check; it covers a failure the in-process check cannot detect at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from quantbot.console.control import apply_proposal, initial_version, rollback_to
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


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


@pytest.fixture
def database(tmp_path: Path):
    db = Database.create(tmp_path / "policy.db")
    try:
        yield db
    finally:
        db.close()


def test_a_version_round_trips_with_its_provenance_intact(database) -> None:
    """The diff, the actor, the reason and the authority are the point of storing it at all."""
    first = initial_version(now=NOW)
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_policy_version(first.as_row()) is True
        assert repository.record_policy_version(second.as_row()) is True

    with database.transaction() as session:
        history = StorageRepository(session).list_policy_versions()

    assert [row["version"] for row in history] == [1, 2]
    assert history[1]["supersedes"] == first.identity
    assert history[1]["authority"] == "PROJECT_DEFAULT"
    assert history[1]["applied_by"] == "hutch"
    assert "wider clusters" in str(history[1]["reason"])
    assert "0.90" in str(history[1]["changes"]) and "0.85" in str(history[1]["changes"])


def test_an_old_version_still_resolves_after_the_threshold_moves(database) -> None:
    """The whole reason this table exists.

    A refusal recorded under version 1 cites version 1's identity. If applying a change edited
    the row, that refusal would cite an identity nothing can resolve -- and the durable record of
    what the system actually refused, and under what rule, would be gone.
    """
    first = initial_version(now=NOW)
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_policy_version(first.as_row())
        repository.record_policy_version(second.as_row())

    with database.transaction() as session:
        versions = StorageRepository(session).list_policy_versions()
    history = {row["identity"]: row for row in versions}

    assert first.identity in history, "the superseded version is still resolvable"
    assert '"correlation_threshold":"0.90"' in str(history[first.identity]["body"]).replace(
        ", ", ","
    ).replace('": "', '":"')


def test_two_processes_racing_on_the_same_head_cannot_fork_the_history(database) -> None:
    """The failure `apply_proposal` is structurally unable to catch.

    Both callers read version 1, both build a successor to it, and neither sees the other -- the
    optimistic check passes in both processes because it ran before either wrote. Without the
    unique index the result is two rows each claiming to follow version 1, and no reader can then
    say which rule was in force. The second writer must fail instead.
    """
    first = initial_version(now=NOW)
    one = apply_proposal(proposal(first.identity), head=first, now=NOW)
    two = apply_proposal(
        proposal(first.identity, updates={"max_daily_loss_bps": 400}), head=first, now=NOW
    )

    assert one.supersedes == two.supersedes == first.identity
    assert one.identity != two.identity, "genuinely different changes, not a retry"

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_policy_version(first.as_row())
        repository.record_policy_version(one.as_row())

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            StorageRepository(session).record_policy_version(two.as_row())

    with database.transaction() as session:
        history = StorageRepository(session).list_policy_versions()
    assert [row["identity"] for row in history] == [first.identity, one.identity]


def test_two_rows_cannot_claim_the_same_ordinal(database) -> None:
    """Ordering the history is the point of storing it; an ambiguous ordinal defeats that."""
    first = initial_version(now=NOW)
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)
    forged = second.model_copy(update={"identity": "veto-policy-forged", "supersedes": None})

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_policy_version(first.as_row())
        repository.record_policy_version(second.as_row())

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            StorageRepository(session).record_policy_version(forged.as_row())


def test_only_the_first_version_may_have_no_predecessor(database) -> None:
    """A later version with no `supersedes` would be an orphan the chain cannot explain."""
    first = initial_version(now=NOW)
    orphan = first.model_copy(update={"identity": "veto-policy-orphan", "version": 7})

    with database.transaction() as session:
        StorageRepository(session).record_policy_version(first.as_row())

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            StorageRepository(session).record_policy_version(orphan.as_row())


def test_a_superseded_version_cannot_be_deleted_out_from_under_its_successor(database) -> None:
    """`ondelete=RESTRICT`. Pruning history would orphan the refusals recorded against it."""
    first = initial_version(now=NOW)
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.record_policy_version(first.as_row())
        repository.record_policy_version(second.as_row())

    with pytest.raises(IntegrityError):
        with database.transaction() as session:
            session.connection().exec_driver_sql(
                "DELETE FROM veto_policy_versions WHERE identity = ?", (first.identity,)
            )


def test_the_head_is_the_highest_ordinal_rather_than_the_latest_timestamp(database) -> None:
    """Two applies can share a timestamp at this resolution; the ordinal is what is guaranteed."""
    first = initial_version(now=NOW)
    second = apply_proposal(proposal(first.identity), head=first, now=NOW)
    third = rollback_to(first, head=second, actor="hutch", reason="reverting", now=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        for version in (first, second, third):
            repository.record_policy_version(version.as_row())

    with database.transaction() as session:
        head = StorageRepository(session).policy_version_head()

    assert head is not None
    assert head["version"] == 3
    assert head["identity"] == third.identity
    assert str(third.applied_at.isoformat()) == str(second.applied_at.isoformat()), (
        "the timestamps genuinely tie, so ordering by them would be arbitrary here"
    )


def test_no_head_before_anything_is_recorded(database) -> None:
    """Absence is absence. A default head would be a rule nobody chose."""
    with database.transaction() as session:
        assert StorageRepository(session).policy_version_head() is None


def test_recording_the_same_version_twice_is_idempotent(database) -> None:
    """A retried apply must not fail, and must not append a duplicate."""
    first = initial_version(now=NOW)

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.record_policy_version(first.as_row()) is True
        assert repository.record_policy_version(first.as_row()) is False

    with database.transaction() as session:
        assert len(StorageRepository(session).list_policy_versions()) == 1


def test_the_same_identity_with_a_different_body_raises_rather_than_being_absorbed(
    database,
) -> None:
    """The identity is content-addressed. If it can be reached by two bodies it is broken, and
    quietly keeping the first would hide that from every reader downstream.
    """
    first = initial_version(now=NOW)
    impostor = first.model_copy(
        update={"body": {**dict(first.body), "correlation_threshold": "0.1"}}
    )

    with database.transaction() as session:
        StorageRepository(session).record_policy_version(first.as_row())

    with pytest.raises(ValueError, match="not addressing the content"):
        with database.transaction() as session:
            StorageRepository(session).record_policy_version(impostor.as_row())


def test_the_migration_is_additive(tmp_path: Path) -> None:
    """A real 0018 store through the real migration."""
    from alembic import command
    from alembic.config import Config

    from quantbot.storage.schema import SCHEMA_VERSION

    path = tmp_path / "v18.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0018")

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            assert StorageRepository(session).policy_version_head() is None
        with database.engine.connect() as connection:
            marker = connection.exec_driver_sql("SELECT version FROM schema_version").fetchone()
    finally:
        database.close()

    assert marker is not None and marker[0] == SCHEMA_VERSION
