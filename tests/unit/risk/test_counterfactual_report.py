import json
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from io import StringIO

import pytest

from quantbot.cli import main
from quantbot.risk.counterfactual import (
    CounterfactualOutcome,
    HypotheticalExecutionPolicy,
    UnevaluableReason,
)
from quantbot.risk.counterfactual_report import counterfactual_report
from quantbot.risk.regime import MarketRegimeSnapshot
from quantbot.risk.shadow import (
    CounterfactualState,
    RejectedCandidate,
    RejectionReason,
    as_rows,
)
from quantbot.storage import Database, StorageRepository

DAY = date(2026, 3, 2)
POLICY = HypotheticalExecutionPolicy().identity


@pytest.fixture
def database(tmp_path):
    with Database.create(tmp_path / "report.db") as db:
        yield db


def seed(
    db,
    name="a",
    *,
    candidate=None,
    gates=("liquidity",),
    version=1,
    parameters="parameters-1",
    config="cfg-1",
    policy=POLICY,
    state=None,
    value="20",
    regime=None,
    day=DAY,
):
    refusal = RejectedCandidate(
        observation_id=name,
        candidate_id=candidate or f"candidate-{name}",
        strategy_id="strategy",
        configuration_hash=config,
        symbol="AAPL",
        direction="LONG",
        observed_at=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        session_date=day,
        reference_price=Decimal("100"),
        market_regime_snapshot_id=regime,
        reasons=tuple(
            RejectionReason(
                gate_id=gate,
                gate_version=version,
                rule_parameters_hash=parameters,
                detail="refused",
            )
            for gate in gates
        ),
    )
    with db.transaction() as session:
        repo = StorageRepository(session)
        repo.record_rejected_candidate(*as_rows(refusal))
        if state is not None:
            evaluated = state == CounterfactualState.EVALUATED
            outcome = CounterfactualOutcome(
                observation_id=name,
                policy_identity=policy,
                state=state,
                horizon_session=day + timedelta(days=30),
                evaluated_at=datetime(2026, 9, 1, tzinfo=UTC),
                net_excess_bps=Decimal(value) if evaluated else None,
                gross_return_bps=Decimal(value) + 10 if evaluated else None,
                benchmark_return_bps=Decimal("0") if evaluated else None,
                unevaluable_reason=(
                    UnevaluableReason.HORIZON_PRICE_MISSING
                    if state == CounterfactualState.UNEVALUABLE
                    else None
                ),
                detail="fixture outcome",
            )
            repo.record_counterfactual_outcome(outcome.as_row())


def report(db, **kwargs):
    with db.transaction() as session:
        return counterfactual_report(StorageRepository(session), **kwargs)


def test_empty_report_has_no_economic_claim(database):
    result = report(database)
    assert result["ok"] is True
    assert result["policy_identity"] == POLICY
    assert result["distinct_candidates"] == 0
    assert result["groups"] == []
    assert result["analysis_role"] == "EXPLORATORY"


def test_counts_missing_and_terminal_outcomes_without_inventing_pending_horizons(database):
    seed(database, "missing", gates=("liquidity", "correlation"))
    seed(database, "unavailable", state=CounterfactualState.UNAVAILABLE)
    seed(database, "terminal", state=CounterfactualState.UNEVALUABLE)
    seed(database, "evaluated", state=CounterfactualState.EVALUATED)
    result = report(database)
    assert result["distinct_candidates"] == 4
    group = next(g for g in result["groups"] if g["gate_value"]["gate_id"] == "liquidity")
    assert group["distinct_candidates"] == 4
    assert group["outcome_counts"] == {
        "NO_RECORDED_OUTCOME": 1,
        "COUNTERFACTUAL_UNAVAILABLE": 1,
        "COUNTERFACTUAL_UNEVALUABLE": 1,
        "COUNTERFACTUAL_EVALUATED": 1,
    }
    assert group["unevaluable_reasons"] == {"HORIZON_PRICE_MISSING": 1}
    assert group["gate_value"]["verdict"] == "UNDERPOWERED"
    assert group["gate_value"]["mean_net_excess_bps"] is None
    assert result["underpowered_groups"] == 1


