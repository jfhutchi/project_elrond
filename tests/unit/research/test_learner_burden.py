"""Learner model selection is part of the project's permanent search burden."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from quantbot.research import HypothesisRegistry, RegistrationRefused
from quantbot.storage import Database
from quantbot.storage.schema import search_runs


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database.create(tmp_path / "research.db")
    yield db
    db.close()


def record_learner_attempt(
    session: Session,
    identity: str,
    *,
    candidates: int = 1,
    actor: str = "elrond-learner",
    role: str = "VALIDATION",
    configuration_hash: str = "a" * 64,
) -> None:
    session.execute(
        search_runs.insert().values(
            search_id=f"learner:{identity}",
            actor=actor,
            actor_version="1",
            candidates_evaluated=candidates,
            dataset="sip-us-equities-daily",
            start_date="2020-01-02",
            end_date="2025-12-31",
            data_role=role,
            started_at="2026-01-02T21:00:00Z",
            finished_at="2026-01-02T21:00:01Z",
            configuration_hash=configuration_hash,
            detail_json="{}",
        )
    )


@pytest.mark.parametrize("role", ("DISCOVERY", "VALIDATION"))
def test_valid_learner_attempt_increases_global_burden(
    database: Database, role: str
) -> None:
    with database.transaction() as session:
        record_learner_attempt(session, "selected", role=role)

        assert HypothesisRegistry(session, prior_trials=0).cumulative_trials() == 1


def test_discarded_learner_attempts_remain_in_global_burden(database: Database) -> None:
    with database.transaction() as session:
        record_learner_attempt(session, "discarded-a")
        record_learner_attempt(session, "discarded-b")
        record_learner_attempt(session, "selected")

        assert HypothesisRegistry(session, prior_trials=0).cumulative_trials() == 3


def test_learner_namespace_counts_even_when_actor_is_renamed(database: Database) -> None:
    with database.transaction() as session:
        record_learner_attempt(session, "spoofed", actor="some-other-actor")

        assert HypothesisRegistry(session, prior_trials=0).cumulative_trials() == 1


@pytest.mark.parametrize(
    ("role", "configuration_hash"),
    (("PROTECTED_EVALUATION", "a" * 64), ("VALIDATION", "not-a-content-hash")),
)
def test_malformed_learner_namespace_row_fails_closed(
    database: Database, role: str, configuration_hash: str
) -> None:
    with database.transaction() as session:
        record_learner_attempt(
            session,
            "malformed",
            role=role,
            configuration_hash=configuration_hash,
        )

        with pytest.raises(RegistrationRefused, match="learner.*search run"):
            HypothesisRegistry(session, prior_trials=0).cumulative_trials()
