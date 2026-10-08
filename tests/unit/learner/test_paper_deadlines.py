from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.domain import Fill, IntentState, MarketClock, OrderSide
from quantbot.execution import ExecutionCoordinator
from quantbot.learner.paper_deadlines import (
    PaperDeadline,
    PaperDeadlineSupervisor,
    deadline_entry_objections,
    record_deadline,
)
from quantbot.storage import StorageRepository
from tests.integration.test_execution_coordinator import (
    AcceptingBroker,
    _accepted,
    _long_position,
    _settings,
)
from tests.unit.learner.test_paper import NOW, deploy, intent
from tests.unit.learner.test_paper import setup as _paper_setup


@pytest.fixture(name="setup")
def paper_store(tmp_path):
    yield from _paper_setup.__wrapped__(tmp_path)


class Broker(AcceptingBroker):
    def __init__(self, database):
        super().__init__(database, positions=())
        self.orders = {}
        self.fills = []
        self.cancelled = []
        self.now = NOW
        self.open = True

    async def get_market_clock(self):
        return MarketClock(
            timestamp=self.now,
            is_open=self.open,
            next_open=self.now + timedelta(days=1),
            next_close=self.now.replace(hour=20),
        )

    async def get_open_orders(self):
        return [
            order for order in self.orders.values() if order.status not in {"filled", "canceled"}
        ]

    async def get_order_by_client_id(self, client_order_id, *, expected=None):
        return self.orders.get(client_order_id)

    async def get_order(self, order_id):
        return next(order for order in self.orders.values() if order.broker_order_id == order_id)

    async def get_fills(self, *, after=None):
        return list(self.fills)

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        order = await self.get_order(order_id)
        self.orders[order.client_order_id] = order.model_copy(update={"status": "canceled"})
        return True

    async def submit_order(self, request):
        order = await super().submit_order(request)
        self.orders[request.client_order_id] = order
        return order


def deadline(setup):
    database, binding, identity = setup
    return PaperDeadline(
        entry_intent=intent(identity),
        binding=binding,
        identity=identity,
        model_id=binding.model_id,
        prediction_id="prediction-1",
        account_id_hash=PaperDeadline.account_hash("paper-1"),
        recorded_at=NOW,
        reference_at=NOW,
        entry_valid_until=NOW + timedelta(seconds=120),
        deadline_at=NOW + timedelta(minutes=30),
        session_exit_at=NOW.replace(hour=19, minute=58),
        initial_stop=Decimal("99"),
    )


def prepare(setup, *, quantity=None):
    deploy(setup)
    database, binding, identity = setup
    with database.transaction() as session:
        StorageRepository(session).create_run("entry-run", identity, started_at=NOW)
    record = deadline(setup)
    if quantity is not None:
        record = PaperDeadline.model_validate(record.model_dump() | {
            "entry_intent": record.entry_intent.model_dump() | {"quantity": quantity}})
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
    return record, broker, supervisor


@pytest.mark.asyncio
async def test_deadline_clock_is_measured_after_broker_response(setup):
    _, broker, supervisor = prepare(setup)
    original = broker.get_market_clock

    async def delayed_clock():
        broker.now += timedelta(seconds=2)
        return await original()

    broker.get_market_clock = delayed_clock
    result = await supervisor.run_once()
    assert result.healthy
    assert broker.submitted == []


