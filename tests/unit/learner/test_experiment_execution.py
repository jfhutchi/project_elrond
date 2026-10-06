"""Synthetic broker plus the retained promotion/deadline/coordinator gates."""

from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.experiment_contract import DecisionRecord, ExperimentManifest
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.paper import PromotionGatedSubmitter
from quantbot.learner.paper_deadlines import DeadlineGatedSubmitter
from quantbot.operations.cycle import SubmissionRequest
from tests.unit.learner.test_experiment_contract import manifest_data
from tests.unit.learner.test_five_minute_paper import context as _context


def module():
    return import_module("quantbot.learner.experiment_execution")


@pytest.fixture
async def context(tmp_path):
    async for value in _context.__wrapped__(tmp_path):
        database, broker, runner, supervisor = value
        data = manifest_data()
        data.update(
            registered_at=broker.now - timedelta(days=1),
            prospective_start_at=broker.now - timedelta(minutes=35),
        )
        data["roster"] = tuple(
            dict(
                row,
                symbol="DIA" if i == 0 else row["symbol"],
                verified_at=broker.now - timedelta(days=2),
            )
            for i, row in enumerate(data["roster"])
        )
        manifest = ExperimentManifest(**data)
        store = ExperimentStore(tmp_path / "experiment.db", create=True)
        store.append(manifest, idempotency_key="manifest")
        assert store.acquire_lease(
            manifest.experiment_id, owner="test", now=broker.now, duration=timedelta(minutes=3)
        )
        prediction = runner.predictions[0]
        decision = DecisionRecord(
            experiment_id=manifest.experiment_id,
            recorded_at=broker.now,
            model_id=prediction.model_id,
            features=prediction.features,
            score=prediction.score,
            action="BUY",
            reason="synthetic-only fixture",
            synthetic=True,
        )
        services = SimpleNamespace(runner=runner, deadlines=supervisor)
        lower = PromotionGatedSubmitter(
            database.path,
            DeadlineGatedSubmitter(
                database,
                supervisor.coordinator,
                expected_account_id="paper-1",
                clock=lambda broker=broker: broker.now,
            ),
            binding=runner.binding,
            identity=runner._identity,
        )
        await supervisor.run_once()
        yield manifest, decision, store, services, lower, broker


async def test_live_route_unconstructible(context):
    manifest, decision, store, services, _, broker = context
    broker.environment = "LIVE"
    with pytest.raises(ValueError, match="PAPER_ONLY"):
        await module().prepare_entry(
            manifest, decision, store=store, services=services, simulation=True
        )
    assert not broker.submitted


async def test_synthetic_decision_cannot_enter_real_route(context):
    manifest, decision, store, services, _, broker = context
    with pytest.raises(ValueError, match="EXACT_ACTIVATION"):
        await module().prepare_entry(manifest, decision, store=store, services=services)
    assert not broker.submitted


async def test_pending_buy_and_cost_veto_preserved(context):
    manifest, decision, store, services, _, broker = context
    services.runner.predictions = tuple(
        p.model_copy(update={"score": 0.0005, "cost_status": "BELOW_FIXED_COST_FLOOR"})
        for p in services.runner.predictions
    )
    decision = decision.model_copy(update={"score": 0.0005})
    assert (
        await module().prepare_entry(
            manifest, decision, store=store, services=services, simulation=True
        )
        is None
    )
    assert not broker.submitted


async def test_duplicate_tick_one_client_order_and_durable_lot(context):
    manifest, decision, store, services, lower, broker = context
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    assert intent is not None
    wrapper = module().ExperimentOutboxSubmitter(
        store, services=services, lower=lower, owner="test", clock=lambda: broker.now
    )
    queued = store.pending_intents(manifest.experiment_id)[0]
    request = SubmissionRequest.model_validate_json(queued.document)
    await wrapper.submit(
        intent,
        purpose=request.purpose,
        market_eligible=request.market_eligible,
        drawdown=request.drawdown,
        risk_approved=request.risk_approved,
        vetoes=request.vetoes,
    )
    await wrapper.submit(
        intent,
        purpose=request.purpose,
        market_eligible=request.market_eligible,
        drawdown=request.drawdown,
        risk_approved=request.risk_approved,
        vetoes=request.vetoes,
    )
    assert len(broker.submitted) == 1
    assert (
        await module().prepare_entry(
            manifest, decision, store=store, services=services, simulation=True
        )
        is None
    )
    with services.runner._database.transaction() as session:
        from quantbot.storage.lot_ownership import registrations

        assert len(registrations(session, symbol="DIA", now=broker.now)) == 1