def test_policy_gate_parameters_and_configuration_are_separate(database):
    seed(database, "old", state=CounterfactualState.EVALUATED, policy="old-policy")
    seed(database, "version", version=2, state=CounterfactualState.EVALUATED)
    seed(database, "parameters", parameters="parameters-2", state=CounterfactualState.EVALUATED)
    seed(database, "config", config="cfg-2", state=CounterfactualState.EVALUATED)
    result = report(database)
    assert len(result["groups"]) == 4
    assert sum(g["gate_value"]["candidates"] for g in result["groups"]) == 3
    assert "old-policy" in result["available_policy_identities"]
    historical = report(database, policy_identity="old-policy")
    assert sum(g["gate_value"]["candidates"] for g in historical["groups"]) == 1
    assert all(g["gate_value"]["policy_identity"] == "old-policy" for g in historical["groups"])


def test_same_candidate_does_not_inflate_sample_or_counts(database):
    for name in ("one", "two"):
        seed(
            database,
            name,
            candidate="same",
            gates=("liquidity", "correlation"),
            state=CounterfactualState.EVALUATED,
        )
    result = report(database)
    assert result["distinct_candidates"] == 1
    for group in result["groups"]:
        assert group["distinct_candidates"] == 1
        assert group["refusal_observations"] == 2
        assert group["gate_value"]["candidates"] == 1
        assert group["observation_ids"] == ["one", "two"]


def test_conflicting_answers_for_same_candidate_fail_explicitly(database):
    seed(database, "one", candidate="same", state=CounterfactualState.EVALUATED, value="20")
    seed(database, "two", candidate="same", state=CounterfactualState.EVALUATED, value="-20")
    with pytest.raises(ValueError, match="conflicting.*candidate"):
        report(database)


def snapshot(db, *, version=1, source="test", day=DAY, unavailable=False):
    regime = MarketRegimeSnapshot(
        method_version=version,
        as_of_session=day,
        benchmark_symbol="SPY",
        source=source,
        trend="ABOVE",
        volatility="NORMAL",
        breadth="UNAVAILABLE" if unavailable else "BROAD",
        unavailable=("breadth history missing",) if unavailable else (),
    )
    with db.transaction() as session:
        StorageRepository(session).record_market_regime_snapshot(regime.as_row())
    return regime.identity


def test_explicit_regime_view_keeps_definitions_sources_and_missing_context_separate(database):
    for name, regime in (
        ("v1", snapshot(database)),
        ("v2", snapshot(database, version=2)),
        ("source", snapshot(database, source="other")),
        ("partial", snapshot(database, unavailable=True)),
        ("missing", None),
        ("dangling", "snapshot-not-present"),
    ):
        seed(database, name, regime=regime, state=CounterfactualState.EVALUATED)
    result = report(database, by_regime=True)
    assert len(result["groups"]) == 6
    assert all(g["gate_value"]["candidates"] == 1 for g in result["groups"])
    assert {g["regime"]["status"] for g in result["groups"]} == {
        "MEASURED",
        "PARTIAL",
        "NOT_RECORDED",
        "SNAPSHOT_MISSING",
    }
    partial = next(g for g in result["groups"] if g["regime"]["status"] == "PARTIAL")
    assert partial["regime_unavailable_reasons"] == ["breadth history missing"]
    assert len(partial["regime_snapshot_ids"]) == 1


def test_future_regime_binding_cannot_be_used(database):
    seed(database, regime=snapshot(database, day=DAY + timedelta(days=1)))
    with pytest.raises(ValueError, match="future.*regime"):
        report(database, by_regime=True)


def test_same_regime_definition_groups_sessions_and_retains_each_snapshot(database):
    identities = []
    for index in range(3):
        day = DAY + timedelta(days=index)
        identity = snapshot(database, day=day)
        identities.append(identity)
        seed(database, str(index), day=day, regime=identity, state=CounterfactualState.EVALUATED)
    result = report(database, by_regime=True)
    assert len(result["groups"]) == 1
    assert result["groups"][0]["regime_snapshot_ids"] == sorted(identities)
    assert result["groups"][0]["distinct_candidates"] == 3


def test_cli_selects_historical_policy(database, monkeypatch):
    seed(database, policy="historical-policy", state=CounterfactualState.EVALUATED)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database.path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert (
        main(["counterfactual-report", "--policy-identity", "historical-policy"], output=output)
        == 0
    )
    result = json.loads(output.getvalue())
    assert result["policy_identity"] == "historical-policy"
    assert result["groups"][0]["gate_value"]["candidates"] == 1


def test_invalid_policy_fails_as_json(database, monkeypatch):
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database.path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert main(["counterfactual-report", "--policy-identity", " "], output=output) == 2
    assert "policy_identity" in json.loads(output.getvalue())["reason"]


