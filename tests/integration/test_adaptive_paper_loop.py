"""Synthetic full workflow: real safety plumbing, fake provider/broker/fit engine.

Explicit simulation commissioning connects the shadow decision to the retained
cycle. This is not a deployed broker run or prospective profitability evidence.
"""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

from quantbot.domain import BrokerEnvironment, Fill, OrderSide, Position, TradingMode
from quantbot.learner.experiment_adaptation import schedule_next_successor
from quantbot.learner.experiment_contract import CandidateArtifact, DecisionRecord
from quantbot.learner.experiment_coordinator import coordinator_tick
from quantbot.learner.experiment_execution import run_experiment_cycle
from quantbot.learner.experiment_lot_reader import refresh_lot_journal
from quantbot.learner.experiment_lots import LotEvent, resolve_lot
from quantbot.learner.experiment_observation import build_observation_batch
from quantbot.learner.experiment_research import (
    prepare_fit_request,
    research_eligibility,
    run_registered_fit,
)
from quantbot.learner.experiment_store import ExperimentStore
from quantbot.operations.cycle import DataSyncResult
from quantbot.operations.metrics import OperationalMetrics
from tests.fixtures.adaptive_paper import snapshot
from tests.unit.learner.test_experiment_adaptation import forward_labels
from tests.unit.learner.test_experiment_paper import _commissioned_pool
from tests.unit.learner.test_experiment_paper import runtime_context as runtime_context
from tests.unit.learner.test_experiment_research import Charger, Guard, fake_artifact, inputs