async def test_outstanding_buy_blocks_entry_before_outbox(context):
    from quantbot.storage import StorageRepository
    from tests.unit.learner.test_paper import intent as test_intent

    manifest, decision, store, services, _, broker = context
    runner = services.runner
    with runner._database.transaction() as session:
        StorageRepository(session).create_order_intent(test_intent(runner._identity))
    assert (
        await module().prepare_entry(
            manifest, decision, store=store, services=services, simulation=True
        )
        is None
    )
    assert "PAPER_PENDING_BUY_RISK" in runner.last_reasons
    assert store.pending_intents(manifest.experiment_id) == ()
    assert not broker.submitted


async def test_missing_independent_deadline_blocks_entry(context):
    manifest, decision, store, services, _, broker = context
    broker.now += timedelta(seconds=61)
    assert (
        await module().prepare_entry(
            manifest, decision, store=store, services=services, simulation=True
        )
        is None
    )
    assert "DEADLINE_SUPERVISOR_STALE" in services.runner.last_reasons
    assert not broker.submitted


async def test_post_handoff_capture_uses_actual_receipt_time(context, monkeypatch):
    from dataclasses import replace

    from quantbot.execution.snapshot_capture import capture_account_positions

    manifest, decision, store, services, _, broker = context

    async def delayed_capture(*args, **kwargs):
        captured = await capture_account_positions(*args, **kwargs)
        broker.now += timedelta(seconds=1)
        return replace(captured, received_at=broker.now)

    monkeypatch.setattr(module(), "capture_account_positions", delayed_capture)
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    assert intent is not None
    assert not broker.submitted


async def test_outbox_claim_without_durable_send_is_not_blindly_retried(context):
    manifest, decision, store, services, lower, broker = context
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    request = SubmissionRequest.model_validate_json(
        store.claim_intent(intent.intent_id, owner="test", now=broker.now)
    )
    wrapper = module().ExperimentOutboxSubmitter(
        store, services=services, lower=lower, owner="test", clock=lambda: broker.now
    )
    with pytest.raises(ValueError, match="CLAIMED_SEND_REQUIRES_RECOVERY"):
        await wrapper.submit(
            intent,
            purpose=request.purpose,
            market_eligible=request.market_eligible,
            drawdown=request.drawdown,
            risk_approved=request.risk_approved,
            vetoes=request.vetoes,
        )
    assert not broker.submitted


@pytest.mark.parametrize("found", [False, True])
async def test_uncertain_send_looks_up_same_id_without_another_order(context, found):
    from quantbot.domain import IntentState
    from quantbot.storage import StorageRepository
    from tests.integration.test_execution_coordinator import _accepted

    manifest, decision, store, services, lower, broker = context
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    request = SubmissionRequest.model_validate_json(
        store.claim_intent(intent.intent_id, owner="test", now=broker.now)
    )
    with services.runner._database.transaction() as session:
        repo = StorageRepository(session)
        repo.create_order_intent(intent)
        repo.transition_order_intent(
            intent.intent_id, IntentState.ORDER_CREATED, expected_current=IntentState.RISK_APPROVED
        )
        repo.transition_order_intent(
            intent.intent_id, IntentState.SUBMITTING, expected_current=IntentState.ORDER_CREATED
        )
    if found:
        broker.orders[intent.client_order_id] = _accepted(intent)
    wrapper = module().ExperimentOutboxSubmitter(
        store, services=services, lower=lower, owner="test", clock=lambda: broker.now
    )
    if found:
        result = await wrapper.submit(
            intent,
            purpose=request.purpose,
            market_eligible=request.market_eligible,
            drawdown=request.drawdown,
            risk_approved=request.risk_approved,
            vetoes=request.vetoes,
        )
        assert result.recovered_after_ambiguity
    else:
        with pytest.raises(ValueError, match="CLAIMED_SEND_REQUIRES_RECOVERY"):
            await wrapper.submit(
                intent,
                purpose=request.purpose,
                market_eligible=request.market_eligible,
                drawdown=request.drawdown,
                risk_approved=request.risk_approved,
                vetoes=request.vetoes,
            )
    assert not broker.submitted


async def test_periodic_dispatch_cannot_submit_outside_operational_cycle(context):
    manifest, decision, store, services, _, broker = context
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    assert await module().dispatch_pending(store, services=services, now=broker.now) == ()
    assert store.get_intent(intent.intent_id).state == "PENDING"
    assert not broker.submitted


