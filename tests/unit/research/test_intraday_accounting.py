"""An abandoned intraday experiment still spends a trial, exactly once."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from quantbot.research.hypothesis_graph import GraphArtifactKind
from quantbot.research.registry import (
    DataRole,
    HypothesisRegistry,
    RefusalReason,
    RegistrationRefused,
    SearchOrigin,
)
from quantbot.storage import Database
from quantbot.storage.schema import search_runs
from tests.unit.research.test_artifact_evidence import artifact_test_draft, register_graph
from tests.unit.research.test_registry import make_draft, register, window
from tests.unit.research.test_validation_campaign_registry import declared_campaign

NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)
START = date(2026, 1, 1)
END = date(2026, 9, 11)


def record(session, **overrides):
    from quantbot.research.intraday_accounting import record_intraday_attempt

    args = dict(
        search_id="intraday:frozen-orb",
        configuration_hash="a" * 64,
        start=START,
        end=END,
        now=NOW,
    )
    args.update(overrides)
    return record_intraday_attempt(session, **args)


def test_committed_attempt_survives_engine_failure_and_exact_retry(tmp_path: Path):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            assert record(session) == "intraday:frozen-orb"
        with pytest.raises(RuntimeError, match="engine failed"), db.transaction():
            raise RuntimeError("engine failed before producing results")
        with db.transaction() as session:
            record(session, now=NOW + timedelta(hours=1))
            rows = session.execute(select(search_runs)).mappings().all()
            assert len(rows) == 1
            assert rows[0]["dataset"] == "us-equities-daily"
            assert rows[0]["candidates_evaluated"] == 1
            assert HypothesisRegistry(session).cumulative_trials() == 69
    finally:
        db.close()


def test_only_first_measured_descendant_uses_prepaid_trial(tmp_path: Path):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            search_id = record(session)
            registry = HypothesisRegistry(session)
            for number, expected in [(1, 69), (2, 70)]:
                draft = make_draft(
                    hypothesis_id=f"H-INTRADAY-{number}",
                    search_origin=SearchOrigin.MEASURED,
                    search_run_id=search_id,
                    search_cardinality=1,
                    materially_different=f"Prespecified distinct hypothesis {number}",
                    windows=(window(DataRole.VALIDATION, "2016-01-04", "2026-08-18"),),
                )
                result = register(registry, draft)
                assert result.cumulative_trials == expected
                assert registry.cumulative_trials() == expected
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"configuration_hash": "b" * 64},
        {"start": date(2025, 1, 1)},
        {"end": date(2026, 9, 10)},
    ],
)
def test_conflicting_retry_cannot_rewrite_attempt(tmp_path: Path, change):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            record(session)
            before = dict(session.execute(select(search_runs)).mappings().one())
            with pytest.raises(ValueError, match="conflict"):
                record(session, **change)
            assert dict(session.execute(select(search_runs)).mappings().one()) == before
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"search_id": "worker:orb"},
        {"search_id": "intraday:"},
        {"configuration_hash": "not-a-hash"},
        {"end": date(2025, 1, 1)},
        {"dataset": "fresh-intraday-budget"},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
def test_malformed_attempt_is_refused(tmp_path: Path, change):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            with pytest.raises(ValueError):
                record(session, **change)
            assert session.execute(select(search_runs)).first() is None
    finally:
        db.close()


@pytest.mark.parametrize(
    "change",
    [
        {"actor": "unrelated"},
        {"actor_version": "999"},
        {"candidates_evaluated": 2},
        {"detail_json": "{}"},
        {"detail_json": "not json"},
        {"dataset": "fresh-intraday-budget"},
        {"finished_at": "2026-09-15T12:00:00+00:00"},
    ],
)
def test_reserved_namespace_metadata_fails_closed(tmp_path: Path, change):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            record(session)
            session.execute(update(search_runs).values(**change))
            with pytest.raises(RegistrationRefused) as error:
                HypothesisRegistry(session).cumulative_trials()
            assert error.value.reason is RefusalReason.TAMPERED
            with pytest.raises(ValueError):
                record(session)
    finally:
        db.close()


def test_other_unregistered_search_namespaces_keep_existing_accounting(tmp_path: Path):
    db = Database.create(tmp_path / "research.db")
    try:
        with db.transaction() as session:
            record(session, dataset="sip-us-equities-daily")
            stored = dict(session.execute(select(search_runs)).mappings().one())
            assert stored["dataset"] == "us-equities-daily"
            session.execute(
                search_runs.insert().values(
                    **(stored | {"search_id": "worker:unregistered", "candidates_evaluated": 7})
                )
            )
            assert HypothesisRegistry(session).cumulative_trials() == 69
    finally:
        db.close()


def test_intraday_charge_preserves_campaign_artifact_and_historical_burden(tmp_path: Path):
    db = Database.create(tmp_path / "research.db")
    try:
        signal, _, _, registration = register_graph(db)
        with db.transaction() as session:
            registry = HypothesisRegistry(session)
            registry.freeze_validation_campaign(declared_campaign(), now=NOW)
            registry.register_artifact_test(
                registration,
                artifact_test_draft(registration, GraphArtifactKind.COMPONENT, signal.component_id),
                now=NOW,
            )
            # 68 historical + hypothesis + campaign + artifact test.
            assert registry.cumulative_trials() == 71
            record(session)
            record(
                session, search_id="intraday:another-frozen-attempt", configuration_hash="b" * 64
            )
            assert registry.cumulative_trials() == 73
            assert registry.campaign_trials_spent() == 1
    finally:
        db.close()
