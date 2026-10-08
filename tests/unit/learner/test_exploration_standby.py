"""Isolated selected-policy proof; no actual commissioning, accounts or broker orders."""

import hashlib
import json
from pathlib import Path

import pytest

from quantbot.learner import standby
from quantbot.learner.experiment_coordinator import TickReceipt
from quantbot.learner.experiment_launch import ExperimentLaunchConfig
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.exploration_execution import ExplorationCommission
from quantbot.learner.exploration_launch import ExplorationLaunchConfig
from quantbot.research.manifest import content_id
from tests.unit.learner.test_paper_exploration import exploration_context as exploration_context


@pytest.fixture
async def selected(exploration_context, tmp_path):
    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.storage import StorageRepository
    from quantbot.storage.schema import promotion_state

    kwargs, broker, _ = exploration_context
    active, original = tmp_path / "active", tmp_path / "original"
    active.mkdir()
    original.mkdir()
    # Real-proof schema in an isolated fixture; all provenance remains test-generated.
    manifest = kwargs["manifest"].model_copy(update={"synthetic": False})
    policy = kwargs["exploration"].model_copy(
        update={"synthetic": False, "manifest_hash": content_id(manifest)}
    )
    binding = kwargs["binding"].model_copy(
        update={"model_id": policy.policy_id, "artifact_hash": content_id(policy)}
    )
    identity = binding.strategy_identity(git_commit="e" * 40, deployed_at=policy.recorded_at)
    source = ExperimentLaunchConfig(
        root=active,
        store=active / "experiment.db",
        manifest_path=tmp_path / "manifest.json",
        operations_database=kwargs["database"].path,
        declaration_database=tmp_path / "declarations.db",
        clock_receipt=tmp_path / "clock.json",
        code_commit=identity.git_commit,
        manifest_hash=content_id(manifest),
    )
    source.manifest_path.write_text(manifest.model_dump_json(), encoding="utf-8")
    source_path = tmp_path / "source.json"
    source_path.write_text(source.model_dump_json(), encoding="utf-8")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    launch = ExplorationLaunchConfig(
        source_config=source_path,
        source_config_hash=hashlib.sha256(source_path.read_bytes()).hexdigest(),
        policy_path=policy_path,
        policy_id=policy.policy_id,
        binding_hash=binding.configuration_hash,
        policy=kwargs["policy"],
    )
    launch_path = tmp_path / "entry.json"
    launch_path.write_text(launch.model_dump_json(), encoding="utf-8")
    store = ExperimentStore(source.store, create=True)
    store.append(manifest, idempotency_key="manifest")
    store.append(policy, idempotency_key="policy")
    commission = ExplorationCommission(
        experiment_id=manifest.experiment_id,
        policy_id=policy.policy_id,
        manifest_hash=content_id(manifest),
        binding_hash=binding.configuration_hash,
        code_commit=identity.git_commit,
        recorded_at=policy.recorded_at,
        source_authorization_ref="chat:synthetic-test-scope",
    )
    store.append(commission, idempotency_key="commission")
    receipt = TickReceipt(
        experiment_id=manifest.experiment_id,
        recorded_at=broker.now,
        tick_id="a" * 64,
        input_hash="b" * 64,
        status="NORMAL_WAIT",
        reason="MARKET_CLOSED",
        next_expected_event="NEXT_EXCHANGE_SESSION",
        next_wakeup_at=broker.now,
        clock_verified=True,
        synthetic=False,
    )
    store.append(receipt, idempotency_key="tick")
    target = dict(
        receipt.model_dump(mode="json"),
        code_commit=identity.git_commit,
        config_hash=content_id(source.model_dump(mode="json")),
    )
    (active / "receipt.json").write_text(json.dumps(target), encoding="utf-8")
    proof = dict(
        schema="PAPER_EXPLORATION_SELECTION_V1",
        launch_config=str(launch_path),
        launch_hash=hashlib.sha256(launch_path.read_bytes()).hexdigest(),
        commission_id=commission.commission_id,
        binding=binding.model_dump(mode="json"),
        identity=identity.model_dump(mode="json"),
    )
    (active / "exploration-selection.json").write_text(json.dumps(proof), encoding="utf-8")
    with kwargs["database"].transaction() as session:
        repo = StorageRepository(session)
        ledger = PromotionLedger(session)
        old = next(
            row["strategy_id"]
            for row in session.execute(promotion_state.select()).mappings()
            if row["stage"] != "SHADOW"
        )
        ledger.demote(
            old, Stage.SHADOW, reason="isolated selection test", actor="test", now=broker.now
        )
        repo.save_strategy_deployment(identity)
        ledger.enter(
            PromotionState(
                strategy_id=identity.strategy_id,
                strategy_version=identity.version,
                configuration_hash=identity.configuration_hash,
                stage=Stage.PAPER_OBSERVATION,
                reason="isolated selection test",
                actor="test",
                updated_at=broker.now,
            )
        )
    record = standby.EntryStandby(
        schema="ELROND_ENTRY_STANDBY_V1",
        original_strategy_id=old,
        active_strategy_id=identity.strategy_id,
        active_commit=identity.git_commit,
        paper_configuration_hash=content_id(proof),
        active_root=active,
        operational_database=source.operations_database,
        recorded_at=policy.recorded_at,
        active_contract="PAPER_EXPLORATION",
        authority="EXPLICIT_OPERATOR_PAPER_PILOT_SWITCH",
    )
    (original / "entry-standby.json").write_text(
        record.model_dump_json(by_alias=True), encoding="utf-8"
    )
    return original, active, source, broker.now, target, proof, record