def fill_entry(setup, record, broker, *, quantity=Decimal("1"), partial=False):
    database, _, _ = setup
    order = _accepted(record.entry_intent).model_copy(
        update={
            "filled_quantity": quantity,
            "filled_average_price": Decimal("100"),
            "status": "partially_filled" if partial else "filled",
        }
    )
    broker.orders[order.client_order_id] = order
    broker.fills = [
        Fill(
            fill_id="entry-fill",
            broker_order_id=order.broker_order_id,
            symbol="SPY",
            side=OrderSide.BUY,
            quantity=quantity,
            price=Decimal("100"),
            occurred_at=NOW,
        )
    ]
    broker.positions = (
        _long_position(quantity).model_copy(
            update={
                "market_price": Decimal("100"),
                "market_value": quantity * 100,
            }
        ),
    )
    with database.transaction() as session:
        repo = StorageRepository(session)
        repo.create_order_intent(record.entry_intent)
        repo.transition_order_intent(
            record.entry_intent.intent_id,
            IntentState.ORDER_CREATED,
            expected_current=IntentState.RISK_APPROVED,
        )
        repo.transition_order_intent(
            record.entry_intent.intent_id,
            IntentState.SUBMITTING,
            expected_current=IntentState.ORDER_CREATED,
        )
        repo.transition_order_intent(
            record.entry_intent.intent_id,
            IntentState.BROKER_ACCEPTED,
            expected_current=IntentState.SUBMITTING,
        )
        repo.save_broker_order(order)


@pytest.mark.asyncio
async def test_entry_needs_durable_handoff_and_fresh_real_supervisor_heartbeat(setup):
    record, broker, supervisor = prepare(setup)
    database, _, _ = setup
    assert "DEADLINE_SUPERVISOR_UNAVAILABLE" in deadline_entry_objections(
        database,
        record.entry_intent,
        account_id="paper-1",
        now=NOW,
    )
    await supervisor.run_once()
    assert (
        deadline_entry_objections(database, record.entry_intent, account_id="paper-1", now=NOW)
        == ()
    )
    assert "DEADLINE_SUPERVISOR_STALE" in deadline_entry_objections(
        database,
        record.entry_intent,
        account_id="paper-1",
        now=NOW + timedelta(seconds=61),
    )
    assert "DEADLINE_HANDOFF_MISSING" in deadline_entry_objections(
        database,
        record.entry_intent.model_copy(update={"intent_id": "different"}),
        account_id="paper-1",
        now=NOW,
    )


@pytest.mark.asyncio
async def test_deadline_exit_runs_with_entry_kill_switch_and_demoted_model(setup):
    from quantbot.research.promotion import PromotionLedger, Stage

    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    database, _, identity = setup
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert repo.get_kill_switch_state().engaged
        PromotionLedger(session).demote(
            identity.strategy_id, Stage.SHADOW, reason="test", actor="test", now=NOW
        )
    broker.now = record.deadline_at
    result = await supervisor.run_once()
    assert result.exits_submitted == 1
    assert broker.submitted[0].side is OrderSide.SELL
    assert broker.submitted[0].quantity == 1
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert repo.list_fills(broker_order_id=broker.fills[0].broker_order_id) == broker.fills
        assert repo.get_kill_switch_state().engaged
    broker.now += timedelta(seconds=10)
    await supervisor.run_once()
    assert len(broker.submitted) == 1


@pytest.mark.asyncio
async def test_partial_entry_is_cancelled_before_deadline_exit(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker, quantity=Decimal("0.5"), partial=True)
    broker.now = record.deadline_at
    result = await supervisor.run_once()
    assert result.exits_submitted == 1
    assert broker.cancelled == [broker.fills[0].broker_order_id]
    assert broker.submitted[0].quantity == Decimal("0.5")


