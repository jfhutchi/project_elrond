"""Isolated synthetic acceptance only; never imported by runtime services."""

from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.domain import OrderSide, OrderType, TimeInForce
from quantbot.execution import ExecutionCoordinator
from quantbot.learner.paper_deadlines import PaperDeadlineSupervisor, record_deadline
from quantbot.storage import StorageRepository
from tests.integration.test_execution_coordinator import _long_position, _settings
from tests.unit.learner.test_exploration_execution import context as context
from tests.unit.learner.test_paper import NOW, deploy
from tests.unit.learner.test_paper import setup as original_setup
from tests.unit.learner.test_paper_deadlines import Broker, deadline, fill_entry
from tests.unit.learner.test_paper_exploration import (
    exploration_context as original_exploration_context,
)


@pytest.fixture
async def exploration_context(tmp_path):
    from quantbot.learner.exploration_paper import exploration_paper_binding

    async for kwargs, broker, supervisor in original_exploration_context.__wrapped__(tmp_path):
        # Stricter synthetic $10 cap avoids resetting historical equity/high-water evidence.
        config = kwargs["config"].model_copy(
            update={
                "allow_fractional_shares": True,
                "max_position_value_bps": 1,
            }
        )
        binding = exploration_paper_binding(
            kwargs["manifest"],
            kwargs["exploration"],
            config=config,
            policy=kwargs["policy"],
            veto_policy=kwargs["veto_policy"],
            market_data=kwargs["market_data"],
            classification=kwargs["classification"],
        )
        kwargs.update(
            config=config,
            binding=binding,
            identity=binding.strategy_identity(
                git_commit="e" * 40,
                deployed_at=kwargs["exploration"].recorded_at,
            ),
        )
        yield kwargs, broker, supervisor


@pytest.mark.asyncio
async def test_full_retained_fractional_exploration_entry(context):
    from tests.unit.learner.test_exploration_execution import (
        test_exploration_cycle_preserves_all_retained_entry_gates,
    )

    await test_exploration_cycle_preserves_all_retained_entry_gates(context, None)
    _, _, _, broker = context
    assert Decimal("0") < broker.submitted[0].quantity < Decimal("1")
    assert broker.submitted[0].time_in_force is TimeInForce.DAY


@pytest.fixture
def setup(tmp_path):
    yield from original_setup.__wrapped__(tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("trigger", ["deadline", "stop"])
async def test_fractional_owned_exit_retains_precision_and_day_semantics(setup, partial, trigger):
    deploy(setup)
    database, _, identity = setup
    with database.transaction() as session:
        StorageRepository(session).create_run("entry-run", identity, started_at=NOW)
    original = deadline(setup)
    entry_quantity = Decimal("0.024862")
    record = original.model_copy(
        update={
            "entry_intent": original.entry_intent.model_copy(update={"quantity": entry_quantity})
        }
    )
    record_deadline(database, record, run_id="entry-run")
    broker = Broker(database)
    supervisor = PaperDeadlineSupervisor(
        database=database,
        broker=broker,
        coordinator=ExecutionCoordinator(database, broker, _settings()),
        identity=identity,
        expected_account_id="paper-1",
        lock_path=database.path.with_suffix(".lock"),
        clock=lambda: broker.now,
    )
    filled = Decimal("0.012431") if partial else entry_quantity
    fill_entry(setup, record, broker, quantity=filled, partial=partial)
    broker.now = record.deadline_at if trigger == "deadline" else NOW + timedelta(minutes=1)
    if trigger == "stop":
        broker.positions = (
            _long_position(filled).model_copy(
                update={"market_price": Decimal("98"), "market_value": filled * Decimal("98")}
            ),
        )
    result = await supervisor.run_once()
    assert result.exits_submitted == 1
    assert len(broker.submitted) == 1
    exit_order = broker.submitted[0]
    assert exit_order.quantity == filled
    assert exit_order.side is OrderSide.SELL
    assert exit_order.order_type is OrderType.MARKET
    assert exit_order.time_in_force is TimeInForce.DAY
    assert bool(broker.cancelled) is partial
    with database.transaction() as session:
        repository = StorageRepository(session)
        assert (
            repository.list_fills(broker_order_id=broker.fills[0].broker_order_id) == broker.fills
        )
        assert repository.get_kill_switch_state().engaged
    broker.now += timedelta(seconds=10)
    await supervisor.run_once()
    assert len(broker.submitted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [OrderSide.BUY, OrderSide.SELL])
async def test_real_adapter_serializes_fractional_day_order_without_rounding(side):
    from tests.contract.test_alpaca_paper_broker import (
        ScriptedBrokerTransport,
        adapter,
        intent,
        order_payload,
        response,
    )

    order = intent().model_copy(update={"quantity": Decimal("0.024862"), "side": side})
    transport = ScriptedBrokerTransport([response(200, order_payload(order))])
    accepted = await adapter(transport).submit_order(order)
    assert accepted.quantity == order.quantity
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["url"] == "https://paper-api.alpaca.markets/v2/orders"
    assert call["json_body"]["qty"] == "0.024862"
    assert call["json_body"]["type"] == "market"
    assert call["json_body"]["time_in_force"] == "day"
    assert call["json_body"]["side"] == side.value.lower()
