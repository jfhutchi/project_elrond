"""Recorded learning is bounded, private evidence, never execution authority."""

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update

from quantbot.console.app import create_app
from quantbot.console.overview import read_overview
from quantbot.paper_core.activation import Activation
from quantbot.paper_core.contracts import LearningPolicy, LearningReceipt, ModelArtifact
from quantbot.storage import Database
from quantbot.storage.schema import metadata
from tests.unit.console.test_overview import core_role_fixture

NOW = datetime(2026, 11, 5, 14, tzinfo=UTC)


def put(session, name, document):
    session.execute(
        metadata.tables[name].insert().values(id=document.id, payload=document.model_dump_json())
    )


def fixture(database, *, fitted=False, reason="VALIDATION_GATE_FAILED", attempt=True):
    from quantbot.learner.estimators import FittedEstimator
    from quantbot.learner.five_minute import FEATURE_NAMES

    parent = ModelArtifact(description="private-token baseline")
    policy = LearningPolicy(parent_model=parent, evidence_lane="BROKER_PAPER")
    receipt = LearningReceipt(
        recorded_at_utc=NOW - timedelta(minutes=2),
        evidence_lane="BROKER_PAPER",
        run_id="a" * 64,
        cutoff=NOW - timedelta(minutes=3),
        parent_model_id=parent.id,
        resulting_model_id=parent.id,
        policy=policy,
        considered_ids=(),
        exclusions=(),
        split_digest="split",
        status="CANDIDATE_REJECTED",
        reason=reason,
        trial_charge_id="charge",
        training_ids=("train",) * 30,
        validation_ids=("validate",) * 10,
        metrics=(("candidate_mse", 4), ("incumbent_mse", 3), ("private-token metric", 99)),
    )
    model = parent
    if fitted:
        size = len(FEATURE_NAMES)
        estimator = FittedEstimator(
            family="ridge",
            feature_names=FEATURE_NAMES,
            means=(0,) * size,
            scales=(1,) * size,
            coefficients=(0,) * size,
            intercept=1,
            alpha=1,
            training_mse=1,
            seed=0,
            algorithm_version="ridge-cholesky-v1",
        )
        model = ModelArtifact(
            mode="PREDICT",
            feature_version="five-minute-price-alpha-v1",
            estimator=estimator,
            target_contract=policy.target_contract,
            policy_id=policy.policy_id,
            parent_model_id=parent.id,
            learning_receipt_id=receipt.run_id,
        )
        receipt = receipt.model_copy(
            update={
                "resulting_model_id": model.id,
                "candidate_model_id": model.id,
                "status": "AWAITING_ACTIVATION",
                "reason": "PASSED_FIXED_PROCEDURE_GATES",
            }
        )
    reuse = receipt.model_copy(
        update={
            "run_id": "b" * 64,
            "recorded_at_utc": NOW - timedelta(minutes=1),
            "parent_model_id": model.id,
            "resulting_model_id": model.id,
            "status": "NO_CHANGE",
            "reason": "SESSION_BUDGET_OR_INPUT_REUSED",
            "trial_charge_id": None,
            "training_ids": (),
            "validation_ids": (),
            "metrics": (),
        }
    )
    reuse = reuse.model_copy(update={"policy": policy.model_copy(update={"parent_model": model})})
    activation = Activation(
        config_id="config",
        evidence_lane="BROKER_PAPER",
        generation=2,
        previous_activation_id="previous",
        previous_model_id=model.id,
        model_id=model.id,
        model_digest=model.id,
        receipt_id=reuse.id,
        authority_id="authority",
        at=NOW,
        action="ACKNOWLEDGE",
        reason=reuse.reason,
        learning_run_id=reuse.run_id,
    )
    with database.transaction() as session:
        put(session, "core_models", model)
        if model.id != parent.id:
            put(session, "core_models", parent)
        for document in (receipt, reuse):
            put(session, "core_learning_runs", document)
            session.execute(
                metadata.tables["core_learning_considerations"]
                .insert()
                .values(run_id=document.run_id, receipt_id=document.id)
            )
        put(session, "core_activations", activation)
        session.execute(
            metadata.tables["core_active_model"]
            .insert()
            .values(
                id=1,
                activation_id=activation.id,
                model_id=model.id,
                generation=2,
                receipt_id=reuse.id,
            )
        )
        if attempt:
            session.execute(
                metadata.tables["search_runs"]
                .insert()
                .values(
                    search_id="charge",
                    actor="fixture",
                    actor_version="1",
                    candidates_evaluated=1,
                    dataset="fixture",
                    start_date="2026-11-04",
                    end_date="2026-11-04",
                    data_role="VALIDATION",
                    started_at=NOW.isoformat(),
                    finished_at=NOW.isoformat(),
                    configuration_hash="fixture",
                )
            )
            session.execute(
                metadata.tables["core_learning_attempts"]
                .insert()
                .values(
                    session="2026-11-04",
                    run_id=receipt.run_id,
                    search_id="charge",
                    input_id="split",
                    input_revision_digest="revision",
                    reservation_payload="{}",
                )
            )
    return model, activation, receipt


