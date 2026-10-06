"""Immutable experimental authority; all identities/data here are synthetic."""

from datetime import UTC, datetime, timedelta
from importlib import import_module

import pytest
from pydantic import ValidationError


def contract():
    return import_module("quantbot.learner.experiment_contract")


def manifest_data():
    return {
        "experiment_id": "synthetic-experiment",
        "registered_at": datetime(2026, 10, 6, tzinfo=UTC),
        "prospective_start_at": datetime(2026, 10, 6, 13, 30, tzinfo=UTC),
        "training_start": datetime(2026, 2, 2, tzinfo=UTC),
        "training_end": datetime(2026, 3, 13, 21, tzinfo=UTC),
        "validation_start": datetime(2026, 3, 16, tzinfo=UTC),
        "validation_end": datetime(2026, 3, 31, 21, tzinfo=UTC),
        "roster": tuple(
            {
                "symbol": f"SYN{index}",
                "asset_id": f"synthetic-asset-{index}",
                "asset_class": "EQUITY",
                "metadata_source": "https://synthetic.invalid/assets",
                "metadata_hash": "a" * 64,
                "coverage_hash": "b" * 64,
                "verified_at": datetime(2026, 10, 5, tzinfo=UTC),
            }
            for index in range(6)
        ),
        "code_hash": "c" * 64,
        "policy_hash": "d" * 64,
        "synthetic": True,
    }


class Guard:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.ranges = []

    def permits(self, start, end):
        self.ranges.append((start, end))
        return self.allowed


def test_manifest_fixes_limits_and_budget():
    c = contract()
    manifest = c.ExperimentManifest(**manifest_data())
    assert manifest.fit_budget == 6
    assert manifest.maximum_successors == 4
    assert manifest.risk.maximum_new_positions == 1
    assert manifest.risk.maximum_position_bps == 500
    assert manifest.risk.maximum_entry_risk_bps == 25
    assert manifest.risk.stop_bps == 100
    assert manifest.risk.horizon_minutes == 30
    assert manifest.round_trip_cost_bps == 10
    guard = Guard()
    assert len(c.validate_manifest(manifest, guard=guard)) == 64
    assert len(guard.ranges) == 2
    with pytest.raises(ValidationError):
        manifest.fit_budget = 7


def test_guard_precedes_archive_open():
    c = contract()
    manifest = c.ExperimentManifest(**manifest_data())
    opened = []
    with pytest.raises(ValueError, match="PROTECTED"):
        c.guarded_read(manifest, guard=Guard(False), loader=lambda: opened.append(True))
    assert opened == []


@pytest.mark.parametrize("field,value", [
    ("mode", "LIVE"), ("target", "excess_return"), ("fit_budget", 7),
    ("round_trip_cost_bps", 9), ("maximum_successors", 5), ("ridge_alpha", 2),
])
def test_invalid_target_or_live_mode_rejected(field, value):
    c = contract()
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(manifest_data() | {field: value}))


@pytest.mark.parametrize("field,value", [
    ("maximum_new_positions", 2), ("maximum_position_bps", 501),
    ("maximum_entry_risk_bps", 26), ("stop_bps", 101), ("horizon_minutes", 31),
])
def test_risk_cannot_expand(field, value):
    c = contract()
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(manifest_data() | {"risk": {field: value}}))


def test_metadata_identity_not_performance_selects_roster():
    c = contract()
    data = manifest_data()
    identities = list(data["roster"])
    identities[0] = identities[0] | {"qualification_basis": "BEST_HISTORICAL_RETURN"}
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(data | {"roster": identities}))
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(data | {"roster": data["roster"][:5]}))
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(data | {"roster": [data["roster"][0]] * 6}))


def test_future_identity_and_overlapping_dates_rejected():
    c = contract()
    data = manifest_data()
    identities = list(data["roster"])
    identities[0] = identities[0] | {"verified_at": data["registered_at"] + timedelta(days=1)}
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(data | {"roster": identities}))
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(data | {"training_end": data["validation_start"]}))


def test_dates_naive_and_cost_family_search_rejected():
    c = contract()
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(manifest_data() | {"registered_at": datetime(2026, 10, 6)}))
    with pytest.raises(ValidationError):
        c.ExperimentManifest(**(manifest_data() | {"families": ["ridge", "quadratic_ridge"]}))
