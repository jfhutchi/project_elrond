"""Position certainty survives unknown costs without transferring core exit ownership."""

from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.integration.paper_core.test_paper_funds import FundsProvider, enter, prepare, ready
from tests.unit.operations.test_position_protection_supervisor import IDENTITY

from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.protection import PositionProtectionSupervisor
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.store import table
from quantbot.storage import StorageRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "stale", "basis", "quantity", "refresh", "expire"])
async def test_quantity_proof_and_core_delegation_never_create_legacy_exit(tmp_path, failure):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision, reasons = await enter(store, broker)
        assert not reasons and provider.orders
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.get_latest_account_snapshot("paper", certified_only=True) is None
            snapshot = repo.get_latest_account_snapshot("paper", position_certified_only=True)
            assert snapshot is not None and snapshot.positions[0].symbol == decision.symbol
        if failure == "stale":
            clocks.advance(121)
        elif failure == "quantity":
            provider.holdings[decision.symbol] += Decimal(1)
        elif failure == "basis":
            original = broker.get_positions

            async def changed_basis():
                return tuple(
                    p.model_copy(update={"average_entry_price": Decimal(90)})
                    for p in await original()
                )

            broker.get_positions = changed_basis
        elif failure in {"refresh", "expire"}:
            original = broker.get_positions

            async def acquired_positions():
                positions = await original()
                clocks.advance(121 if failure == "expire" else 1)
                if failure == "refresh":
                    ready(store)
                return positions

            broker.get_positions = acquired_positions
        supervisor = PositionProtectionSupervisor(
            database=db,
            broker=broker,
            expected_account_id="paper",
            strategy_identity=IDENTITY,
            metrics=OperationalMetrics(),
            clock=lambda: clocks.wall,
        )
        result = await supervisor.evaluate(now=clocks.wall)
        blocked = failure not in {None, "refresh"}
        assert result.unprotected_count == (1 if blocked else 0)
        with db.transaction() as session:
            assert not session.execute(select(table("position_protection_actions"))).first()
            evaluations = (
                session.execute(select(table("position_protection_evaluations"))).mappings().all()
            )
            assert evaluations[0]["reason"] == (
                "CORE_GUARDIAN_AUTHORITY_UNAVAILABLE" if blocked else "CORE_GUARDIAN_OWNS_EXIT"
            )
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper", certified_only=True)
                is None
            )
        # Guardian remains the sole exit owner, including when retained protection is degraded.
        from tests.integration.paper_core.test_paper_funds import close

        if failure is None:
            await close(store, broker, clocks)
            assert len(provider.orders) == 2
            assert provider.orders[-1]["qty"] == provider.orders[0]["qty"]
            assert store.documents("core_round_trips")[0]["open_quantity"] == "0"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inherited_protection_uses_quantity_proof_without_acquiring_core_lot(tmp_path):
    class Inherited(FundsProvider):
        def __init__(self, clocks):
            super().__init__(clocks)
            self.holdings = {"SPY": Decimal(2)}

    db, store, broker, _, clocks = await prepare(
        tmp_path, provider_factory=Inherited, legacy_owners=(("SPY", IDENTITY.strategy_id),)
    )
    try:
        await enter(store, broker)
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        result = await PositionProtectionSupervisor(
            database=db,
            broker=broker,
            expected_account_id="paper",
            strategy_identity=IDENTITY,
            metrics=OperationalMetrics(),
            clock=lambda: clocks.wall,
        ).evaluate(now=clocks.wall)
        assert result.position_count == 2 and result.unprotected_count == 0
        with db.transaction() as session:
            rows = (
                session.execute(select(table("position_protection_evaluations"))).mappings().all()
            )
            inherited = next(r for r in rows if r["symbol"] == "SPY")
            assert inherited["strategy_id"] == IDENTITY.strategy_id and inherited["reconciled"] == 1
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper", certified_only=True)
                is None
            )
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("possible_send", [False, True])
async def test_prepared_legacy_exit_cannot_cross_core_ownership_boundary(
    tmp_path, monkeypatch, possible_send
):
    from quantbot.paper_core import protection_bridge
    from quantbot.risk.protection import ProtectionPolicy

    db, store, broker, _, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        original = broker.get_positions

        async def lower_mark():
            return tuple(
                p.model_copy(update={"market_price": Decimal(80), "market_value": p.quantity * 80})
                for p in await original()
            )

        broker.get_positions = lower_mark
        supervisor = PositionProtectionSupervisor(
            database=db,
            broker=broker,
            expected_account_id="paper",
            strategy_identity=IDENTITY,
            metrics=OperationalMetrics(),
            clock=lambda: clocks.wall,
            policy=ProtectionPolicy(hard_stop_bps=Decimal(1)),
        )
        # Simulate a legacy action prepared before the core ownership boundary became visible.
        with monkeypatch.context() as patch:
            patch.setattr(protection_bridge, "core_reserves_symbol", lambda *args: ())
            result = await supervisor.evaluate(now=clocks.wall)
        assert result.exit_required_count == 1
        if possible_send:
            from sqlalchemy import update

            with db.transaction() as session:
                action = (
                    session.execute(select(table("position_protection_actions"))).mappings().one()
                )
                session.execute(
                    update(table("order_intents"))
                    .where(table("order_intents").c.intent_id == action["intent_id"])
                    .values(state="SUBMISSION_UNKNOWN")
                )
        calls = []

        class Submitter:
            async def submit_durable(self, *args, **kwargs):
                calls.append(("POST", args))

            async def reconcile_durable(self, *args, **kwargs):
                calls.append(("GET_ONLY", args))

        from dataclasses import replace

        await replace(supervisor, submitter=Submitter())._dispatch_cycle_actions(
            cycle_id=result.cycle_id, moment=clocks.wall
        )
        assert [c[0] for c in calls] == (["GET_ONLY"] if possible_send else [])
        with db.transaction() as session:
            action = session.execute(select(table("position_protection_actions"))).mappings().one()
            assert action["status"] == ("UNCERTAIN" if possible_send else "PENDING")
            assert StorageRepository(session).get_order_intent(action["intent_id"]).state.value == (
                "SUBMISSION_UNKNOWN" if possible_send else "RISK_APPROVED"
            )
            assert (
                StorageRepository(session).get_order_intent(action["intent_id"]).strategy_id
                == IDENTITY.strategy_id
            )
    finally:
        db.close()
