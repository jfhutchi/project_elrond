"""Synthetic PAPER handoff; no deployed broker activity."""
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.experiment_store import ExperimentStore
from quantbot.learner.paper_runner import IntradayPaperServices
from tests.unit.learner.test_paper_exploration import exploration_context as exploration_context


@pytest.fixture
async def context(exploration_context, tmp_path):
    from decimal import Decimal

    from quantbot.research.promotion import PromotionLedger, PromotionState, Stage
    from quantbot.storage import StorageRepository
    from quantbot.storage.schema import promotion_state
    kwargs, broker, supervisor = exploration_context
    broker.account_snapshot = broker.account_snapshot.model_copy(update={
        "account": broker.account_snapshot.account.model_copy(update={
            "equity": Decimal("100000"), "cash": Decimal("100000"),
            "buying_power": Decimal("100000"),
        }),
    })
    runner = import_module("quantbot.learner.exploration_paper").ExplorationPaperRunner(**kwargs)
    with kwargs["database"].transaction() as session:
        for row in session.execute(promotion_state.select()).mappings():
            if row["stage"] != "SHADOW":
                PromotionLedger(session).demote(row["strategy_id"], Stage.SHADOW,
                                               reason="synthetic test", actor="test",
                                               now=broker.now)
        repo = StorageRepository(session)
        repo.save_strategy_deployment(kwargs["identity"])
        PromotionLedger(session).enter(PromotionState(
            strategy_id=kwargs["identity"].strategy_id,
            strategy_version=kwargs["identity"].version,
            configuration_hash=kwargs["identity"].configuration_hash,
            stage=Stage.PAPER_OBSERVATION, reason="synthetic test", actor="test",
            updated_at=broker.now,
        ))
        repo.create_run("exploration-intent", kwargs["identity"], started_at=broker.now)
    runner.bind_run("exploration-intent")
    store = ExperimentStore(tmp_path / "entry.db", create=True)
    store.append(kwargs["manifest"], idempotency_key="manifest")
    store.append(kwargs["exploration"], idempotency_key="policy")
    from quantbot.learner.experiment_evaluation import calendar_evidence

    store.append(calendar_evidence(kwargs["manifest"], kwargs["source"]),
                 idempotency_key="synthetic-calendar")
    assert store.acquire_lease(kwargs["manifest"].experiment_id, owner="test", now=broker.now,
                               duration=timedelta(minutes=3))
    services = IntradayPaperServices(runner=runner, deadlines=supervisor, cycle=SimpleNamespace())
    return kwargs, services, store, broker


async def test_exploration_journals_owned_intent_before_any_broker_send(context):
    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    intent = await module.prepare_exploration_entry(
        kwargs["manifest"], kwargs["exploration"], store=store, services=services, simulation=True,
    )
    assert intent is not None
    pending = store.pending_intents(kwargs["manifest"].experiment_id)
    assert len(pending) == 1 and pending[0].intent_id == intent.intent_id
    events = store.read_as_of(kwargs["manifest"].experiment_id, broker.now)
    assert any(row.kind == "ExplorationObservation" for row in events)
    assert any(row.kind == "LotEvent" for row in events)
    assert broker.submitted == []
    assert await module.prepare_exploration_entry(
        kwargs["manifest"], kwargs["exploration"], store=store, services=services, simulation=True,
    ) is None
    assert len(store.pending_intents(kwargs["manifest"].experiment_id)) == 1


async def test_synthetic_exploration_cannot_be_commissioned_as_real_paper(context):
    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    with pytest.raises(ValueError, match="SYNTHETIC"):
        await module.prepare_exploration_entry(
            kwargs["manifest"], kwargs["exploration"], store=store, services=services,
        )
    assert broker.submitted == []


async def test_entry_refusal_is_durable_and_does_not_create_an_intent(context, monkeypatch):
    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    async def refuse():
        return services.runner._no_entry("PAPER_PREDICTION_STALE")
    monkeypatch.setattr(services.runner, "evaluate", refuse)
    assert await module.prepare_exploration_entry(kwargs["manifest"], kwargs["exploration"],
                                                 store=store, services=services,
                                                 simulation=True) is None
    events = store.read_as_of(kwargs["manifest"].experiment_id, broker.now)
    rows = [e for e in events if e.kind == "ExplorationEntryRefusal"]
    assert len(rows) == 1
    record = rows[0].decode(module.ExplorationEntryRefusal)
    assert record.reasons == ("PAPER_PREDICTION_STALE",)
    assert record.recorded_at == broker.now and record.synthetic
    assert record.grants_execution is False
    assert record.policy_id == kwargs["exploration"].policy_id
    assert record.binding_hash == services.runner.binding.configuration_hash
    assert not store.pending_intents(kwargs["manifest"].experiment_id)
    assert broker.submitted == []