def read(database):
    from quantbot.console.core_learning import read_core_learning

    with database.transaction() as session:
        return read_core_learning(session, config_id="config", now=NOW)


def test_overview_learning_is_unavailable_without_account_scope(tmp_path):
    with Database.create(tmp_path / "test.db") as database:
        fixture(database)
        core_role_fixture(database, NOW)
        with database.transaction() as session:
            assert not read_overview(session, account_id="other", now=NOW).learning.available


def test_baseline_keeps_rejected_charged_attempt_after_no_change(tmp_path):
    with Database.create(tmp_path / "test.db") as database:
        model, _, _ = fixture(database)
        panel = read(database)
    assert panel.available and panel.model_id == model.id
    assert panel.model_status == "COLLECT · no fitted model"
    assert panel.consideration_reason == "SESSION_BUDGET_OR_INPUT_REUSED"
    assert panel.attempt_verdict == "CANDIDATE_REJECTED"
    assert panel.attempt_reason == "VALIDATION_GATE_FAILED"
    assert panel.training_samples == 30 and panel.validation_samples == 10
    assert panel.metrics == (("candidate_mse", 4.0), ("incumbent_mse", 3.0))
    assert "private-token" not in panel.model_dump_json()


def test_fitted_model_and_empty_attempt(tmp_path):
    with Database.create(tmp_path / "test.db") as database:
        fixture(database, fitted=True, attempt=False)
        panel = read(database)
    assert panel.available and panel.model_status == "PREDICT · fitted model"
    assert panel.attempt_verdict == "No evaluated attempt recorded"


def test_fitted_model_requires_accepted_final_training_receipt(tmp_path):
    with Database.create(tmp_path / "test.db") as database:
        _, _, receipt = fixture(database, fitted=True, attempt=False)
        with database.transaction() as session:
            mapping = metadata.tables["core_learning_considerations"]
            session.execute(mapping.delete().where(mapping.c.run_id == receipt.run_id))
        assert not read(database).available


def test_latest_inserted_decision_keeps_its_historical_model(tmp_path):
    from quantbot.paper_core.contracts import Decision

    with Database.create(tmp_path / "test.db") as database:
        current, _, _ = fixture(database, fitted=True)
        historical = ModelArtifact(description="private-token historical")
        activation = Activation(
            config_id="config",
            evidence_lane="BROKER_PAPER",
            generation=1,
            previous_activation_id=None,
            previous_model_id=None,
            model_id=historical.id,
            model_digest=historical.id,
            receipt_id=None,
            authority_id=None,
            at=NOW - timedelta(hours=1),
            action="INITIALIZE",
            reason="private-token",
        )
        decision = Decision(
            recorded_at_utc=NOW - timedelta(minutes=10),
            evidence_lane="BROKER_PAPER",
            activation_id=activation.id,
            model_generation=1,
            model_digest=historical.id,
            model_mode="COLLECT",
            observation_id="observation",
            policy_id="policy",
            model_id=historical.id,
            learning_receipt_id=historical.learning_receipt_id,
            config_id="config",
            inventory_id="inventory",
            inventory_generation=1,
            symbol="PRIVATE_SYMBOL",
            action="WAIT",
            gates=("private-token",),
            quantity=0,
            price=1,
            valid_until=NOW,
            session_close=NOW,
        )
        with database.transaction() as session:
            put(session, "core_models", historical)
            put(session, "core_activations", activation)
            put(session, "core_decisions", decision)
            # Later insert with older timestamp wins over all-history max-date logic.
            decision = decision.model_copy(update={"recorded_at_utc": NOW - timedelta(minutes=20)})
            put(session, "core_decisions", decision)
        panel = read(database)
    assert panel.available and panel.model_id == current.id
    assert panel.decision_model_id == historical.id
    assert panel.decision_at == NOW - timedelta(minutes=20)
    assert "PRIVATE_SYMBOL" not in panel.model_dump_json()


