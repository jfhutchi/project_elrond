"""Saved range/role integrity and confidentiality; synthetic reports only."""

import hashlib
import json
import sqlite3

from fastapi.testclient import TestClient

from quantbot.console.app import create_app
from quantbot.console.history import read_history
from quantbot.research.manifest import content_id
from quantbot.storage import Database
from tests.unit.console.test_learner_panel import fixture_store


def bundle(tmp_path):
    root = fixture_store(tmp_path / "learner")
    old = json.loads((root / "evidence/ridge-economics.json").read_text())["report"]
    model = dict(
        model_id="model-1",
        dataset_id="dataset-1",
        family="ridge",
        training_start="2025-09-22T20:00:00Z",
        training_end="2025-12-31T21:00:00Z",
        validation_start="2026-01-02T21:00:00Z",
        validation_end="2026-03-31T20:00:00Z",
    )
    old["lineage"]["model_hash"] = content_id(model)
    old.update(
        view_role="VALIDATION_REUSED",
        source_evidence="LOCAL_DECLARED",
        raw_cohorts=1,
        instrument_count=2,
    )
    doc = dict(
        schema=1,
        views=[
            dict(
                role="VALIDATION_REUSED",
                range=r,
                tag="ridge",
                report=old,
                report_hash=content_id(old),
            )
            for r in ("ALL", "2026-03")
        ],
    )
    return root, model, doc


def save(root, doc):
    (root / "evidence/history-v1.json").write_text(
        json.dumps(dict(hash=content_id(doc), document=doc))
    )


def test_verified_range_and_role_excludes_arbitrary_report_fields(tmp_path):
    root, model, doc = bundle(tmp_path)
    save(root, doc)
    panel = read_history(root, {"model-1": model}, "2026-03")
    assert panel.options == ("2026-03",)
    assert panel.sections[0].role == "VALIDATION_REUSED"
    assert panel.sections[0].series[0].net_return < 0
    assert "do-not-render-this" not in panel.model_dump_json()
    assert read_history(root, {"model-1": model}, "../../").sections == ()
    assert read_history(root, {"model-1": model}, "2026-04").sections == ()


def test_nonfinite_nested_price_cannot_render_nan_curve(tmp_path):
    root, model, doc = bundle(tmp_path)
    for view in doc["views"]:
        view["report"]["portfolios"]["top_quintile"]["daily_equity"][0]["equity"] = float("nan")
        view["report_hash"] = content_id(view["report"])
    save(root, doc)
    assert read_history(root, {"model-1": model}).sections == ()


def test_role_tampering_and_declared_dates_fail_closed(tmp_path):
    root, model, doc = bundle(tmp_path)
    for view in doc["views"]:
        view["role"] = view["report"]["view_role"] = "TRAINING_IN_SAMPLE"
        view["report_hash"] = content_id(view["report"])
    save(root, doc)
    assert read_history(root, {"model-1": model}).sections == ()


def test_rendered_learner_range_is_read_only_and_labels_simulation(tmp_path):
    root, dates, doc = bundle(tmp_path)
    with sqlite3.connect(root / "state/learner.db") as c:
        old = json.loads(
            c.execute("select document_json from learner_model_versions").fetchone()[0]
        )
        old.update(dates)
        text = json.dumps(old)
        c.execute(
            "update learner_model_versions set document_json=?,document_hash=?",
            (text, hashlib.sha256(text.encode()).hexdigest()),
        )
    for view in doc["views"]:
        view["report"]["lineage"]["model_hash"] = content_id(old)
        view["report_hash"] = content_id(view["report"])
    save(root, doc)
    db = Database.create(tmp_path / "operational.db")
    try:
        before = (root / "state/learner.db").read_bytes()
        with TestClient(create_app(db, learner_directory=root)) as client:
            response = client.get("/learner?history_range=2026-03")
            assert response.status_code == 200
            assert "Validation replay" in response.text
            assert "reused during model development" in response.text
            assert "broker trades or daily account" in response.text
            assert "2026-03 decision month" in response.text
            assert client.get("/learner?history_range=../../").status_code == 422
            assert (
                client.get("/api/learner?history_range=2026-03").json()["history"]["selected"]
                == "2026-03"
            )
        assert before == (root / "state/learner.db").read_bytes()
    finally:
        db.close()