async def test_prepared_entry_ownership_survives_actual_fill_journal_import(context):
    from decimal import Decimal

    from quantbot.domain import Fill, OrderSide
    from quantbot.learner.experiment_lot_reader import refresh_lot_journal
    from quantbot.storage import StorageRepository

    manifest, decision, store, services, lower, broker = context
    intent = await module().prepare_entry(
        manifest, decision, store=store, services=services, simulation=True
    )
    request = SubmissionRequest.model_validate_json(
        store.pending_intents(manifest.experiment_id)[0].document
    )
    wrapper = module().ExperimentOutboxSubmitter(
        store, services=services, lower=lower, owner="test", clock=lambda: broker.now
    )
    result = await wrapper.submit(
        intent,
        purpose=request.purpose,
        market_eligible=request.market_eligible,
        drawdown=request.drawdown,
        risk_approved=request.risk_approved,
        vetoes=request.vetoes,
    )
    order = result.broker_order.model_copy(
        update={
            "status": "filled",
            "filled_quantity": intent.quantity,
            "filled_average_price": Decimal(106),
        }
    )
    with services.runner._database.transaction() as session:
        repo = StorageRepository(session)
        repo.update_broker_order_lifecycle(order)
        repo.record_fill(
            Fill(
                fill_id="actual-synthetic-fill",
                broker_order_id=order.broker_order_id,
                symbol=intent.symbol,
                side=OrderSide.BUY,
                quantity=intent.quantity,
                price=106,
                fee=Decimal(".10"),
                occurred_at=broker.now,
            )
        )
    imported = refresh_lot_journal(
        store,
        manifest,
        operational_path=services.runner._database.path,
        now=broker.now + timedelta(seconds=1),
    )
    assert any(row.fill_id == "actual-synthetic-fill" for row in imported)
    assert store.state(manifest.experiment_id).state != "FAULTED"


@pytest.mark.parametrize("halt", [None, "kill", "reconciliation", "data"])
async def test_integrated_cycle_retains_lifecycle_before_any_dispatch(context, halt):
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.operations.cycle import DataSyncResult
    from quantbot.operations.metrics import OperationalMetrics
    from quantbot.storage import StorageRepository

    manifest, decision, store, services, _, broker = context
    runner = services.runner

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=halt != "reconciliation", reasons=("synthetic-recon",))

    class Sync:
        async def sync(self):
            return DataSyncResult(
                healthy=halt != "data",
                completed_through=broker.now,
                bars_stored=0,
                stale_symbols=(),
                reasons=() if halt != "data" else ("synthetic-data",),
            )

    runtime = SimpleNamespace(
        database=runner._database,
        settings=SimpleNamespace(
            TRADING_MODE=TradingMode.PAPER,
            BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
            EXPECTED_ACCOUNT_ID="paper-1",
        ),
        running_git_commit=runner._identity.git_commit,
        coordinator=services.deadlines.coordinator,
        paths=SimpleNamespace(lock=runner._database.path.with_suffix(".cycle.lock")),
        recovery=Recovery(),
        metrics=OperationalMetrics(),
        runtime_context=lambda: None,
    )
    sync = Sync()
    if halt == "kill":
        with runner._database.transaction() as session:
            StorageRepository(session).set_kill_switch(
                True, reason="synthetic halt", updated_at=broker.now
            )
    result = await module().run_experiment_cycle(
        runtime,
        services,
        manifest=manifest,
        decisions=(decision,),
        store=store,
        owner="test",
        data_sync=sync,
        clock=lambda: broker.now,
        simulation=True,
        run_id="adaptive-cycle",
    )
    assert result.status == ("SUCCEEDED" if halt is None else "HALTED")
    assert len(broker.submitted) == (1 if halt is None else 0)
    if halt is not None:
        assert store.pending_intents(manifest.experiment_id) == ()


def test_wiring_cannot_mix_execution_and_operational_service_graphs(context):
    manifest, decision, store, services, _, broker = context
    runtime = SimpleNamespace(
        database=SimpleNamespace(path=services.runner._database.path.with_name("unrelated.db")),
        coordinator=services.deadlines.coordinator,
    )
    with pytest.raises(ValueError, match="OPERATIONAL_SERVICE_GRAPH_MISMATCH"):
        module().wire_experiment_services(
            runtime,
            services,
            manifest=manifest,
            decisions=(decision,),
            store=store,
            owner="test",
            data_sync=SimpleNamespace(),
            clock=lambda: broker.now,
            simulation=True,
        )
    assert not broker.submitted