@pytest.mark.parametrize("state", ["FAULTED", "COMPLETED", "REJECTED", "INSUFFICIENT_EVIDENCE"])
async def test_terminal_exploration_cannot_prepare_new_entry(context, monkeypatch, state):
    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    current = store.state(kwargs["manifest"].experiment_id)
    monkeypatch.setattr(store, "state", lambda _: current.model_copy(update={"state": state}))
    assert await module.prepare_exploration_entry(
        kwargs["manifest"], kwargs["exploration"], store=store, services=services, simulation=True,
    ) is None
    assert store.pending_intents(kwargs["manifest"].experiment_id) == ()
    assert broker.submitted == []


@pytest.mark.parametrize("reason", ["EVALUATION_WINDOW_COMPLETE", "CALENDAR_UNKNOWN"])
async def test_exhausted_or_unknown_evaluation_cannot_prepare_entry(context, monkeypatch, reason):
    from quantbot.learner import experiment_evaluation

    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    monkeypatch.setattr(experiment_evaluation, "evaluate_experiment",
                        lambda *args, **kw: SimpleNamespace(new_entries_allowed=False,
                                                            status=reason))
    assert await module.prepare_exploration_entry(
        kwargs["manifest"], kwargs["exploration"], store=store, services=services, simulation=True,
    ) is None
    assert store.pending_intents(kwargs["manifest"].experiment_id) == ()
    assert broker.submitted == []


@pytest.mark.parametrize("halt", [None, "kill", "reconciliation", "data"])
async def test_exploration_cycle_preserves_all_retained_entry_gates(context, halt):
    from quantbot.domain import BrokerEnvironment, TradingMode
    from quantbot.operations.cycle import DataSyncResult
    from quantbot.operations.metrics import OperationalMetrics
    from quantbot.storage import StorageRepository

    module = import_module("quantbot.learner.exploration_execution")
    kwargs, services, store, broker = context
    runner = services.runner

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=halt != "reconciliation", reasons=("synthetic-recon",))

    class Sync:
        async def sync(self):
            return DataSyncResult(healthy=halt != "data", completed_through=broker.now,
                                  bars_stored=0, stale_symbols=(),
                                  reasons=() if halt != "data" else ("synthetic-data",))

    runtime = SimpleNamespace(
        database=runner._database,
        settings=SimpleNamespace(TRADING_MODE=TradingMode.PAPER,
                                 BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
                                 EXPECTED_ACCOUNT_ID="paper-1"),
        running_git_commit=runner._identity.git_commit,
        coordinator=services.deadlines.coordinator,
        paths=SimpleNamespace(lock=runner._database.path.with_suffix(".cycle.lock")),
        recovery=Recovery(), metrics=OperationalMetrics(), runtime_context=lambda: None,
    )
    from quantbot.operations.protection import PositionProtectionSupervisor
    runtime.protector = lambda: PositionProtectionSupervisor(
        database=runner._database, broker=broker, expected_account_id="paper-1",
        strategy_identity=runner._identity, metrics=runtime.metrics,
    )
    if halt == "kill":
        with runner._database.transaction() as session:
            StorageRepository(session).set_kill_switch(True, reason="synthetic test",
                                                       updated_at=broker.now)
    result = await module.run_exploration_cycle(
        runtime, services, manifest=kwargs["manifest"], policy=kwargs["exploration"],
        store=store, owner="test", data_sync=Sync(), clock=lambda: broker.now,
        run_id="exploration-full-cycle", simulation=True,
    )
    assert result.status == ("SUCCEEDED" if halt is None else "HALTED")
    assert len(broker.submitted) == (1 if halt is None else 0)
    if halt is not None:
        assert store.pending_intents(kwargs["manifest"].experiment_id) == ()


async def test_coordinator_exploration_context_requires_distinct_real_commission(context):
    from quantbot.learner import experiment_coordinator
    kwargs, services, store, broker = context
    with pytest.raises(ValueError, match="EXPLORATION_COMMISSION"):
        await experiment_coordinator.coordinator_tick(
            store, kwargs["manifest"], now=broker.now, observation=kwargs["observation"],
            execution=services, clock_verified=True,
            active_cycle=experiment_coordinator.ExplorationCycleContext(
                runtime=SimpleNamespace(), data_sync=SimpleNamespace(),
                clock=lambda: broker.now, policy=kwargs["exploration"],
            ),
        )
    assert broker.submitted == []