def test_rejected_attempt_cannot_claim_an_unaccepted_final_model(tmp_path):
    with Database.create(tmp_path / "test.db") as database:
        _, _, receipt = fixture(database)
        changed = receipt.model_copy(update={"resulting_model_id": "unaccepted-fold-model"})
        with database.transaction() as session:
            put(session, "core_learning_runs", changed)
            mapping = metadata.tables["core_learning_considerations"]
            session.execute(
                update(mapping)
                .where(mapping.c.run_id == changed.run_id)
                .values(receipt_id=changed.id)
            )
        assert not read(database).available


@pytest.mark.parametrize(
    "field,value",
    [
        ("config_id", "wrong"),
        ("evidence_lane", "SYNTHETIC_TEST"),
        ("at", NOW + timedelta(seconds=1)),
        ("model_digest", "wrong"),
    ],
)
def test_validly_hashed_but_mismatched_selection_is_unavailable(tmp_path, field, value):
    with Database.create(tmp_path / "test.db") as database:
        _, activation, _ = fixture(database)
        changed = activation.model_copy(update={field: value})
        with database.transaction() as session:
            put(session, "core_activations", changed)
            session.execute(
                update(metadata.tables["core_active_model"]).values(activation_id=changed.id)
            )
        assert not read(database).available


@pytest.mark.parametrize(
    "invalid", ["missing", "corrupt", "hash", "config", "future", "generation", "oversize", "lane"]
)
def test_invalid_evidence_is_unavailable(tmp_path, invalid):
    with Database.create(tmp_path / "test.db") as database:
        model, activation, _ = fixture(database)
        with database.transaction() as session:
            table = metadata.tables["core_activations"]
            if invalid == "missing":
                session.execute(metadata.tables["core_active_model"].delete())
            elif invalid == "generation":
                session.execute(update(metadata.tables["core_active_model"]).values(generation=3))
            else:
                changes = {
                    "hash": {"reason": "different"},
                    "config": {"config_id": "wrong"},
                    "future": {"at": NOW + timedelta(seconds=1)},
                    "lane": {"evidence_lane": "SYNTHETIC_TEST"},
                }
                payload = (
                    "bad-json"
                    if invalid == "corrupt"
                    else " " * (2 * 1024 * 1024 + 1)
                    if invalid == "oversize"
                    else activation.model_copy(update=changes[invalid]).model_dump_json()
                )
                session.execute(update(table).values(payload=payload))
        assert not read(database).available


def test_reader_queries_are_bounded_read_only(tmp_path):
    statements = []
    with Database.create(tmp_path / "test.db") as database:
        fixture(database)
        with database.transaction() as session:
            connection = session.connection()
            event.listen(
                connection,
                "before_cursor_execute",
                lambda conn, cursor, statement, *args: statements.append(statement),
            )
            from quantbot.console.core_learning import read_core_learning

            assert read_core_learning(session, config_id="config", now=NOW).available
    assert len(statements) <= 12
    for statement in statements:
        assert statement.lstrip().upper().startswith("SELECT")
        assert "WHERE" in statement or "LIMIT" in statement
    assert any("core_decisions.rowid DESC" in statement for statement in statements)
    assert any("core_learning_attempts.session DESC" in statement for statement in statements)


def test_api_and_template_withhold_private_reasons_and_keep_roles(tmp_path, monkeypatch):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "private-account")
    with Database.create(tmp_path / "test.db") as database:
        fixture(database, reason="private-token exception")
        core_role_fixture(database, NOW)
        client = TestClient(create_app(database))
        api = client.get("/api/state")
        page = client.get("/")
    assert api.status_code == page.status_code == 200
    assert api.json()["overview"]["learning"]["available"]
    assert "private-token" not in api.text + page.text
    assert "Unrecognized reason; inspect private receipts" in page.text
    assert "Prediction checks do not establish profitable trading." in page.text
    assert "Latest charged attempt" in page.text and "Last consideration" in page.text
    assert "Legacy learner records" in page.text