async def test_shadow_forecast_simulated_fill_independent_exit_outcome_and_fresh_successor(
    runtime_context, tmp_path
):
    kwargs, broker = runtime_context
    frozen = kwargs["manifest"]
    store = ExperimentStore(tmp_path / "loop.db", create=True)
    store.append(frozen, idempotency_key="manifest")
    charger, guard = Charger(), Guard()
    training, validation = inputs()
    request = prepare_fit_request(
        frozen,
        family="ridge",
        training=training,
        validation=validation,
        now=frozen.registered_at,
        guard=guard,
    )
    fit = run_registered_fit(
        store,
        frozen,
        request,
        guard=guard,
        charger=charger,
        now=frozen.registered_at,
        synthetic_engine=lambda req: fake_artifact(req).model_copy(update={"intercept": 0.02}),
    )
    candidate = next(
        event.decode(CandidateArtifact)
        for event in store.read_as_of(frozen.experiment_id, broker.now)
        if event.kind == "CandidateArtifact"
    )
    source = next(iter(kwargs["sources"].values()))
    observed = build_observation_batch(frozen, source, now=broker.now)
    tick = await coordinator_tick(
        store,
        frozen,
        now=broker.now,
        observation=observed,
        execution=None,
        clock_verified=True,
        owner="integration",
    )
    assert tick.decisions_recorded == 6 and tick.intents == 0
    decisions = tuple(
        event.decode(DecisionRecord)
        for event in store.read_as_of(frozen.experiment_id, broker.now)
        if event.kind == "DecisionRecord"
    )
    assert all(row.synthetic and not row.paper_eligible for row in decisions)
    kwargs = kwargs | {"candidate": candidate, "decisions": decisions}
    kwargs, broker, runner, supervisor = await _commissioned_pool((kwargs, broker))
    services = SimpleNamespace(runner=runner, deadlines=supervisor)

    class Recovery:
        async def recover(self, *args, **kwargs):
            return SimpleNamespace(ready=True, reasons=())

    class Sync:
        async def sync(self):
            return DataSyncResult(
                healthy=True,
                completed_through=broker.now,
                bars_stored=0,
                stale_symbols=(),
                reasons=(),
            )

    runtime = SimpleNamespace(
        database=runner._database,
        settings=SimpleNamespace(
            TRADING_MODE=TradingMode.PAPER,
            BROKER_ENVIRONMENT=BrokerEnvironment.PAPER,
            EXPECTED_ACCOUNT_ID="paper-1",
        ),
        running_git_commit=runner._identity.git_commit,
        coordinator=supervisor.coordinator,
        paths=SimpleNamespace(lock=runner._database.path.with_suffix(".cycle.lock")),
        recovery=Recovery(),
        metrics=OperationalMetrics(),
        runtime_context=lambda: None,
    )
    result = await run_experiment_cycle(
        runtime,
        services,
        manifest=frozen,
        decisions=decisions,
        store=store,
        owner="integration",
        data_sync=Sync(),
        clock=lambda: broker.now,
        run_id="synthetic-full-loop",
        simulation=True,
    )
    assert result.orders_submitted == 1 and len(broker.submitted) == 1
    entry = broker.submitted[0]
    price = Decimal(str(decisions[0].features.reference_close))

    def fill(intent, price, identifier):
        order = broker.orders[intent.client_order_id].model_copy(
            update={
                "filled_quantity": intent.quantity,
                "filled_average_price": price,
                "status": "filled",
            }
        )
        broker.orders[intent.client_order_id] = order
        broker.fills.append(
            Fill(
                fill_id=identifier,
                broker_order_id=order.broker_order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                price=price,
                fee=Decimal(".10"),
                occurred_at=broker.now,
            )
        )

    fill(entry, price, "synthetic-owned-entry")
    broker.positions = (
        Position(
            symbol=entry.symbol,
            quantity=entry.quantity,
            market_price=price,
            market_value=entry.quantity * price,
            average_entry_price=price,
        ),
    )
    broker.now += timedelta(seconds=1)
    assert (await supervisor.run_once()).healthy
    refresh_lot_journal(store, frozen, operational_path=runner._database.path, now=broker.now)
    declaration = next(
        event.decode(LotEvent)
        for event in store.read_as_of(frozen.experiment_id, broker.now)
        if event.kind == "LotEvent" and event.decode(LotEvent).kind == "ENTRY_DECLARED"
    )
    assert resolve_lot(store, declaration.lot_id, now=broker.now).status == "INCOMPLETE"
    broker.now = decisions[0].features.target_at
    assert (await supervisor.run_once()).exits_submitted == 1
    exit_intent = broker.submitted[-1]
    assert exit_intent.side is OrderSide.SELL
    fill(exit_intent, price + 1, "synthetic-owned-exit")
    broker.positions = ()
    broker.now += timedelta(seconds=1)
    assert (await supervisor.run_once()).healthy
    refresh_lot_journal(store, frozen, operational_path=runner._database.path, now=broker.now)
    outcome = resolve_lot(store, declaration.lot_id, now=broker.now)
    assert outcome.status == "COMPLETE", outcome
    assert outcome.synthetic and outcome.net_pnl == entry.quantity - Decimal(".20")
    assert len(broker.submitted) == 2

    # Fresh labels are synthetic forward fixtures, not account returns or live evidence.
    # Align the fixture's frozen prospective origin with this test's exact manifest.
    from unittest.mock import patch

    import tests.unit.learner.test_experiment_adaptation as adaptation_fixture

    with patch.object(adaptation_fixture, "manifest", lambda: frozen):
        labels = forward_labels(store, 10)
    now = labels[-1].available_at
    successor = schedule_next_successor(store, frozen, now=now, guard=guard)
    pending = research_eligibility(store, frozen, now=now)
    assert successor.status == "QUEUED" and pending.parent_model_id == fit.candidate_id
    child = run_registered_fit(
        store,
        frozen,
        pending,
        guard=guard,
        charger=charger,
        now=now,
        synthetic_engine=fake_artifact,
    )
    assert child.status == "SUCCEEDED" and len(charger.calls) == 2
    assert store.state(frozen.experiment_id).activation_id is None
    next_open = (now + timedelta(days=1)).replace(hour=13, minute=30, second=0, microsecond=0)
    next_source = snapshot(opened=next_open)
    next_tick = await coordinator_tick(
        store,
        frozen,
        now=next_source.received_at,
        observation=build_observation_batch(frozen, next_source, now=next_source.received_at),
        execution=None,
        clock_verified=True,
        owner="integration",
    )
    assert next_tick.decisions_recorded == 12 and next_tick.intents == 0
    assert len(broker.submitted) == 2