async def test_selected_exploration_uses_actual_journal_and_remains_read_only(selected):
    root, active, source, now, *_ = selected
    before = source.operations_database.read_bytes(), source.store.read_bytes()
    result = standby.standby_status(root, now=now)
    assert result["status"] == "STANDBY"
    assert result["blockers"] == ["MARKET_CLOSED"]
    assert result["trading_enabled"] is False and result["orders_submitted"] == 0
    assert not (active / "worker.json").exists()
    assert before == (source.operations_database.read_bytes(), source.store.read_bytes())


@pytest.mark.parametrize("failure", ["unjournaled", "clock", "pin", "proof"])
async def test_selected_exploration_cannot_invent_completion_or_consent(selected, failure):
    root, active, _, now, target, proof, _ = selected
    if failure == "proof":
        proof["commission_id"] = "c" * 64
        (active / "exploration-selection.json").write_text(json.dumps(proof), encoding="utf-8")
    else:
        target[
            {"unjournaled": "tick_id", "clock": "clock_verified", "pin": "code_commit"}[failure]
        ] = False if failure == "clock" else "f" * (40 if failure == "pin" else 64)
        (active / "receipt.json").write_text(json.dumps(target), encoding="utf-8")
    with pytest.raises(standby.StandbyVerificationError):
        standby.standby_status(root, now=now)


@pytest.mark.parametrize("cycle_status", ["HALTED", "BLOCKED", "ERROR"])
async def test_failed_actual_cycle_never_becomes_closed_expected_wait(selected, cycle_status):
    from datetime import timedelta

    from quantbot.console.market_status import MarketStatus
    from quantbot.learner.experiment_coordinator import ExplorationCycleReceipt
    from quantbot.operations.cycle import CycleResult

    root, _, source, at, target, proof, _ = selected
    store = ExperimentStore(source.store)
    cycle = ExplorationCycleReceipt(
        experiment_id=target["experiment_id"],
        recorded_at=at,
        tick_id=target["tick_id"],
        commission_id=proof["commission_id"],
        policy_id=proof["binding"]["model_id"],
        result=CycleResult(
            run_id="synthetic-blocked",
            status=cycle_status,
            reasons=("SYNTHETIC_RISK_FAILURE",),
            signals_evaluated=0,
            orders_submitted=0,
            started_at=at,
            finished_at=at,
        ),
    )
    store.append(cycle, idempotency_key="synthetic-cycle")
    now = at + timedelta(minutes=11)
    market = MarketStatus(
        checked_at=now,
        valid_until=now + timedelta(seconds=30),
        reported_open=False,
        expected_open=False,
        clock_status="HEALTHY",
        closed_session_consistent=True,
    )
    with pytest.raises(standby.StandbyVerificationError, match="STANDBY_ENTRY_HEARTBEAT_STALE"):
        standby.observation_standby_status(root, now=now, market=market)


async def test_pending_recovery_uses_complete_history_when_current_cohort_is_partial(
    selected,
    monkeypatch,
):
    from types import SimpleNamespace

    from quantbot.learner import exploration_launch as module
    from quantbot.learner.experiment_observation import build_observation_batch
    from tests.fixtures.adaptive_paper import snapshot

    _, _, source, now, _, proof, _ = selected
    launch = module.ExplorationLaunchConfig.model_validate_json(
        Path(proof["launch_config"]).read_bytes())
    manifest = module.load_manifest(source)
    capture = snapshot()
    now = capture.received_at
    batch = build_observation_batch(manifest, capture, now=now)
    store = ExperimentStore(source.store)
    store.append(batch, idempotency_key="historical-complete")
    store.enqueue_intent(manifest.experiment_id, intent_id="synthetic-pending", document="{}")
    partial = batch.model_copy(update={"features": batch.features[:-1]})
    settings = SimpleNamespace(
        TRADING_MODE=SimpleNamespace(value="PAPER"),
        BROKER_ENVIRONMENT=SimpleNamespace(value="PAPER"),
        LIVE_TRADING_ACKNOWLEDGED=False,
        EXPECTED_ACCOUNT_ID="synthetic",
    )
    monkeypatch.setattr(module, "clock_healthy", lambda *args: True)
    monkeypatch.setattr(module, "read_entry_ownership", lambda: True)

    def check_before_runtime(_source, _manifest, chosen, _captures):
        assert chosen == batch and module.complete_cohort(manifest, chosen)
        raise RuntimeError("synthetic-recovery-selected-before-runtime")

    monkeypatch.setattr(module, "resolve_sources", check_before_runtime)
    with pytest.raises(RuntimeError, match="synthetic-recovery-selected"):
        await module.prepare_exploration_tick(
            launch, source, settings, store, manifest, partial, (), now
        )