def test_powered_groups_reuse_existing_cost_adjusted_report(database):
    for index in range(45):
        seed(
            database,
            str(index),
            day=DAY + timedelta(days=index),
            state=CounterfactualState.EVALUATED,
            value="-25",
        )
    group = report(database)["groups"][0]
    assert group["gate_value"]["verdict"] == "REFUSALS_AVOIDED_LOSSES"
    assert Decimal(group["gate_value"]["mean_net_excess_bps"]) == Decimal("-25")


def dump(db):
    with sqlite3.connect(db.path) as connection:
        return list(connection.iterdump())


def test_real_cli_is_read_only_and_never_builds_runtime(database, monkeypatch):
    seed(database, state=CounterfactualState.EVALUATED)
    before = dump(database)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database.path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)

    def forbidden():
        pytest.fail("a reporting command must not construct the trading runtime")

    monkeypatch.setattr("quantbot.cli._default_context", forbidden)
    output = StringIO()
    assert main(["counterfactual-report", "--by-regime"], output=output) == 0
    result = json.loads(output.getvalue())
    assert result["groups"][0]["gate_value"]["verdict"] == "UNDERPOWERED"
    assert dump(database) == before


def test_cli_missing_store_fails_without_creating_it(tmp_path, monkeypatch):
    path = tmp_path / "absent" / "missing.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert main(["counterfactual-report"], output=output) != 0
    assert json.loads(output.getvalue())["ok"] is False
    assert not path.parent.exists()


def test_cli_does_not_repair_rows_or_change_journal_mode(database, monkeypatch):
    seed(database)
    database.close()
    with sqlite3.connect(database.path) as connection:
        connection.execute("DELETE FROM kill_switch_state")
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone() == ("delete",)
    before = dump(database)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database.path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert main(["counterfactual-report"], output=output) == 0
    assert dump(database) == before
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)


def test_duplicate_unavailable_explanations_are_not_silently_dropped(database):
    seed(database, "one", candidate="same", state=CounterfactualState.UNAVAILABLE)
    seed(database, "two", candidate="same", state=CounterfactualState.UNAVAILABLE)
    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE counterfactual_outcomes SET detail='different unavailable reason' "
            "WHERE observation_id='two'"
        )
    with pytest.raises(ValueError, match="conflicting.*candidate"):
        report(database)


def test_corrupt_store_returns_json_failure(tmp_path, monkeypatch):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a database")
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert main(["counterfactual-report"], output=output) == 2
    assert json.loads(output.getvalue())["ok"] is False
    assert path.read_bytes() == b"this is not a database"


def test_read_only_session_enforces_no_writes(database):
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    with Database.read_only(database.path) as session:
        with pytest.raises(OperationalError, match="readonly"):
            session.execute(text("DELETE FROM kill_switch_state"))


def test_read_only_session_keeps_one_snapshot_while_writer_appends(database):
    with Database.read_only(database.path) as session:
        repo = StorageRepository(session)
        assert repo.list_rejected_candidates() == []
        seed(database, state=CounterfactualState.EVALUATED)
        assert repo.list_counterfactual_outcomes() == []
        assert repo.list_rejected_candidates() == []
    with Database.read_only(database.path) as session:
        assert len(StorageRepository(session).list_counterfactual_outcomes()) == 1


@pytest.mark.parametrize("version_offset", [-1, 1])
def test_cli_rejects_other_schema_versions_without_rewriting(database, monkeypatch, version_offset):
    from quantbot.storage.schema import SCHEMA_VERSION

    with sqlite3.connect(database.path) as connection:
        connection.execute(
            "UPDATE schema_version SET version=?", (SCHEMA_VERSION + version_offset,)
        )
    before = dump(database)
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(database.path))
    monkeypatch.delenv("QUANTBOT_DATABASE_URL", raising=False)
    output = StringIO()
    assert main(["counterfactual-report"], output=output) == 2
    assert json.loads(output.getvalue())["ok"] is False
    assert dump(database) == before


def test_read_only_session_does_not_stamp_alembic_and_escapes_paths(tmp_path):
    path = tmp_path / "store #1.db"
    with Database.create(path) as db:
        db.close()
        with sqlite3.connect(path) as connection:
            connection.execute("DELETE FROM alembic_version")
        before = dump(db)
        with Database.read_only(path) as session:
            assert counterfactual_report(StorageRepository(session))["distinct_candidates"] == 0
        assert dump(db) == before
