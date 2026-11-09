"""Durable PAPER entries must fit one final acquisition into the real request budget."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.integration.paper_core.test_paper_funds import prepare, ready
from tests.integration.paper_core.test_resource_loop_recovery import ResourceProvider

from quantbot.domain import IntentState
from quantbot.execution import OrderSubmissionBlocked
from quantbot.paper_core.account_control import (
    AccountRequestControl,
    BudgetedTransport,
    initialize_account_control,
    observed_host_identity,
)
from quantbot.paper_core.contracts import Decision, EntryIntent, Observation, ProtectionPlan
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import guardian_cycle
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.runtime import coordinator
from quantbot.paper_core.store import table
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
async def test_final_send_does_not_replay_inventory_validation_before_writer_fence(
    tmp_path, monkeypatch
):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        assert not (await guardian_cycle(store, broker, now=clocks.wall)).reasons
        clocks.advance(2100)
        ready(store)
        await trader_batch_cycle(
            store,
            broker,
            await broker.observation_cohort(),
            broker.config.initial_model,
            now=clocks.wall,
        )
        ready(store)
        with db.transaction() as session:
            intent = StorageRepository(session).get_order_intent(store.links()[0]["intent_id"])
        assert intent is not None
        request = provider.request

        async def aged_quote(method, url, **kwargs):
            response = await request(method, url, **kwargs)
            if url.endswith("/quotes/latest"):
                payload = json.loads(response.content)
                for quote in payload["quotes"].values():
                    quote["t"] = (clocks.wall - timedelta(seconds=2)).isoformat()
                response = response.model_copy(update={"content": json.dumps(payload).encode()})
            return response

        provider.request = aged_quote
        ledger = store.ledger

        def slow_ledger(session):
            result = ledger(session)
            # Model measured local hydration cost, retaining the real ledger checks.
            clocks.advance(0.7)
            return result

        monkeypatch.setattr(store, "ledger", slow_ledger)
        result = await coordinator(store, broker).submit_durable(
            intent,
            purpose=OrderPurpose.ENTRY,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
        )
        assert result.intent.state is IntentState.FILLED
        assert len(provider.orders) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prepared_entry_sends_with_remaining_final_acquisition_budget(tmp_path, monkeypatch):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert not receipt.reasons, receipt.reasons
        clocks.advance(2100)
        ready(store)
        cohort = await broker.observation_cohort()
        decisions = await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        assert any(decision.action == "BUY" for decision in decisions), [d.gates for d in decisions]
        ready(store)
        control_path = tmp_path / "account-control.db"
        initialize_account_control(control_path)
        epoch = "e" * 64
        monkeypatch.setattr(
            "quantbot.paper_core.account_control.observed_host_epoch", lambda: epoch
        )
        control = AccountRequestControl(
            control_path=str(control_path),
            account_scope="a" * 64,
            host_identity=observed_host_identity(),
            host_epoch=epoch,
        )
        transport = BudgetedTransport(provider, control, "trader", clocks)
        broker.adapter._transport = transport
        # Acquisition/discovery already spent most of this minute's allowance.
        # Six remaining trading requests cover final clock, asset, account,
        # positions, open orders and the reserved POST, without a duplicate pass.
        for _ in range(34):
            assert transport.budgets["trading"].acquire(
                "trader", purpose="COHORT_ACQUISITION", now_monotonic=clocks.mono
            )
        provider.delay = 0.4
        started = clocks.mono
        reasons = await dispatch_entries(store, broker)
        assert len(provider.orders) == 1, reasons
        assert 0 < clocks.mono - started < 5
        order = provider.orders[0]
        with db.transaction() as session:
            repo = StorageRepository(session)
            stored = repo.get_broker_order_by_client_order_id(order["client_order_id"])
            assert stored is not None
            entries = [repo.get_order_intent(link["intent_id"]) for link in store.links()]
            assert any(entry.state is IntentState.FILLED for entry in entries)
        await dispatch_entries(store, broker)
        assert len(provider.orders) == 1
        status = transport.budgets["trading"].status(now_monotonic=clocks.mono)
        assert status["role_used"]["trader"] <= 40
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventory_delay", "validation_delay", "allowed"),
    [(0.7, 2.2, True), (1.1, 2.2, False), (1.8, 0, False)],
)
async def test_final_quote_follows_inventory_without_refreshing_inventory_age(
    tmp_path, monkeypatch, inventory_delay, validation_delay, allowed
):
    db, store, broker, provider, clocks = await prepare(
        tmp_path,
        provider_factory=ResourceProvider,
        entry_policy=EntryPolicy(contract="RESOURCE_BOUNDED_PAPER_V1", symbols=("AAPL", "MSFT")),
        initial_wall=datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    try:
        store.runtime_role = "guardian"
        assert not (await guardian_cycle(store, broker, now=clocks.wall)).reasons
        clocks.advance(2100)
        ready(store)
        cohort = await broker.observation_cohort()
        await trader_batch_cycle(
            store, broker, cohort, broker.config.initial_model, now=clocks.wall
        )
        ready(store)
        with db.transaction() as session:
            intent = StorageRepository(session).get_order_intent(store.links()[0]["intent_id"])
        assert intent is not None
        # Retained failed work must still traverse the real ledger/portfolio readers.
        link = store.links()[0]
        observation = store.read("core_observations", link["observation_id"], Observation)
        decision = store.read("core_decisions", link["decision_id"], Decision)
        rows = {
            name: []
            for name in (
                "core_observations",
                "core_decisions",
                "core_protection_plans",
                "order_intents",
                "core_intent_links",
                "order_events",
            )
        }
        for number in range(2000):
            old_observation = observation.model_copy(update={"source_refs": (f"history-{number}",)})
            old_decision = decision.model_copy(update={"observation_id": old_observation.id})
            old_plan = ProtectionPlan.for_decision(old_decision, store.config)
            old_intent = intent.model_copy(
                update={
                    "intent_id": f"history-{number}",
                    "client_order_id": f"history-{number}",
                    "state": IntentState.ERROR,
                }
            )
            for name, value in (
                ("core_observations", old_observation),
                ("core_decisions", old_decision),
                ("core_protection_plans", old_plan),
            ):
                rows[name].append(dict(id=value.id, payload=value.model_dump_json()))
            rows["order_intents"].append(StorageRepository._intent_payload(old_intent))
            rows["core_intent_links"].append(
                dict(
                    intent_id=old_intent.intent_id,
                    decision_id=old_decision.id,
                    protection_id=old_plan.id,
                    observation_id=old_observation.id,
                    role="ENTRY",
                    lot_id=old_decision.id,
                    payload=EntryIntent(
                        intent=old_intent, decision_id=old_decision.id, protection_id=old_plan.id
                    ).model_dump_json(),
                )
            )
            rows["order_events"].append(
                dict(
                    event_id=f"history-refused-{number}",
                    intent_id=old_intent.intent_id,
                    broker_order_id=None,
                    event_type="CORE_ENTRY_SEND_REFUSED",
                    occurred_at=clocks.wall.isoformat(),
                    detail_json=json.dumps(
                        {
                            "definitively_unsent": True,
                            "state_at_refusal": "ORDER_CREATED",
                            "guard_contract": "core-final-send-v1",
                        }
                    ),
                )
            )
        with db.transaction() as session:
            for name, documents in rows.items():
                session.execute(table(name).insert(), documents)
        from quantbot.paper_core import portfolio, runtime, send_guard

        original_budget = portfolio.portfolio_budget
        marked_calls = 0
        in_locked_guard = False
        locked_inventory_checks = []
        current_checks = 0
        inventory_check = portfolio.entry_inventory_objections
        original_guard = runtime.final_send_in_session

        def counted_inventory(*args, **kwargs):
            nonlocal current_checks
            if in_locked_guard:
                current_checks += 1
            return inventory_check(*args, **kwargs)

        def observed_guard(*args, **kwargs):
            nonlocal in_locked_guard, current_checks
            in_locked_guard, current_checks = True, 0
            try:
                return original_guard(*args, **kwargs)
            finally:
                locked_inventory_checks.append(current_checks)
                in_locked_guard = False

        monkeypatch.setattr(portfolio, "entry_inventory_objections", counted_inventory)
        monkeypatch.setattr(send_guard, "entry_inventory_objections", counted_inventory)
        monkeypatch.setattr(runtime, "final_send_in_session", observed_guard)

        def delayed_validation(*args, **kwargs):
            nonlocal marked_calls
            result = original_budget(*args, **kwargs)
            if kwargs.get("market_snapshot") is not None:
                marked_calls += 1
                if marked_calls == 2:
                    clocks.advance(validation_delay)
            return result

        monkeypatch.setattr(portfolio, "portfolio_budget", delayed_validation)
        request = provider.request
        calls = []

        async def delayed_inventory(method, url, **kwargs):
            calls.append((method, url))
            if method == "GET" and url.endswith(("/account", "/positions", "/orders")):
                clocks.advance(inventory_delay)
            response = await request(method, url, **kwargs)
            if url.endswith("/quotes/latest"):
                payload = json.loads(response.content)
                for quote in payload["quotes"].values():
                    quote["t"] = (clocks.wall - timedelta(seconds=2)).isoformat()
                response = response.model_copy(update={"content": json.dumps(payload).encode()})
            return response

        provider.request = delayed_inventory
        submitter = coordinator(store, broker)
        started = clocks.wall
        arguments = dict(
            purpose=OrderPurpose.ENTRY,
            market_eligible=True,
            drawdown=None,
            risk_approved=None,
            vetoes=None,
        )
        if allowed:
            result = await submitter.submit_durable(intent, **arguments)
            assert result.intent.state is IntentState.FILLED
            assert len(provider.orders) == 1
        else:
            with pytest.raises(OrderSubmissionBlocked) as refused:
                await submitter.submit_durable(intent, **arguments)
            assert "PORTFOLIO_MARKS_STALE_OR_MISMATCHED" in refused.value.decision.reasons
            assert "ENTRY_PRICE_UNAVAILABLE_OR_STALE" not in refused.value.decision.reasons
            assert not provider.orders
        assert submitter._entry_acquired_at == started
        assert locked_inventory_checks == ([2] if inventory_delay < 1.5 else [])
        assert sum("paper-api.alpaca.markets" in url for _, url in calls) == (6 if allowed else 5)
        quote_index = next(i for i, (_, url) in enumerate(calls) if url.endswith("/quotes/latest"))
        assert all(
            next(
                i for i, (method, url) in enumerate(calls) if method == "GET" and url.endswith(path)
            )
            < quote_index
            for path in ("/account", "/positions", "/orders")
        )
    finally:
        db.close()
