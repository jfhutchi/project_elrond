"""Synthetic tracking evidence: no broker calls, fitting or operational writes."""

from datetime import timedelta

import pytest

from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.learner.five_minute_forward import FiveMinutePrediction
from quantbot.learner.forward import _read, _save
from quantbot.learner.tracking_feedback import ingest_market_labels
from quantbot.learner.tracking_feedback import shadow_tick as observe_shadow
from tests.unit.learner.test_adaptive_research import NOW, label


def shadow_tick(**options):
    options.setdefault("clock", lambda: options["now"])
    return observe_shadow(**options)


def source(tmp_path):
    row = label(NOW - timedelta(hours=1))
    prediction = FiveMinutePrediction(
        model_id="a" * 64,
        features=row.features,
        recorded_at=row.features.decision_at,
        score=0.0001,
        cost_status="BELOW_FIXED_COST_FLOOR",
    )
    path = tmp_path / "source" / "outcomes" / row.features.feature_at.date().isoformat() / "p.json"
    _save(
        path.parent.parent.parent / "predictions" / path.parent.name / path.name,
        prediction.model_dump(mode="json"),
    )
    document = dict(
        prediction_id=path.stem,
        prediction_hash=prediction.prediction_id,
        label=row.model_dump(mode="json"),
        evidence_class="PROSPECTIVE_LOCAL",
        orders=0,
        account_pnl=False,
    )
    _save(path, document)
    return path, document, row


def ingest(tmp_path, **options):
    args = dict(
        source=tmp_path / "source",
        output=tmp_path / "learning",
        now=NOW,
        guard=lambda *_: True,
        model_ids=("a" * 64,),
    )
    args.update(options)
    return ingest_market_labels(**args)


def test_all_forecasts_including_rejected_entries_become_deduplicated_market_labels(tmp_path):
    source(tmp_path)
    assert ingest(tmp_path) == 1
    assert ingest(tmp_path) == 0
    records = list((tmp_path / "learning").glob("*.json"))
    assert len(records) == 1
    record = _read(records[0])
    assert record["selection"] == "ALL_VALID_FORECASTS_NOT_ONLY_EXECUTED_TRADES"
    assert record["account_pnl"] is False


def test_guard_runs_before_protected_document_is_read(tmp_path):
    path, _, _ = source(tmp_path)
    path.write_text("not json")
    assert ingest(tmp_path, guard=lambda *_: False) == 0


@pytest.mark.parametrize("change", ["hash", "future", "features", "authority", "model"])
def test_bad_lineage_or_lookahead_rejected(tmp_path, change):
    path, document, row = source(tmp_path)
    if change == "hash":
        document["prediction_hash"] = "b" * 64
    if change == "future":
        document["label"] = row.model_copy(
            update={"available_at": NOW + timedelta(hours=1)}
        ).model_dump(mode="json")
    if change == "features":
        document["label"]["features"]["reference_close"] = 101
    if change == "authority":
        document["orders"] = 1
    if change == "model":
        with pytest.raises(ValueError):
            ingest(tmp_path, model_ids=("b" * 64,))
        return
    path.unlink()
    _save(path, document)
    with pytest.raises(ValueError):
        ingest(tmp_path)


def test_missing_source_is_not_reported_as_zero_progress_success(tmp_path):
    with pytest.raises(FileNotFoundError):
        ingest(tmp_path)


def test_resolution_revisions_are_not_extra_training_examples(tmp_path):
    path, document, row = source(tmp_path)
    prediction = _read(path.parent.parent.parent / "predictions" / path.parent.name / path.name)
    prediction["model_id"] = "b" * 64
    second = FiveMinutePrediction.model_validate(prediction)
    other = path.with_name("q.json")
    _save(
        other.parent.parent.parent / "predictions" / other.parent.name / other.name,
        second.model_dump(mode="json"),
    )
    document.update(
        prediction_id="q",
        prediction_hash=second.prediction_id,
        label=row.model_copy(
            update={
                "available_at": row.available_at + timedelta(minutes=1),
                "source_snapshot_hash": "later",
            }
        ).model_dump(mode="json"),
    )
    _save(other, document)
    assert ingest(tmp_path, model_ids=("a" * 64, "b" * 64)) == 1
    assert len(list((tmp_path / "learning").glob("*.json"))) == 1
    assert len(list((tmp_path / "learning" / "provenance").glob("*.json"))) == 2
    other.unlink()
    document["label"].update(raw_return=0.003, excess_return=0.003)
    _save(other, document)
    with pytest.raises(ValueError, match="conflict"):
        ingest(tmp_path, model_ids=("a" * 64, "b" * 64))


