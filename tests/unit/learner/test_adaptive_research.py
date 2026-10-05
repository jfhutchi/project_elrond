"""Isolated fits, synthetic labels, no active model replacement."""

from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner import adaptive_research as study
from quantbot.learner.five_minute import FiveMinuteFeatureRow, FiveMinuteLabel

START = datetime(2026, 10, 1, 14, tzinfo=UTC)
CUTOFF = datetime(2026, 10, 3, tzinfo=UTC)
NOW = datetime(2026, 10, 5, 17, tzinfo=UTC)


def label(decision, value=0.002):
    row = FiveMinuteFeatureRow(
        symbol="DIA",
        decision_at=decision,
        feature_at=decision,
        target_at=decision + timedelta(minutes=30),
        values=(value,) * 10,
        reference_close=100,
        benchmark_close=100,
        source_snapshot_hash="synthetic",
        receipt_at=decision,
    )
    return FiveMinuteLabel(
        features=row,
        raw_return=value,
        benchmark_return=0,
        excess_return=value,
        available_at=row.target_at,
        source_snapshot_hash="synthetic-outcome",
        evaluation_class="PROSPECTIVE_LOCAL",
    )


def run(tmp_path, **kwargs):
    options = dict(
        root=tmp_path / "experiment",
        training=(label(START), label(START + timedelta(days=1))),
        validation=(label(NOW - timedelta(hours=1)),),
        cutoff=CUTOFF,
        now=NOW,
        guard=lambda *_: True,
        charge=lambda _: None,
    )
    options.update(kwargs)
    return study.fit_shadow_challengers(**options)


def test_two_fits_use_forward_labels_and_report_provisional_validation(tmp_path):
    charges = []
    result = run(tmp_path, charge=charges.append)
    assert len(charges) == len(set(charges)) == 2
    assert len(result["candidates"]) == 2
    assert result["training_forward_labels"] == 2
    assert result["validation_completed_sessions"] == 0
    assert result["validation_status"] == "PROVISIONAL_INSUFFICIENT_FORWARD_SESSIONS"
    assert result["active_model_changed"] is False
    assert all(c["order_authority"] is False for c in result["candidates"])
    with pytest.raises(FileExistsError):
        run(tmp_path, charge=lambda _: pytest.fail("repeated experiment charged"))


@pytest.mark.parametrize(
    "change", ["holdout", "training-lookahead", "validation-overlap", "future-label", "duplicate"]
)
def test_invalid_ranges_never_charge_or_fit(tmp_path, change):
    options = {"charge": lambda _: pytest.fail("invalid study must not consume trial")}
    if change == "holdout":
        options["guard"] = lambda *_: False
    if change == "training-lookahead":
        options["training"] = (label(CUTOFF - timedelta(minutes=5)),)
    if change == "validation-overlap":
        options["validation"] = (label(START),)
    if change == "future-label":
        options["validation"] = (label(NOW),)
    if change == "duplicate":
        options["training"] = (label(START), label(START))
    with pytest.raises(ValueError):
        run(tmp_path, **options)


def test_failure_spends_budget_and_no_retry_or_refit(tmp_path, monkeypatch):
    charges = []

    def broken(*args, **kwargs):
        assert len(charges) == 2
        raise RuntimeError("synthetic engine failure")

    monkeypatch.setattr(study, "fit_estimator", broken)
    with pytest.raises(RuntimeError):
        run(tmp_path, charge=charges.append)
    with pytest.raises(FileExistsError):
        run(tmp_path)
    assert len(charges) == 2


def test_historical_split_uses_target_date_not_later_archive_retrieval():
    historical = label(datetime(2026, 2, 2, 14, tzinfo=UTC))
    historical = historical.model_copy(
        update={
            "features": historical.features.model_copy(
                update={"receipt_at": START, "evaluation_class": "EXPLORATORY_HISTORICAL"}
            ),
            "available_at": START,
            "evaluation_class": "EXPLORATORY_HISTORICAL",
        }
    )
    training, validation = study.chronological_inputs(
        historical=(historical,),
        forward=(label(START), label(NOW - timedelta(hours=1))),
        historical_target_cutoff=datetime(2026, 3, 16, tzinfo=UTC),
        training_cutoff=CUTOFF,
        now=NOW,
    )
    assert len(training) == 2
    assert historical in training
    assert len(validation) == 1


def test_delayed_historical_retrieval_does_not_consume_intervening_holdout(tmp_path):
    old = label(datetime(2026, 2, 2, 14, tzinfo=UTC))
    old = old.model_copy(
        update={
            "features": old.features.model_copy(
                update={"receipt_at": START, "evaluation_class": "EXPLORATORY_HISTORICAL"}
            ),
            "available_at": START,
            "evaluation_class": "EXPLORATORY_HISTORICAL",
        }
    )
    checked = []

    def guard(start, end):
        checked.append((start, end))
        return end < datetime(2026, 4, 1, tzinfo=UTC) or start.date() >= START.date()

    result = run(tmp_path, training=(old, label(START)), guard=guard)
    assert result["engine_calls"] == 2
    assert (old.features.feature_at - timedelta(minutes=35), old.features.target_at) in checked
    assert (old.features.feature_at, old.available_at) not in checked
