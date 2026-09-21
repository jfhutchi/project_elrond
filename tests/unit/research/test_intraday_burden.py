"""Intraday pre-engine charges remain part of the canonical qualification burden."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import update

from quantbot.learner.estimators import fit_estimator
from quantbot.research.intraday_accounting import record_intraday_attempt
from quantbot.research.registry import HypothesisRegistry
from quantbot.storage import Database
from quantbot.storage.schema import search_runs


@pytest.mark.parametrize("fit_fails", (False, True))
def test_committed_intraday_charge_survives_fit_and_idempotent_retry(tmp_path, fit_fails):
    now = datetime(2026, 9, 21, 15, tzinfo=UTC)
    attempt = dict(
        search_id="intraday:learner-fixed-candidate",
        configuration_hash="a" * 64,
        start=date(2026, 1, 2),
        end=date(2026, 3, 31),
    )
    with Database.create(tmp_path / "research.db") as database:
        with database.transaction() as session:
            before = HypothesisRegistry(session).cumulative_trials()
            record_intraday_attempt(session, **attempt, now=now)

        # The real fit executes only after the budget transaction commits. A failed
        # fit leaves no fitted artifact, but must spend exactly the same trial.
        if fit_fails:
            with pytest.raises(ValueError, match="length"):
                fit_estimator(((1.0,), (2.0,)), (1.0,), ("return",), family="ridge")
        else:
            fit_estimator(((1.0,), (2.0,)), (1.0, 2.0), ("return",), family="ridge")

        with database.transaction() as session:
            assert HypothesisRegistry(session).cumulative_trials() == before + 1
            record_intraday_attempt(session, **attempt, now=now + timedelta(hours=1))
        with database.transaction() as session:
            assert HypothesisRegistry(session).cumulative_trials() == before + 1


@pytest.mark.parametrize("mutation", (
    {"actor": "unrelated-actor"},
    {"configuration_hash": "not-a-hash"},
    {"detail_json": "{}"},
    {"candidates_evaluated": 2},
))
def test_malformed_intraday_namespace_row_blocks_global_burden(tmp_path, mutation):
    with Database.create(tmp_path / "research.db") as database:
        with database.transaction() as session:
            record_intraday_attempt(
                session,
                search_id="intraday:learner-corrupted-candidate",
                configuration_hash="b" * 64,
                start=date(2026, 1, 2),
                end=date(2026, 3, 31),
                now=datetime(2026, 9, 21, 15, tzinfo=UTC),
            )
            session.execute(update(search_runs).values(**mutation))
        with database.transaction() as session:
            with pytest.raises(ValueError, match="intraday attempt.*malformed"):
                HypothesisRegistry(session).cumulative_trials()
