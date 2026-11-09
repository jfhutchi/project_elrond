"""A rejected attempt must defer acknowledgement without crashing scheduled learning."""

from datetime import timedelta

import pytest
from tests.integration.paper_core.test_daily_paper_loop import SESSIONS, DailyProvider, advance_to
from tests.integration.paper_core.test_paper_funds import enter, prepare

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core.roles import daily_consideration
from quantbot.storage import StorageRepository


class RejectingProvider(DailyProvider):
    async def request(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/orders"):
            return BrokerTransportResponse(
                status_code=422, headers={}, content=b'{"message":"rejected"}'
            )
        return await super().request(method, url, **kwargs)


@pytest.mark.asyncio
async def test_rejected_claim_defers_acknowledgement_without_new_authorities(tmp_path):
    db, store, broker, _, clocks = await prepare(tmp_path, provider_factory=RejectingProvider)
    try:
        decision, _ = await enter(store, broker)
        assert decision.action == "BUY"
        with db.transaction() as session:
            intent = StorageRepository(session).get_order_intent(store.links()[0]["intent_id"])
            assert intent.state.value == "REJECTED"
        advance_to(clocks, SESSIONS[0] + timedelta(hours=7))
        before = store.documents("core_activation_authorities")
        result = await daily_consideration(store, broker, broker.config)
        assert "ACKNOWLEDGEMENT_DEFERRED:ACTIVATION_UNRESOLVED_CLAIM" in result
        runs = store.documents("core_learning_runs")
        assert runs[-1]["status"] == "NO_CHANGE"
        assert runs[-1]["trial_charge_id"] is None
        assert store.documents("core_activation_authorities") == before
        assert await daily_consideration(store, broker, broker.config) == result
        assert store.documents("core_learning_runs") == runs
        assert store.documents("core_activation_authorities") == before
        assert not store.documents("core_round_trips")
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason", ["ACTIVATION_UNRESOLVED_CLAIM", "ACTIVATION_UNRESOLVED_SUBMISSION"]
)
async def test_locked_boundary_race_defers_acknowledgement(tmp_path, monkeypatch, reason):
    """A boundary change after the read-only preflight still fails closed explicitly."""
    from quantbot.paper_core import activation
    from quantbot.storage import StateConflictError

    db, store, broker, _, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        advance_to(clocks, SESSIONS[0] + timedelta(hours=7))
        original = activation._boundary
        checks = 0

        def concurrent_boundary(session, *, cutoff):
            nonlocal checks
            original(session, cutoff=cutoff)
            checks += 1
            if checks == 2:
                # Fault injection at the independent writer's commit boundary.
                raise StateConflictError(reason)

        monkeypatch.setattr(activation, "_boundary", concurrent_boundary)
        before = store.documents("core_activations")
        result = await daily_consideration(store, broker, broker.config)
        assert "ACKNOWLEDGEMENT_DEFERRED:" + reason in result
        assert store.documents("core_activations") == before
        assert store.documents("core_learning_runs")[-1]["status"] == "NO_CHANGE"
    finally:
        db.close()
