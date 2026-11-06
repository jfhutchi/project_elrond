"""Continuous risk authority using isolated PAPER protocol fixtures only."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tests.integration.paper_core.test_paper_funds import prepare

from quantbot.paper_core.capital import opening_for_session
from quantbot.paper_core.crypto_policy import CryptoPaperPolicy
from quantbot.paper_core.entry_policy import EntryPolicy
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.store import CoreStore
from quantbot.paper_core.time_evidence import acquire_time_evidence


async def fixture(tmp_path, *, opening=True, initial_wall=None):
    policy = EntryPolicy(
        contract="RESOURCE_BOUNDED_PAPER_V1",
        universe="ALPACA_ASSETS_V1",
        symbols=(),
        crypto=CryptoPaperPolicy(),
    )
    values = await prepare(
        tmp_path,
        entry_policy=policy,
        initial_wall=initial_wall or datetime(2026, 10, 14, 13, 30, 3, tzinfo=UTC),
        warmup_seconds=0,
    )
    db, store, broker, provider, clocks = values
    broker.store = store
    if opening:
        timing = await acquire_time_evidence(broker, clocks, context=store.config)
        store.record("core_time_evidence", timing)
        source = opening_for_session(store, timing, preferred_proof=store.latest_proof())
        assert source is not None
    return values


async def test_balance_alone_cannot_bootstrap_a_continuous_loss_floor(tmp_path):
    from quantbot.paper_core.continuous_capital import acquire_continuous_capital

    db, store, broker, _, _ = await fixture(tmp_path, opening=False)
    try:
        assert await acquire_continuous_capital(broker, store, store.latest_proof()) is None
        assert not any(
            d.get("contract") == "ACCOUNT_RISK_ANCHOR_V1"
            for d in store.documents("core_provider_acquisitions")
        )
    finally:
        db.close()


async def test_crypto_capital_route_retains_verified_stock_opening_capture(tmp_path):
    from quantbot.paper_core.capital import acquire_session_capital
    from quantbot.paper_core.continuous_capital import ContinuousCapitalEvidence

    db, store, broker, _, clocks = await fixture(tmp_path, opening=False)
    try:
        timing = await acquire_time_evidence(broker, clocks, context=store.config)
        store.record("core_time_evidence", timing)
        store.runtime_time_evidence_id = timing.id
        assert not store.documents("core_session_openings")
        identity = await acquire_session_capital(broker, store, store.latest_proof())
        assert identity is not None
        receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        assert receipt.complete
        assert len(store.documents("core_session_openings")) == 1
    finally:
        db.close()


async def test_actual_capture_preserves_prior_losses_and_has_no_cash_reconciliation(tmp_path):
    from quantbot.paper_core.continuous_capital import (
        AccountRiskAnchor,
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
        continuous_budget_authority,
    )

    db, store, broker, provider, clocks = await fixture(tmp_path)
    try:
        clocks.advance(300)
        provider.account_overrides["equity"] = "97500"
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        identity = await acquire_continuous_capital(broker, store, proof)
        receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        assert receipt.complete
        assert "cash_residual" not in receipt.model_dump()
        anchor = store.read("core_provider_acquisitions", receipt.anchor_id, AccountRiskAnchor)
        assert anchor.equity_floor == Decimal(100000)
        assert anchor.captured_at == proof.recorded_at_utc
        assert anchor.captured_at != anchor.risk_day_start
        with db.transaction() as session:
            _, reasons = continuous_budget_authority(
                store,
                session,
                proof,
                now=clocks.wall,
                account_equity=proof.account.equity,
                loss_bps=200,
            )
        assert "ACCOUNT_CARRY_FORWARD_LOSS_HALT" in reasons
    finally:
        db.close()


async def test_anchor_is_idempotent_across_restart_and_current_receipt_reuse(tmp_path):
    from quantbot.paper_core.continuous_capital import acquire_continuous_capital

    db, store, broker, _, clocks = await fixture(tmp_path)
    try:
        proof = store.latest_proof()
        first = await acquire_continuous_capital(broker, store, proof)
        broker.store = restarted = CoreStore(db, store.config, clocks=clocks)
        assert await acquire_continuous_capital(broker, restarted, proof) == first
        anchors = [
            d
            for d in store.documents("core_provider_acquisitions")
            if d.get("contract") == "ACCOUNT_RISK_ANCHOR_V1"
        ]
        assert len(anchors) == 1
    finally:
        db.close()


async def test_external_flow_stays_blocked_after_empty_later_page_and_midnight(tmp_path):
    from quantbot.paper_core.continuous_capital import (
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
        continuous_budget_authority,
    )

    db, store, broker, provider, clocks = await fixture(tmp_path)
    try:
        provider.fills = [
            {
                "id": "cash-transfer",
                "activity_type": "CSD",
                "net_amount": "100",
                "date": "2026-10-14",
            }
        ]
        identity = await acquire_continuous_capital(broker, store, store.latest_proof())
        receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        assert not receipt.complete and receipt.external_flow_ids == ("cash-transfer",)
        provider.fills = []
        clocks.advance(16 * 3600)
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        await acquire_continuous_capital(broker, store, proof)
        with db.transaction() as session:
            _, reasons = continuous_budget_authority(
                store,
                session,
                proof,
                now=clocks.wall,
                account_equity=proof.account.equity,
                loss_bps=200,
            )
        assert "ACCOUNT_EXTERNAL_CAPITAL_FLOW" in reasons
    finally:
        db.close()


async def test_date_only_crypto_fee_is_not_invented_external_cash_flow(tmp_path):
    from quantbot.paper_core.continuous_capital import (
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
    )

    db, store, broker, provider, _ = await fixture(tmp_path)
    try:
        provider.fills = [
            {
                "id": "fee",
                "activity_type": "CFEE",
                "date": "2026-10-14",
                "net_amount": "0",
                "symbol": "ETHUSD",
                "qty": "-0.0001",
            }
        ]
        identity = await acquire_continuous_capital(broker, store, store.latest_proof())
        receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        assert receipt.complete and not receipt.external_flow_ids
        assert receipt.fee_uncertainty_ids == ("fee",)
    finally:
        db.close()


async def test_stale_proof_does_not_create_an_anchor(tmp_path):
    from quantbot.paper_core.continuous_capital import acquire_continuous_capital

    db, store, broker, _, clocks = await fixture(tmp_path)
    try:
        proof = store.latest_proof()
        clocks.advance(121)
        with pytest.raises(ValueError, match="CONTINUOUS_FUNDS_UNVERIFIED"):
            await acquire_continuous_capital(broker, store, proof)
    finally:
        db.close()


async def test_retained_anchor_rejects_floor_below_its_trusted_opening(tmp_path):
    from sqlalchemy import select

    from quantbot.paper_core.continuous_capital import (
        ANCHOR_KIND,
        AccountRiskAnchor,
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
        continuous_budget_authority,
    )
    from quantbot.paper_core.store import put, table

    db, store, broker, provider, clocks = await fixture(tmp_path)
    try:
        clocks.advance(300)
        provider.account_overrides["equity"] = "97500"
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        identity = await acquire_continuous_capital(broker, store, proof)
        receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        anchor = store.read("core_provider_acquisitions", receipt.anchor_id, AccountRiskAnchor)
        weakened = anchor.model_copy(update={"equity_floor": Decimal(99000)})
        with db.transaction() as session:
            index = table("core_document_index")
            retained = dict(
                session.execute(
                    select(index).where(index.c.kind == ANCHOR_KIND, index.c.id == anchor.id)
                )
                .mappings()
                .one()
            )
            session.execute(
                index.delete().where(index.c.kind == ANCHOR_KIND, index.c.id == anchor.id)
            )
            put(session, "core_provider_acquisitions", weakened.id, weakened)
            session.execute(index.insert().values(**(retained | {"id": weakened.id})))
        with db.transaction() as session:
            _, reasons = continuous_budget_authority(
                store,
                session,
                proof,
                now=clocks.wall,
                account_equity=proof.account.equity,
                loss_bps=200,
            )
        assert "ACCOUNT_RISK_ANCHOR_UNVERIFIED" in reasons
    finally:
        db.close()


async def test_crypto_portfolio_uses_current_funds_without_equity_cash_rounding(tmp_path):
    from quantbot.paper_core.continuous_capital import acquire_continuous_capital
    from quantbot.paper_core.portfolio import portfolio_budget

    db, store, broker, _, clocks = await fixture(tmp_path)
    try:
        proof = store.latest_proof()
        await acquire_continuous_capital(broker, store, proof)
        with db.transaction() as session:
            budget = portfolio_budget(store, session, proof, now=clocks.wall)
        assert not budget.objections, budget.objections
        assert budget.opening_id is not None
    finally:
        db.close()


async def test_concurrent_same_proof_creates_one_anchor_and_returns_same_receipt(tmp_path):
    from quantbot.paper_core.continuous_capital import acquire_continuous_capital

    db, store, broker, provider, _ = await fixture(tmp_path)
    try:
        arrived = 0
        barrier = asyncio.Event()
        request = provider.request

        async def overlap(method, url, **kwargs):
            nonlocal arrived
            if "/account/activities" in url:
                arrived += 1
                if arrived == 2:
                    barrier.set()
                await barrier.wait()
            return await request(method, url, **kwargs)

        provider.request = overlap
        proof = store.latest_proof()
        results = await asyncio.gather(
            *(acquire_continuous_capital(broker, store, proof) for _ in range(2))
        )
        assert results[0] == results[1]
        assert (
            len(
                [
                    d
                    for d in store.documents("core_provider_acquisitions")
                    if d.get("contract") == "ACCOUNT_RISK_ANCHOR_V1"
                ]
            )
            == 1
        )
    finally:
        db.close()


async def test_midnight_carries_prior_loss_floor_and_sleep_uses_lower_budget(tmp_path):
    from quantbot.paper_core.continuous_capital import (
        AccountRiskAnchor,
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
        continuous_budget_authority,
    )

    db, store, broker, provider, clocks = await fixture(tmp_path)
    try:
        identity = await acquire_continuous_capital(broker, store, store.latest_proof())
        first = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        first_anchor = store.read("core_provider_acquisitions", first.anchor_id, AccountRiskAnchor)
        clocks.advance(16 * 3600)  # The next Eastern day, still inside the sleep window.
        provider.account_overrides["equity"] = "98500"
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        for _ in range(4):
            identity = await acquire_continuous_capital(broker, store, proof)
            receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
            if receipt.complete:
                break
        assert receipt.complete
        anchor = store.read("core_provider_acquisitions", receipt.anchor_id, AccountRiskAnchor)
        assert anchor.risk_day > first_anchor.risk_day
        assert anchor.equity_floor == first_anchor.equity_floor == Decimal(100000)
        assert anchor.predecessor_id == first_anchor.id
        with db.transaction() as session:
            _, night = continuous_budget_authority(
                store,
                session,
                proof,
                now=clocks.wall,
                account_equity=proof.account.equity,
                loss_bps=100,
            )
            _, day = continuous_budget_authority(
                store,
                session,
                proof,
                now=clocks.wall,
                account_equity=proof.account.equity,
                loss_bps=200,
            )
        assert "ACCOUNT_CARRY_FORWARD_LOSS_HALT" in night
        assert "ACCOUNT_CARRY_FORWARD_LOSS_HALT" not in day
    finally:
        db.close()


@pytest.mark.parametrize(
    ("origin", "transition", "hours"),
    [
        (datetime(2026, 3, 6, 14, 30, 3, tzinfo=UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC), 23),
        (
            datetime(2026, 10, 30, 13, 30, 3, tzinfo=UTC),
            datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
            25,
        ),
    ],
)
async def test_dst_day_length_and_repeated_hour_never_reset_anchor(
    tmp_path, origin, transition, hours
):
    from quantbot.paper_core.continuous_capital import (
        AccountRiskAnchor,
        ContinuousCapitalEvidence,
        acquire_continuous_capital,
    )

    db, store, broker, _, clocks = await fixture(tmp_path, initial_wall=origin)
    try:
        await acquire_continuous_capital(broker, store, store.latest_proof())
        clocks.advance((transition - clocks.wall).total_seconds())
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        for _ in range(10):
            identity = await acquire_continuous_capital(broker, store, proof)
            receipt = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
            if receipt.complete:
                break
        assert receipt.complete
        anchor = store.read("core_provider_acquisitions", receipt.anchor_id, AccountRiskAnchor)
        assert (anchor.risk_day_end - anchor.risk_day_start).total_seconds() == hours * 3600
        clocks.advance(3600)
        await synchronize(store, broker, now=clocks.wall)
        identity = await acquire_continuous_capital(broker, store, store.latest_proof())
        later = store.read("core_provider_acquisitions", identity, ContinuousCapitalEvidence)
        assert later.complete and later.anchor_id == anchor.id
    finally:
        db.close()
