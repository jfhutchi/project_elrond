"""Readiness changes between acquisition and POST must not strand paper entries."""

from decimal import Decimal

import pytest
from tests.integration.paper_core.test_paper_funds import candidate, prepare, ready

from quantbot.brokers.base import BrokerProtocolError
from quantbot.domain import IntentState
from quantbot.paper_core.contracts import ProtectionPlan
from quantbot.paper_core.funds import entry_inventory_objections
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.roles import claim_objections, dispatch_entries
from quantbot.paper_core.send_guard import definitively_unsent
from quantbot.paper_core.trader import claim_entry
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
async def test_equity_mark_change_preserves_verified_cash_and_current_risk_limits(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        ready(store)
        provider.account_overrides["equity"] = "100000.01"
        reasons = await dispatch_entries(store, broker)
        assert reasons == ()
        assert len(provider.orders) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_pre_send_refusal_rolls_back_with_failed_state_transition(tmp_path, monkeypatch):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        ready(store)
        request = provider.request

        async def changed(method, url, **kwargs):
            if url.endswith("/account"):
                with db.transaction() as session:
                    state = (
                        StorageRepository(session).get_order_intent(entry.intent.intent_id).state
                    )
                if state is IntentState.ORDER_CREATED:
                    provider.cash -= 1
            return await request(method, url, **kwargs)

        original = StorageRepository.transition_order_intent

        def fail_transition(self, intent_id, state, **kwargs):
            if state is IntentState.ERROR:
                raise RuntimeError("OFFLINE_CRASH_BEFORE_COMMIT")
            return original(self, intent_id, state, **kwargs)

        provider.request = changed
        monkeypatch.setattr(StorageRepository, "transition_order_intent", fail_transition)
        assert "RuntimeError" in await dispatch_entries(store, broker)
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent(entry.intent.intent_id).state is IntentState.ORDER_CREATED
            assert not any(
                e.event_type == "CORE_ENTRY_SEND_REFUSED"
                for e in repo.list_order_events(intent_id=entry.intent.intent_id)
            )
        assert not provider.orders
    finally:
        db.close()


@pytest.mark.asyncio
async def test_post_protocol_failure_remains_ambiguous_and_reserves_slot(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        ready(store)
        request = provider.request

        async def uncertain(method, url, **kwargs):
            if method == "POST":
                raise BrokerProtocolError("OFFLINE_UNCERTAIN_POST")
            return await request(method, url, **kwargs)

        provider.request = uncertain
        assert "BrokerProtocolError" in await dispatch_entries(store, broker)
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_order_intent(entry.intent.intent_id).state is IntentState.ERROR
            assert not definitively_unsent(repo, entry.intent.intent_id)
        assert f"CORE_ENTRY_SLOT_RESERVED:{entry.intent.intent_id}" in claim_objections(store)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_paired_inventory_reads_allow_market_marks_but_keep_cash_certification(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        request = provider.request
        accounts = 0

        async def marked(method, url, **kwargs):
            nonlocal accounts
            if url.endswith("/account"):
                accounts += 1
                provider.account_overrides["equity"] = str(Decimal(100000) + accounts)
            return await request(method, url, **kwargs)

        provider.request = marked
        await synchronize(store, broker, now=clocks.wall)
        with db.transaction() as session:
            assert not entry_inventory_objections(
                store, session, store.latest_proof(), now=clocks.wall
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_final_cash_change_is_atomically_recorded_as_unsent_and_releases_slot(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        ready(store)
        request = provider.request

        async def changed(method, url, **kwargs):
            if url.endswith("/account"):
                with db.transaction() as session:
                    state = (
                        StorageRepository(session).get_order_intent(entry.intent.intent_id).state
                    )
                if state is IntentState.ORDER_CREATED:
                    provider.cash -= Decimal(1)
            return await request(method, url, **kwargs)

        provider.request = changed
        reasons = await dispatch_entries(store, broker)
        assert "CORE_CURRENT_CASH_OR_ACCOUNT_UNCERTIFIED" in reasons
        assert not provider.orders
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.get_order_intent(entry.intent.intent_id)
            assert intent.state is IntentState.ERROR
            assert definitively_unsent(repo, intent.intent_id)
        assert not any(r.startswith("CORE_ENTRY_SLOT_RESERVED:") for r in claim_objections(store))
        batch = process_outcomes(store, cutoff=clocks.wall)
        assert not any("ORDER_UNAVAILABLE" in r for r in batch.reasons)
        assert not store.documents("core_round_trips")
        provider.calls.clear()
        await broker.owned_events(store)
        assert not any("orders:by_client_order_id" in url for _, url, _ in provider.calls)
    finally:
        db.close()