def model(created_at):
    return dict(
        model_target="raw_return",
        target_contract="five-minute-raw-dia-return-shadow-v1",
        family="ridge",
        order_authority=False,
        created_at=created_at.isoformat(),
        artifact=dict(
            family="ridge",
            feature_names=FEATURE_NAMES,
            means=[0] * 10,
            scales=[1] * 10,
            coefficients=[0] * 10,
            intercept=0.002,
            alpha=1.0,
            training_mse=0.0,
            seed=0,
            algorithm_version="ridge-cholesky-v1",
        ),
    )


def test_shadow_is_fresh_deduplicated_and_resolves_only_real_elapsed_label(tmp_path):
    path, _, row = source(tmp_path)
    candidate = model(row.features.decision_at - timedelta(minutes=1))
    args = dict(
        source=tmp_path / "source",
        output=tmp_path / "shadow",
        models=[candidate],
        guard=lambda *_: True,
        model_ids=("a" * 64,),
        clock_healthy=True,
    )
    # Outcome can't be read early: withhold genuine later receipt until target elapses.
    saved = path.read_bytes()
    path.unlink()
    decision = row.features.decision_at + timedelta(seconds=75)
    assert shadow_tick(**args, now=decision) == dict(predictions=1, outcomes=0)
    assert shadow_tick(**args, now=decision) == dict(predictions=0, outcomes=0)
    path.write_bytes(saved)
    assert shadow_tick(**args, now=NOW) == dict(predictions=0, outcomes=1)
    assert shadow_tick(**args, now=NOW) == dict(predictions=0, outcomes=0)


@pytest.mark.parametrize("reason", ["clock", "late", "new-model"])
def test_shadow_never_backfills_or_uses_unverified_clock(tmp_path, reason):
    path, _, row = source(tmp_path)
    path.unlink()
    created = row.features.decision_at - timedelta(minutes=1)
    now = row.features.decision_at + timedelta(seconds=75)
    if reason == "new-model":
        created = now
    if reason == "late":
        now += timedelta(minutes=2)
    result = shadow_tick(
        source=tmp_path / "source",
        output=tmp_path / "shadow",
        models=[model(created)],
        now=now,
        guard=lambda *_: True,
        model_ids=("a" * 64,),
        clock_healthy=reason != "clock",
    )
    assert result == dict(predictions=0, outcomes=0)


@pytest.mark.parametrize(
    "change", ["before-decision", "authority", "score", "contract", "identity"]
)
def test_archived_shadow_provenance_is_revalidated_before_outcomes(tmp_path, change):
    path, _, row = source(tmp_path)
    saved = path.read_bytes()
    path.unlink()
    args = dict(
        source=tmp_path / "source",
        output=tmp_path / "shadow",
        models=[model(row.features.decision_at - timedelta(minutes=1))],
        guard=lambda *_: True,
        model_ids=("a" * 64,),
        clock_healthy=True,
    )
    shadow_tick(**args, now=row.features.decision_at + timedelta(seconds=75))
    archived = next((tmp_path / "shadow" / "predictions").glob("*/*.json"))
    document = _read(archived)
    if change == "before-decision":
        document["recorded_at"] = (row.features.decision_at - timedelta(seconds=1)).isoformat()
    if change == "authority":
        document["order_authority"] = True
    if change == "score":
        document["score"] = 0.003
    if change == "contract":
        document["target_contract"] = "other"
    if change == "identity":
        document["feature_identity"] = "x"
    archived.unlink()
    _save(archived, document)
    path.write_bytes(saved)
    with pytest.raises(ValueError, match="provenance"):
        shadow_tick(**args, now=NOW)


def test_slow_work_cannot_backdate_a_forecast(tmp_path):
    path, _, row = source(tmp_path)
    path.unlink()
    result = shadow_tick(
        source=tmp_path / "source",
        output=tmp_path / "shadow",
        models=[model(row.features.decision_at - timedelta(minutes=1))],
        guard=lambda *_: True,
        model_ids=("a" * 64,),
        clock_healthy=True,
        now=row.features.decision_at + timedelta(seconds=75),
        clock=lambda: row.features.target_at + timedelta(seconds=1),
    )
    assert result == dict(predictions=0, outcomes=0)


def test_successful_advancing_clock_records_actual_later_time(tmp_path):
    path, _, row = source(tmp_path)
    path.unlink()
    started = row.features.decision_at + timedelta(seconds=75)
    samples = iter([started + timedelta(seconds=1), started + timedelta(seconds=2)])
    result = shadow_tick(
        source=tmp_path / "source",
        output=tmp_path / "shadow",
        models=[model(row.features.decision_at - timedelta(minutes=1))],
        guard=lambda *_: True,
        model_ids=("a" * 64,),
        clock_healthy=True,
        now=started,
        clock=lambda: next(samples),
    )
    assert result == dict(predictions=1, outcomes=0)
    archived = next((tmp_path / "shadow" / "predictions").glob("*/*.json"))
    assert _read(archived)["recorded_at"] == (started + timedelta(seconds=1)).isoformat()
