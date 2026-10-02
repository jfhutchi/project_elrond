"""Explicit standby must stay read-only and fail closed on lost pilot evidence."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.learner import standby
from quantbot.learner.paper import PaperModelBinding
from quantbot.research.manifest import content_id


@pytest.fixture
def case(tmp_path, monkeypatch):
    now = datetime(2026, 10, 1, 14, tzinfo=UTC)
    original = tmp_path / "original"
    active = tmp_path / "active"
    original.mkdir()
    active.mkdir()
    op = tmp_path / "operational.db"
    model = "a" * 64
    pin = "b" * 40
    binding = PaperModelBinding(
        model_id=model, artifact_hash="c" * 64, code_hash="d" * 64, execution_config_hash="e" * 64
    )
    identity = binding.strategy_identity(git_commit=pin, deployed_at=now)
    old = "learner-" + "f" * 64
    config = dict(
        mode="PAPER",
        experimental_symbol="DIA",
        model_id=model,
        operational_database=str(op),
        risk_per_trade_bps=25,
        max_position_value_bps=500,
        policy=dict(exclusive_learner_entry=True, stop_loss_bps=100, inherited={"proof": True}),
    )
    (active / "paper-execution.json").write_text(json.dumps(config))
    paper = dict(
        binding=binding.model_dump(),
        identity=identity.model_dump(mode="json"),
        status="WAITING",
        blockers=["PAPER_CURRENT_COHORT_UNAVAILABLE"],
    )
    receipt = dict(code_commit=pin, finished_at=now.isoformat(), paper=paper)
    (active / "worker.json").write_text(json.dumps(receipt))
    record = dict(
        schema="ELROND_ENTRY_STANDBY_V1",
        original_strategy_id=old,
        active_strategy_id=identity.strategy_id,
        active_commit=pin,
        paper_configuration_hash=content_id(config),
        active_root=str(active),
        operational_database=str(op),
        recorded_at=now.isoformat(),
        authority="EXPLICIT_OPERATOR_PAPER_PILOT_SWITCH",
    )
    (original / "entry-standby.json").write_text(json.dumps(record))
    with sqlite3.connect(op) as c:
        c.execute(
            "CREATE TABLE promotion_state(strategy_id TEXT,stage TEXT,"
            "strategy_version TEXT,configuration_hash TEXT)"
        )
        c.executemany(
            "INSERT INTO promotion_state VALUES (?,?,?,?)",
            [
                (old, "SHADOW", "old", "old"),
                (
                    identity.strategy_id,
                    "PAPER_OBSERVATION",
                    identity.version,
                    identity.configuration_hash,
                ),
            ],
        )
        c.execute(
            "CREATE TABLE strategy_deployments(strategy_id TEXT,version TEXT,"
            "git_commit TEXT,configuration_hash TEXT,deployment_timestamp TEXT)"
        )
        c.execute(
            "INSERT INTO strategy_deployments VALUES (?,?,?,?,?)",
            (
                identity.strategy_id,
                identity.version,
                pin,
                identity.configuration_hash,
                now.isoformat(),
            ),
        )
    monkeypatch.setattr(standby, "consent_recorded", lambda *_args, **_kwargs: True)
    return original, active, op, now, record, config, receipt


def test_verified_standby_does_not_grant_authority_or_write_database(case):
    original, _, op, now, *_ = case
    before = op.read_bytes()
    result = standby.standby_status(original, now=now)
    assert result["status"] == "STANDBY" and result["promotion"] == "SHADOW"
    assert not result["trading_enabled"] and result["orders_submitted"] == 0
    assert result["blockers"] == ["PAPER_CURRENT_COHORT_UNAVAILABLE"]
    assert op.read_bytes() == before


@pytest.mark.parametrize(
    "failure",
    [
        "original-active",
        "target-demoted",
        "lost-deployment",
        "configuration-changed",
        "stale-heartbeat",
        "wrong-pin",
        "target-missing-binding",
        "missing-consent",
    ],
)
def test_invalid_standby_cannot_mask_missing_evidence(case, monkeypatch, failure):
    original, active, op, now, record, config, receipt = case
    with sqlite3.connect(op) as c:
        if failure == "original-active":
            c.execute(
                "UPDATE promotion_state SET stage='PAPER_OBSERVATION' WHERE strategy_id=?",
                (record["original_strategy_id"],),
            )
        elif failure == "target-demoted":
            c.execute(
                "UPDATE promotion_state SET stage='SHADOW' WHERE strategy_id=?",
                (record["active_strategy_id"],),
            )
        elif failure == "lost-deployment":
            c.execute("DELETE FROM strategy_deployments")
    if failure == "configuration-changed":
        config["max_position_value_bps"] = 600
        (active / "paper-execution.json").write_text(json.dumps(config))
    elif failure == "stale-heartbeat":
        receipt["finished_at"] = (now - timedelta(minutes=11)).isoformat()
    elif failure == "wrong-pin":
        receipt["code_commit"] = "0" * 40
    elif failure == "target-missing-binding":
        receipt["paper"].pop("binding")
    elif failure == "missing-consent":
        monkeypatch.setattr(standby, "consent_recorded", lambda *_args, **_kwargs: False)
    (active / "worker.json").write_text(json.dumps(receipt))
    with pytest.raises((ValueError, KeyError)):
        standby.standby_status(original, now=now)


def test_real_target_setup_failure_remains_visible(case):
    original, active, _, now, _, _, receipt = case
    receipt["paper"]["blockers"] = ["PAPER_EXECUTION_NOT_CONFIGURED"]
    (active / "worker.json").write_text(json.dumps(receipt))
    assert standby.standby_status(original, now=now)["blockers"] == [
        "PAPER_EXECUTION_NOT_CONFIGURED"
    ]


def test_progress_labels_verified_standby_and_preserves_real_setup_alerts(tmp_path):
    from quantbot.learner.progress import monitor_progress
    from quantbot.operations.alerts import AlertStore

    now = datetime(2026, 10, 1, 14, tzinfo=UTC)
    receipt = dict(
        status="BLOCKED",
        paper={"status": "STANDBY"},
        operator_consent="RECORDED",
        blockers=["FORECAST_LOOKBACK_WARMUP"],
        intraday={"progress_expected": False},
    )
    with AlertStore(tmp_path / "alerts.db") as store:
        result = monitor_progress(store, receipt, now=now)
        assert result["paper_status"] == "STANDBY_FORECASTS_ACTIVE"
        assert not result["active_alerts"]
        receipt["blockers"] = ["PAPER_EXECUTION_NOT_CONFIGURED"]
        result = monitor_progress(store, receipt, now=now)
        assert result["paper_status"] == "MISSING_COMPONENT"
        assert any(a["code"] == "PAPER_EXECUTION_NOT_CONFIGURED" for a in result["active_alerts"])


@pytest.mark.parametrize("valid", [True, False])
@pytest.mark.parametrize("contract", [None, "five-minute-price-alpha-v1"])
@pytest.mark.parametrize("sentinel_directory", [False, True])
def test_worker_standby_preserves_forecasts_and_never_calls_entry(
    tmp_path, monkeypatch, valid, contract, sentinel_directory
):
    from quantbot.config import Settings
    from quantbot.learner.worker import run_cycle
    from tests.unit.learner.test_worker import worker_store

    root, database = worker_store(tmp_path)
    for name in ("forward.json", "paper-execution.json", "entry-standby.json"):
        (root / name).write_text("{}")
    if contract:
        (root / "paper-execution.json").write_text(json.dumps({"contract": contract}))
    if sentinel_directory:
        (root / "entry-standby.json").unlink()
        (root / "entry-standby.json").mkdir()

    async def forecast(*_args, **_kwargs):
        return dict(
            blockers=[
                "PAPER_PROMOTION_REQUIRED",
                "PAPER_EXECUTION_NOT_CONFIGURED",
                "FORECAST_LOOKBACK_WARMUP",
            ],
            progress_expected=False,
        )

    async def unexpected_entry(*_args, **_kwargs):
        pytest.fail("standby must never invoke original entry worker")

    def status(*_args, **_kwargs):
        if not valid:
            raise ValueError("synthetic invalid standby")
        return dict(
            status="STANDBY",
            trading_enabled=False,
            promotion="SHADOW",
            blockers=["PAPER_CURRENT_COHORT_UNAVAILABLE"],
            cleared_gate_codes=[
                "PAPER_PROMOTION_REQUIRED",
                "PAPER_EXECUTION_NOT_CONFIGURED",
                "PAPER_OBSERVATION_NOT_EARNED",
            ],
        )

    monkeypatch.setattr("quantbot.learner.forward.run_forward_cycle", forecast)
    monkeypatch.setattr("quantbot.learner.execution_worker.execute_paper_tick", unexpected_entry)
    monkeypatch.setattr(standby, "standby_status", status)
    try:
        result = run_cycle(root, code_commit="a" * 40, settings=Settings())
        assert result["intraday"]["blockers"] == [
            "PAPER_PROMOTION_REQUIRED",
            "PAPER_EXECUTION_NOT_CONFIGURED",
            "FORECAST_LOOKBACK_WARMUP",
        ]
        assert not result["trading_enabled"]
        assert "PAPER_PROMOTION_REQUIRED" not in result["blockers"]
        assert "PAPER_EXECUTION_NOT_CONFIGURED" not in result["blockers"]
        if valid:
            assert result["progress"]["paper_status"] == "STANDBY_FORECASTS_ACTIVE"
        else:
            assert "OUTCOME_PROCESSING_FAILED" not in result["blockers"]
            assert result["safe_failure_code"] == "STANDBY_VERIFICATION_FAILED"
            assert any(
                a["code"] == "STANDBY_VERIFICATION_FAILED"
                for a in result["progress"]["active_alerts"]
            )
    finally:
        database.close()


@pytest.mark.parametrize("change", [None, "trading", "orders", "target", "status", "promotion"])
def test_receipt_reader_accepts_only_verified_no_order_standby(case, change):
    from quantbot.learner.receipts import valid_worker_authority

    original, _, _, now, *_ = case
    # Use a fixed clock through the helper to test receipt shape independently of wall time.
    verified = standby.standby_status(original, now=now)
    from unittest.mock import patch

    receipt = dict(promotion="SHADOW", trading_enabled=False, paper=verified.copy())
    if change == "trading":
        receipt["trading_enabled"] = True
    elif change == "orders":
        receipt["paper"]["orders_submitted"] = 1
    elif change == "target":
        receipt["paper"]["active_strategy_id"] = "learner-" + "0" * 64
    elif change == "status":
        receipt["paper"]["status"] = "SUCCEEDED"
    elif change == "promotion":
        receipt["paper"]["promotion"] = "PAPER_OBSERVATION"
    with patch.object(standby, "standby_status", return_value=verified):
        assert valid_worker_authority(original, receipt) is (change is None)


def test_receipt_reader_rejects_unverified_standby(case, monkeypatch):
    from quantbot.learner.receipts import valid_worker_authority

    original, _, _, now, *_ = case
    receipt = dict(
        promotion="SHADOW", trading_enabled=False, paper=standby.standby_status(original, now=now)
    )

    def invalid(*_args, **_kwargs):
        raise ValueError("lost standby proof")

    monkeypatch.setattr(standby, "standby_status", invalid)
    assert not valid_worker_authority(original, receipt)


def test_configuration_refusal_has_precise_safe_code(case):
    original, active, _, now, _, config, _ = case
    config["max_position_value_bps"] = 600
    (active / "paper-execution.json").write_text(json.dumps(config))
    with pytest.raises(standby.StandbyVerificationError) as error:
        standby.standby_status(original, now=now)
    assert error.value.code == "STANDBY_CONFIGURATION_MISMATCH"


def test_refresh_metadata_requires_exact_deployment_and_does_not_claim_freshness(case, tmp_path):
    import hashlib

    original, active, op, now, record, config, receipt = case
    receipt["finished_at"] = (now - timedelta(minutes=11)).isoformat()
    (active / "worker.json").write_text(json.dumps(receipt))
    record["paper_configuration_hash"] = "0" * 64
    (original / "entry-standby.json").write_text(json.dumps(record))
    before = (original / "entry-standby.json").read_bytes()
    op_before = op.read_bytes()
    result = standby.refresh_standby_binding(
        original,
        expected_commit=receipt["code_commit"],
        expected_model_id=config["model_id"],
        expected_previous_hash=hashlib.sha256(before).hexdigest(),
        backup_directory=tmp_path / "backup",
        now=now,
    )
    assert result["metadata_bound"] is True and result["runtime_health_verified"] is False
    assert result["current_status_code"] == "STANDBY_ENTRY_HEARTBEAT_STALE"
    assert op.read_bytes() == op_before
    assert (tmp_path / "backup" / "entry-standby-before.json").read_bytes() == before
    new = json.loads((original / "entry-standby.json").read_text())
    assert new["paper_configuration_hash"] == content_id(config)
    assert new["authority"] == record["authority"]
    with pytest.raises(standby.StandbyVerificationError) as error:
        standby.standby_status(original, now=now)
    assert error.value.code == "STANDBY_ENTRY_HEARTBEAT_STALE"


@pytest.mark.parametrize(
    "failure", ["wrong-commit", "wrong-model", "changed-before", "stage", "consent"]
)
def test_refresh_refuses_invalid_proof_without_metadata_write(case, tmp_path, monkeypatch, failure):
    import hashlib

    original, _, op, now, _, config, receipt = case
    before = (original / "entry-standby.json").read_bytes()
    pin = receipt["code_commit"] if failure != "wrong-commit" else "0" * 40
    model = config["model_id"] if failure != "wrong-model" else "0" * 64
    old_hash = hashlib.sha256(before).hexdigest() if failure != "changed-before" else "0" * 64
    if failure == "stage":
        with sqlite3.connect(op) as c:
            c.execute("UPDATE promotion_state SET stage='SHADOW'")
    elif failure == "consent":
        monkeypatch.setattr(standby, "consent_recorded", lambda *_a, **_k: False)
    with pytest.raises((ValueError, standby.StandbyVerificationError)):
        standby.refresh_standby_binding(
            original,
            expected_commit=pin,
            expected_model_id=model,
            expected_previous_hash=old_hash,
            backup_directory=tmp_path / "backup",
            now=now,
        )
    assert (original / "entry-standby.json").read_bytes() == before


@pytest.mark.parametrize(
    "field", ["strategy_version", "configuration_hash", "deployment_timestamp"]
)
def test_full_registry_identity_must_match(case, field):
    original, _, op, now, record, *_ = case
    with sqlite3.connect(op) as connection:
        if field == "deployment_timestamp":
            connection.execute(
                "UPDATE strategy_deployments SET deployment_timestamp=?",
                ((now - timedelta(seconds=1)).isoformat(),),
            )
        else:
            connection.execute(
                f"UPDATE promotion_state SET {field}=? WHERE strategy_id=?",
                ("wrong", record["active_strategy_id"]),
            )
    with pytest.raises(standby.StandbyVerificationError):
        standby.standby_status(original, now=now)


def test_refresh_preserves_concurrent_operator_metadata(case, tmp_path, monkeypatch):
    import hashlib

    original, _, _, now, _, config, receipt = case
    path = original / "entry-standby.json"
    before = path.read_bytes()
    concurrent = before + b"\n"
    validate = standby._validated_selection

    def changed(*args, **kwargs):
        result = validate(*args, **kwargs)
        path.write_bytes(concurrent)
        return result

    monkeypatch.setattr(standby, "_validated_selection", changed)
    with pytest.raises(standby.StandbyVerificationError):
        standby.refresh_standby_binding(
            original,
            expected_commit=receipt["code_commit"],
            expected_model_id=config["model_id"],
            expected_previous_hash=hashlib.sha256(before).hexdigest(),
            backup_directory=tmp_path / "backup",
            now=now,
        )
    assert path.read_bytes() == concurrent


def test_unknown_error_text_cannot_be_published():
    error = standby.StandbyVerificationError("synthetic sensitive provider details")
    assert str(error) == error.code == "STANDBY_VERIFICATION_FAILED"
