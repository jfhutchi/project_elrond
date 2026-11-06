"""Portfolio reservations retain gross ownership while custody is unresolved."""

from decimal import Decimal

import pytest
from tests.unit.paper_core.test_crypto_custody import owned_fixture
from tests.unit.paper_core.test_operational_crypto_policy import crypto_policy

from quantbot.domain import Position
from quantbot.paper_core.portfolio import portfolio_budget
from quantbot.paper_core.store import CoreStore


@pytest.mark.asyncio
@pytest.mark.parametrize("custody", [Decimal(".009975"), Decimal(0)])
@pytest.mark.parametrize("scope_valid", [True, False])
async def test_unresolved_crypto_preserves_gross_exposure_and_occupied_slot(
    tmp_path, monkeypatch, custody, scope_valid
):
    db, store, broker, _, _ = await owned_fixture(tmp_path)
    try:
        # Authority is exercised separately by the custody receipt tests. This test
        # isolates portfolio computation using actual retained owned intents/fills.
        monkeypatch.setattr(CoreStore, "entry_policy", property(lambda _: crypto_policy()))
        monkeypatch.setattr(
            "quantbot.paper_core.portfolio.universe_health_objections", lambda *a: ()
        )
        monkeypatch.setattr(
            "quantbot.paper_core.continuous_capital.continuous_budget_authority",
            lambda *a, **kw: ("verified-anchor", ()),
        )
        checks = []

        def scope(*args, **kwargs):
            checks.append(args[2].id)
            return () if scope_valid else ("CRYPTO_CUSTODY_BASIS_CHANGED",)

        monkeypatch.setattr("quantbot.paper_core.portfolio.entry_inventory_objections", scope)
        positions = (
            (
                Position(
                    symbol="BTC/USD",
                    quantity=custody,
                    market_price=50000,
                    market_value=custody * 50000,
                    average_entry_price=50000,
                ),
            )
            if custody
            else ()
        )
        proof = store.latest_proof().model_copy(
            update={
                "recorded_at_utc": broker.now,
                "complete": False,
                "expected_quantities": (("BTC/USD", Decimal(".01")),),
                "actual_quantities": (("BTC/USD", custody),) if custody else (),
                "positions": positions,
                "quarantined_symbols": ("BTC/USD",),
                "custody_evidence_ids": ("receipt",),
            }
        )
        with db.transaction() as session:
            result = portfolio_budget(store, session, proof, now=broker.now)
        assert checks == [proof.id]
        assert "BTC/USD" in result.occupied_symbols
        assert result.gross_headroom == proof.account.equity * Decimal(".95") - Decimal(500)
        if scope_valid:
            assert "PORTFOLIO_UNATTRIBUTED_POSITION" not in result.objections
        else:
            assert "CRYPTO_CUSTODY_BASIS_CHANGED" in result.objections
            assert "PORTFOLIO_UNATTRIBUTED_POSITION" in result.objections
    finally:
        db.close()
