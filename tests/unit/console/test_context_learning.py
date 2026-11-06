"""Context learner status is bounded metadata, never source text or worker liveness."""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.research.manifest import content_id
from quantbot.storage import Database

NOW = datetime(2026, 11, 6, 22, tzinfo=UTC)


def research(tmp_path, *, status=None, binding=True):
    path = tmp_path / "synthetic-research.db"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("create table metadata(key text primary key,payload text)")
        db.execute("create table receipts(id text primary key,payload text)")
        if binding:
            db.execute(
                "insert into metadata values (?,?)",
                (
                    "context:learning-binding",
                    json.dumps(
                        {
                            "contract": "SCHEDULED_CONTEXT_LEARNING_V1",
                            "secret_path": "PRIVATE-SENTINEL",
                        }
                    ),
                ),
            )
        if status is not None:
            db.execute(
                "insert into metadata values (?,?)", ("context:learning-status", json.dumps(status))
            )
    return path


def status(**changes):
    return (
        dict(
            contract="SCHEDULED_CONTEXT_LEARNING_STATUS_V1",
            recorded_at=NOW.isoformat(),
            state="COLLECTING",
            snapshots_journaled=7,
            feedback_journaled=0,
            independent_reviews=0,
            eligible_examples=0,
            gate_counts=[["NO_ELIGIBLE_INDEPENDENT_REVIEWS", 1]],
            raw_news="SECRET-STORY-SENTINEL",
        )
        | changes
    )


def test_uncommissioned_missing_and_old_schema_never_create_research_database(tmp_path):
    from quantbot.console.context_learning import read_context_learning

    missing = tmp_path / "missing.db"
    assert read_context_learning(missing, now=NOW).state == "UNCOMMISSIONED"
    assert not missing.exists()
    path = research(tmp_path, binding=False)
    assert read_context_learning(path, now=NOW).state == "UNCOMMISSIONED"


def test_zero_reviews_are_data_gate_not_missing_connection_or_stale_worker(tmp_path):
    from quantbot.console.context_learning import read_context_learning

    path = research(tmp_path, status=status(recorded_at=(NOW - timedelta(days=8)).isoformat()))
    panel = read_context_learning(path, now=NOW)
    assert panel.state == "COLLECTING" and panel.independent_reviews == 0
    assert panel.status_changed_at == NOW - timedelta(days=8)
    assert panel.observed_snapshots == 7 and panel.used_documents is None
    assert panel.current_model_inputs == "PRICE_ONLY_NO_VERIFIED_CONTEXT_FILTER"
    assert "SECRET" not in panel.model_dump_json() and "PRIVATE" not in panel.model_dump_json()


def test_pending_error_and_future_status_are_explicit(tmp_path):
    from quantbot.console.context_learning import read_context_learning

    path = research(tmp_path)
    assert read_context_learning(path, now=NOW).state == "PENDING"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "insert into metadata values (?,?)",
            (
                "context:learning-status",
                json.dumps(
                    status(
                        state="UNAVAILABLE", gate_counts=[["CONTEXT_LEARNING_FAILED:ValueError", 1]]
                    )
                ),
            ),
        )
    assert read_context_learning(path, now=NOW).state == "UNAVAILABLE"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "update metadata set payload=? where key=?",
            (
                json.dumps(status(recorded_at=(NOW + timedelta(seconds=1)).isoformat())),
                "context:learning-status",
            ),
        )
    assert read_context_learning(path, now=NOW).state == "ERROR"


def test_state_and_learner_page_share_sanitized_read_only_status(tmp_path):
    path = research(tmp_path, status=status())
    before = path.read_bytes()
    with Database.create(tmp_path / "synthetic-console.db") as database:
        with TestClient(create_app(database, research_store=path)) as client:
            payload = client.get("/api/state").json()["context_learning"]
            assert payload["independent_reviews"] == 0
            page = client.get("/learner")
            assert page.status_code == 200
            assert "News context learning" in page.text
            assert "Status changed" in page.text and "independent reading labels" in page.text
            assert "SECRET-STORY-SENTINEL" not in page.text
    assert path.read_bytes() == before


def test_shadow_version_is_separate_from_trading_and_unknown_fit_counts(tmp_path):
    from quantbot.console.context_learning import read_context_learning

    path = research(
        tmp_path,
        status=status(
            independent_reviews=12,
            eligible_examples=8,
            training_start=NOW.isoformat(),
            split_at=(NOW + timedelta(days=2)).isoformat(),
            evaluation_end=(NOW + timedelta(days=4)).isoformat(),
            impact_evaluation_end=(NOW + timedelta(days=8)).isoformat(),
        ),
    )
    version = {"mode": "SHADOW_ONLY", "candidate_id": "c" * 64, "recorded_at": NOW.isoformat()}
    identity = content_id(version)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("insert into receipts values (?,?)", (identity, json.dumps(version)))
        db.execute(
            "insert into metadata values (?,?)",
            ("context:interpreter-current", json.dumps(identity)),
        )
    panel = read_context_learning(path, now=NOW)
    assert panel.interpreter_mode == "SHADOW_ONLY"
    assert panel.current_model_inputs == "PRICE_ONLY_NO_VERIFIED_CONTEXT_FILTER"
    assert panel.independent_reviews == 12 and panel.eligible_examples == 8
    assert panel.trial_fits is None and panel.fitted_candidates is None
    assert panel.used_documents is None and panel.impact_due == NOW + timedelta(days=8)


def test_model_generated_reviews_are_not_reported_as_independent_truth(tmp_path):
    path = research(
        tmp_path,
        status=status(
            model_generated_reviews=3,
            semantic_target="MODEL_TEACHER_AGREEMENT_V1",
            interpreter_kind="UNTRAINED_FIXED_LEXICAL_BASELINE",
        ),
    )
    with Database.create(tmp_path / "synthetic-console.db") as database:
        with TestClient(create_app(database, research_store=path)) as client:
            panel = client.get("/api/state").json()["context_learning"]
            assert panel["model_generated_reviews"] == 3
            assert panel["independent_reviews"] == 0
            assert panel["semantic_target"] == "MODEL_TEACHER_AGREEMENT_V1"
            assert panel["interpreter_kind"] == "UNTRAINED_FIXED_LEXICAL_BASELINE"
            page = client.get("/learner").text
            assert "Model-generated reviews" in page
            assert "not independent truth" in page