@pytest.mark.asyncio
async def test_unverified_position_is_not_sold_and_supervisor_blocks_entries(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.positions = (_long_position(Decimal("2")),)
    broker.now = record.deadline_at
    result = await supervisor.run_once()
    assert not result.healthy
    assert "DEADLINE_POSITION_MISMATCH" in result.reasons
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_hard_stop_and_session_deadline_are_independent_of_forecast_entry(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.now = NOW + timedelta(minutes=1)
    broker.positions = (
        _long_position(Decimal("1")).model_copy(
            update={
                "market_price": Decimal("98"),
                "market_value": Decimal("98"),
            }
        ),
    )
    assert (await supervisor.run_once()).exits_submitted == 1


@pytest.mark.asyncio
async def test_deadline_gate_cannot_pass_a_fresh_heartbeat_for_a_different_account(setup):
    record, broker, supervisor = prepare(setup)
    await supervisor.run_once()
    database, _, _ = setup
    assert "DEADLINE_ACCOUNT_MISMATCH" in deadline_entry_objections(
        database,
        record.entry_intent,
        account_id="other-account",
        now=NOW,
    )


def test_handoff_is_immutable_and_cannot_extend_thirty_minute_cap(setup):
    from quantbot.storage import StateConflictError

    record, _, _ = prepare(setup)
    database, _, _ = setup
    with pytest.raises(ValueError, match="thirty"):
        PaperDeadline.model_validate(
            record.model_dump()
            | {
                "deadline_at": NOW + timedelta(minutes=31),
            }
        )
    with pytest.raises(StateConflictError):
        record_deadline(
            database, record.model_copy(update={"initial_stop": Decimal("98")}), run_id="entry-run"
        )


@pytest.mark.asyncio
async def test_existing_protective_sell_prevents_a_second_deadline_sell(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    database, _, _ = setup
    external = record.entry_intent.model_copy(
        update={
            "intent_id": "existing-protective-exit",
            "client_order_id": "existing-protective-exit",
            "side": OrderSide.SELL,
            "created_at": NOW + timedelta(seconds=1),
        }
    )
    with database.transaction() as session:
        StorageRepository(session).create_order_intent(external)
    from quantbot.risk import OrderPurpose

    await supervisor.coordinator.submit_durable(
        external,
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )
    broker.now = record.deadline_at
    result = await supervisor.run_once()
    assert "DEADLINE_EXTERNAL_EXIT_PENDING" in result.reasons
    assert len(broker.submitted) == 1  # Only the existing protection authority's sell.
    # An acknowledged external exit can be partly filled and then cancelled. The
    # deadline authority verifies those fills before covering only the remainder.
    order = broker.orders[external.client_order_id].model_copy(
        update={
            "status": "canceled",
            "filled_quantity": Decimal("0.4"),
            "filled_average_price": Decimal("100"),
        }
    )
    broker.orders[external.client_order_id] = order
    broker.fills.append(
        Fill(
            fill_id="protective-fill",
            broker_order_id=order.broker_order_id,
            symbol="SPY",
            side=OrderSide.SELL,
            quantity=Decimal("0.4"),
            price=Decimal("100"),
            occurred_at=broker.now,
        )
    )
    broker.positions = (_long_position(Decimal("0.6")),)
    broker.now += timedelta(seconds=10)
    # Give the fake exchange unique order IDs, as the actual broker does.
    original_submit = broker.submit_order

    async def unique_submit(request):
        result = (await original_submit(request)).model_copy(
            update={"broker_order_id": "replacement-" + request.client_order_id}
        )
        broker.orders[request.client_order_id] = result
        return result

    broker.submit_order = unique_submit
    assert (await supervisor.run_once()).exits_submitted == 1
    assert broker.submitted[-1].quantity == Decimal("0.6")


@pytest.mark.asyncio
async def test_final_submit_rechecks_durable_minute_reference_expiry(setup):
    from quantbot.learner.paper_deadlines import DeadlineGatedSubmitter
    from quantbot.risk import OrderPurpose
    from tests.unit.learner.test_paper import Submitter

    record, broker, supervisor = prepare(setup)
    broker.now = NOW + timedelta(seconds=119)
    await supervisor.run_once()
    delegate = Submitter()
    gateway = DeadlineGatedSubmitter(
        setup[0], delegate, expected_account_id="paper-1", clock=lambda: broker.now
    )
    broker.now += timedelta(seconds=20)
    with pytest.raises(ValueError, match="PAPER_ENTRY_REFERENCE_EXPIRED"):
        await gateway.submit(
            record.entry_intent,
            purpose=OrderPurpose.ENTRY,
            market_eligible=True,
            drawdown=None,
            risk_approved=True,
        )
    assert delegate.calls == []


@pytest.mark.asyncio
async def test_deadline_sell_blocks_later_protective_sell(setup):
    from quantbot.execution import OrderSubmissionBlocked
    from quantbot.risk import OrderPurpose

    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.now = record.deadline_at
    assert (await supervisor.run_once()).exits_submitted == 1
    deadline_exit = broker.submitted[0]
    protective = deadline_exit.model_copy(
        update={"intent_id": "later-protection", "client_order_id": "later-protection"}
    )
    with setup[0].transaction() as session:
        StorageRepository(session).create_order_intent(protective)
    with pytest.raises(OrderSubmissionBlocked) as blocked:
        await supervisor.coordinator.submit_durable(
            protective,
            purpose=OrderPurpose.STRATEGY_EXIT,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )
    assert "EXIT_REDUCING_ORDER_PENDING" in blocked.value.decision.reasons
    assert len(broker.submitted) == 1
    # Resuming the already accepted same durable order never submits again.
    await supervisor.coordinator.submit_durable(
        deadline_exit,
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )
    assert len(broker.submitted) == 1


@pytest.mark.asyncio
async def test_pending_protective_exit_does_not_prevent_due_partial_entry_cancellation(setup):
    from quantbot.risk import OrderPurpose

    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker, quantity=Decimal("0.5"), partial=True)
    protective = record.entry_intent.model_copy(
        update={
            "intent_id": "partial-protection",
            "client_order_id": "partial-protection",
            "side": OrderSide.SELL,
            "quantity": Decimal("0.5"),
        }
    )
    with setup[0].transaction() as session:
        StorageRepository(session).create_order_intent(protective)
    await supervisor.coordinator.submit_durable(
        protective,
        purpose=OrderPurpose.STRATEGY_EXIT,
        market_eligible=True,
        drawdown=None,
        risk_approved=None,
    )
    broker.now = record.deadline_at
    result = await supervisor.run_once()
    assert broker.cancelled == [broker.fills[0].broker_order_id]
    assert "DEADLINE_EXTERNAL_EXIT_PENDING" in result.reasons
    assert len(broker.submitted) == 1
    with setup[0].transaction() as session:
        stored = StorageRepository(session).get_order_intent(record.entry_intent.intent_id)
        assert stored.state is IntentState.CANCELLED


@pytest.mark.asyncio
async def test_fractional_exit_uses_its_broker_clock_and_never_sends_twice(setup):
    record, broker, supervisor = prepare(setup, quantity=Decimal("0.045887"))
    fill_entry(setup, record, broker, quantity=Decimal("0.045887"))
    original_submit = broker.submit_order
    async def filled_exit(request):
        order = await original_submit(request)
        broker_order = order.model_copy(update={
            "submitted_at": broker.now - timedelta(milliseconds=121), "status": "filled",
            "filled_quantity": request.quantity, "filled_average_price": Decimal("100")})
        broker.orders[order.client_order_id] = broker_order
        broker.fills.append(Fill(fill_id="fractional-exit-fill",
            broker_order_id=broker_order.broker_order_id, symbol=request.symbol,
            side=OrderSide.SELL, quantity=request.quantity, price=Decimal("100"),
            occurred_at=broker.now - timedelta(milliseconds=117)))
        broker.positions = ()
        return order
    broker.submit_order = filled_exit
    async def filtered_fills(*, after=None):
        return [f for f in broker.fills if after is None or f.occurred_at >= after]
    broker.get_fills = filtered_fills
    broker.now = record.deadline_at
    assert (await supervisor.run_once()).exits_submitted == 1
    broker.now += timedelta(seconds=10)
    assert (await supervisor.run_once()).healthy
    broker.now += timedelta(seconds=10)
    assert (await supervisor.run_once()).healthy
    assert len(broker.submitted) == 1
    with setup[0].transaction() as session:
        events = StorageRepository(session).list_order_events()
        assert sum(e.event_type == "LEARNER_PAPER_DEADLINE_CLOSED_V1" for e in events) == 1


@pytest.mark.asyncio
async def test_same_deadline_failure_has_one_incident_but_every_measurement_is_retained(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.positions = (_long_position(Decimal("2")),)
    broker.now = record.deadline_at
    assert not (await supervisor.run_once()).healthy
    broker.now += timedelta(seconds=10)
    assert not (await supervisor.run_once()).healthy
    with setup[0].transaction() as session:
        repo = StorageRepository(session)
        assert len(repo.list_incidents(unresolved_only=True)) == 1
        assert len(repo.list_signals(symbol="PAPER_DEADLINES")) == 2
    # A different active root cause must still get its own incident.
    async def stale_clock():
        return MarketClock(timestamp=broker.now-timedelta(seconds=121), is_open=True,
                           next_open=broker.now+timedelta(days=1),
                           next_close=broker.now.replace(hour=20))
    broker.get_market_clock = stale_clock
    broker.now += timedelta(seconds=10)
    assert (await supervisor.run_once()).reasons == ("DEADLINE_BROKER_CLOCK_STALE",)
    with setup[0].transaction() as session:
        assert len(StorageRepository(session).list_incidents(unresolved_only=True)) == 2
    assert broker.submitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "damage", ["duplicate", "quantity", "symbol", "side", "order", "early", "future"]
)
async def test_broker_clock_anchor_cannot_adopt_invalid_or_unowned_fills(setup, damage):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    fill = broker.fills[0]
    if damage == "duplicate":
        broker.fills.append(fill)
    else:
        values = {"quantity": {"quantity": Decimal(".5")},
                  "symbol": {"symbol": "OTHER"},
                  "side": {"side": OrderSide.SELL},
                  "order": {"broker_order_id": "unrelated-order"},
                  "early": {"occurred_at": NOW-timedelta(seconds=1)},
                  "future": {"occurred_at": NOW+timedelta(hours=1)}}[damage]
        broker.fills[0] = fill.model_copy(update=values)
    broker.now = record.deadline_at
    assert not (await supervisor.run_once()).healthy
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_unrelated_provider_fills_are_not_attributed_to_owned_order(setup):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.fills.append(broker.fills[0].model_copy(update={
        "fill_id": "foreign-fill", "broker_order_id": "foreign-order", "quantity": Decimal("7")}))
    broker.now = record.deadline_at
    assert (await supervisor.run_once()).exits_submitted == 1
    assert len(broker.submitted) == 1 and broker.submitted[0].quantity == Decimal("1")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["stale", "future", "reversed"])
async def test_clock_domain_fix_still_refuses_unhealthy_actual_clock(setup, failure):
    record, broker, supervisor = prepare(setup)
    fill_entry(setup, record, broker)
    broker.now = record.deadline_at
    async def bad_clock():
        if failure == "reversed":
            broker.now -= timedelta(seconds=1)
        timestamp = broker.now + (timedelta(seconds=-121) if failure == "stale"
                                  else timedelta(seconds=1) if failure == "future"
                                  else timedelta(0))
        return MarketClock(timestamp=timestamp, is_open=True,
                           next_open=broker.now+timedelta(days=1),
                           next_close=broker.now.replace(hour=20))
    broker.get_market_clock = bad_clock
    assert not (await supervisor.run_once()).healthy
    assert broker.submitted == []


@pytest.mark.asyncio
async def test_deduplication_does_not_hide_a_different_handoff_or_resolve_history(setup):
    from quantbot.learner.paper_deadlines import DeadlineStateError
    record, broker, supervisor = prepare(setup)
    async def failed_process(*args, **kwargs):
        raise DeadlineStateError("DEADLINE_POSITION_MISMATCH")
    supervisor._process = failed_process
    assert not (await supervisor.run_once()).healthy
    second = PaperDeadline.model_validate(record.model_dump() | {
        "entry_intent": record.entry_intent.model_dump() | {
            "intent_id": "another-entry", "client_order_id": "another-entry-client"}})
    record_deadline(setup[0], second, run_id="entry-run")
    broker.now += timedelta(seconds=10)
    assert not (await supervisor.run_once()).healthy
    broker.now += timedelta(seconds=10)
    assert not (await supervisor.run_once()).healthy
    with setup[0].transaction() as session:
        repo = StorageRepository(session)
        assert len(repo.list_incidents(unresolved_only=True)) == 2
        assert len(repo.list_signals(symbol="PAPER_DEADLINES")) == 3
    assert broker.submitted == []
