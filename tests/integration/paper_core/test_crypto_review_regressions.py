"""Offline regressions for shared stock risk and retired crypto ownership claims."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.integration.paper_core.test_crypto_paper_protocol import prepared_crypto
from tests.integration.paper_core.test_paper_funds import ready

from quantbot.domain import IntentState
from quantbot.paper_core.capital import opening_for_session
from quantbot.paper_core.continuous_capital import _trusted_opening
from quantbot.paper_core.contracts import Decision, Observation
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.outcomes import import_broker_events
from quantbot.paper_core.portfolio import opening_balance_source_valid
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.send_guard import record_pre_send_refusal
from quantbot.paper_core.store import put, table
from quantbot.paper_core.time_evidence import acquire_time_evidence
from quantbot.paper_core.trader import trader_batch_cycle
from quantbot.risk import OrderPurpose
from quantbot.storage import StorageRepository
from quantbot.storage.repositories import StateConflictError


async def bought_crypto(tmp_path):
    values = await prepared_crypto(tmp_path)
    _, store, broker, provider, clocks = values
    decisions = await trader_batch_cycle(
        store,
        broker,
        await broker.observation_cohort(),
        broker.config.initial_model,
        now=clocks.wall,
    )
    assert len(decisions) == 1 and decisions[0].action == "BUY"
    ready(store)
    await dispatch_entries(store, broker)
    assert len(provider.orders) == 1
    return values


@pytest.mark.parametrize("definitely_unsent", [True, False])
async def test_scoped_reconciliation_ignores_only_definitively_unsent_claims(
    tmp_path, definitely_unsent
):
    db, store, broker, _, clocks = await bought_crypto(tmp_path)
    try:
        with db.transaction() as session:
            links = table("core_intent_links")
            original = (
                session.execute(select(links).where(links.c.role == "ENTRY")).mappings().one()
            )
            repo = StorageRepository(session)
            observation = store.read(
                "core_observations", original["observation_id"], Observation
            ).model_copy(update={"recorded_at_utc": clocks.wall})
            decision = store.read("core_decisions", original["decision_id"], Decision).model_copy(
                update={"observation_id": observation.id}
            )
            put(session, "core_observations", observation.id, observation)
            put(session, "core_decisions", decision.id, decision)
            intent = repo.get_order_intent(original["intent_id"]).model_copy(
                update={
                    "intent_id": "retired-crypto-claim",
                    "client_order_id": "qb-retired-crypto",
                    "state": IntentState.ORDER_CREATED,
                }
            )
            repo.create_order_intent(intent)
            session.execute(
                links.insert().values(
                    **{
                        **dict(original),
                        "intent_id": intent.intent_id,
                        "lot_id": decision.id,
                        "observation_id": observation.id,
                        "decision_id": decision.id,
                    }
                )
            )
            if definitely_unsent:
                record_pre_send_refusal(
                    session, intent, OrderPurpose.ENTRY, ("STALE_QUOTE",), clock=lambda: clocks.wall
                )
                repo.transition_order_intent(
                    intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
                )
        if definitely_unsent:
            await synchronize(store, broker, now=clocks.wall)
            assert store.latest_proof().quarantined_symbols == ("BTC/USD",)
        else:
            with pytest.raises(StateConflictError, match="EXCLUSIVE_ENTRY_UNAVAILABLE"):
                await synchronize(store, broker, now=clocks.wall)
    finally:
        db.close()


async def test_real_stock_opening_equity_survives_pending_coin_fee_and_later_ledger(
    tmp_path, monkeypatch
):
    db, store, broker, provider, clocks = await bought_crypto(tmp_path)
    try:
        target = datetime(2026, 11, 9, 14, 30, 3, tzinfo=UTC)
        clocks.advance((target - clocks.wall).total_seconds())
        provider.account_overrides["equity"] = "110000"
        # The bounded activity collector catches up without backdating any balance.
        for _ in range(13):
            import_broker_events(store, await broker.owned_events(store))
            broker.accept_activity_cursor(store)
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        assert not proof.complete and proof.quarantined_symbols == ("BTC/USD",)
        with monkeypatch.context() as legacy:
            legacy.setattr(type(store), "entry_policy", property(lambda _: None))
            with db.transaction() as session:
                assert not opening_balance_source_valid(store, session, proof, current=True)
                assert not opening_balance_source_valid(store, session, proof, current=False)
        timing = await acquire_time_evidence(broker, clocks, context=store.config)
        store.record("core_time_evidence", timing)
        opening = opening_for_session(store, timing, preferred_proof=proof)
        assert opening is not None
        assert opening.equity == Decimal(110000) and opening.proof_id == proof.id
        assert not store.latest_proof().complete
        with db.transaction() as session:
            assert _trusted_opening(store, session, clocks.wall).id == opening.id
        # Later attribution changes the ledger; it must not erase the witnessed opening.
        provider.fee_visible = True
        clocks.advance(301)
        await synchronize(store, broker, now=clocks.wall)
        with db.transaction() as session:
            assert _trusted_opening(store, session, clocks.wall).id == opening.id
            sources = table("core_provider_acquisitions")
            session.execute(sources.delete().where(sources.c.id == proof.acquisition_id))
            assert _trusted_opening(store, session, clocks.wall) is None
    finally:
        db.close()
