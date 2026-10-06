from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest

from tests.fixtures.adaptive_paper import OPEN, manifest, snapshot


def observation():
    return import_module("quantbot.learner.experiment_observation")


def test_seven_bars_and_120_second_bound():
    module, source = observation(), snapshot()
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    assert len(batch.features) == 6
    assert all(row.status == "AVAILABLE" for row in batch.coverage)
    assert batch.synthetic
    exact = module.build_observation_batch(manifest(), source, now=OPEN + timedelta(minutes=37))
    assert len(exact.features) == 6
    late = module.build_observation_batch(manifest(), source,
                                          now=OPEN + timedelta(minutes=37, seconds=1))
    assert late.features == ()


def test_missing_member_rejected_without_imputation():
    module = observation()
    source = snapshot(missing=(("SYN0", 6),))
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    assert len(batch.features) == 5
    assert "SYN0" not in {row.symbol for row in batch.features}
    assert batch.coverage[0].status == "MISSING"
    source = snapshot(missing=(("SPY", 6),))
    assert module.build_observation_batch(manifest(), source, now=source.received_at).features == ()


def test_early_close_has_no_full_horizon():
    module = observation()
    source = snapshot(minute=40, session_minutes=65)
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    assert batch.features == ()
    assert {row.status for row in batch.coverage} == {"NO_ENTRY_WINDOW"}


def test_dst_open_uses_exchange_calendar():
    module = observation()
    winter_open = datetime(2026, 11, 2, 14, 30, tzinfo=UTC)
    source = snapshot(opened=winter_open)
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    assert len(batch.features) == 6
    assert batch.features[0].feature_at == winter_open + timedelta(minutes=35)


def test_labels_include_vetoed_predictions():
    module, source = observation(), snapshot()
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    later = snapshot(minute=65)
    labels = module.resolve_batch_labels(batch, later, now=later.received_at)
    assert len(labels) == 6
    assert all(row.available_at == later.received_at for row in labels)
    assert labels[0].raw_return == pytest.approx(112 / 106 - 1)
    assert module.resolve_batch_labels(batch, source, now=source.received_at) == ()


def test_future_receipt_and_wrong_feed_refused():
    module, source = observation(), snapshot()
    with pytest.raises(ValueError, match="FUTURE"):
        module.build_observation_batch(manifest(), source,
                                      now=source.received_at - timedelta(seconds=1))
    wrong = source.model_copy(update={"source_id": "alpaca:iex:raw:1min"})
    with pytest.raises(ValueError):
        module.build_observation_batch(manifest(), wrong, now=source.received_at)


def test_warmup_and_closed_are_expected_waits():
    module, source = observation(), snapshot(minute=30)
    batch = module.build_observation_batch(manifest(), source, now=source.received_at)
    assert batch.features == ()
    assert {row.status for row in batch.coverage} == {"WARMUP"}
    batch = module.build_observation_batch(manifest(), source,
                                          now=OPEN + timedelta(hours=7))
    assert {row.status for row in batch.coverage} == {"CLOSED_SESSION"}
