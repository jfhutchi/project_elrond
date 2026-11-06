from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from quantbot.domain import Account
from quantbot.paper_core.entry_policy import (
    AssetEligibility,
    EntryPolicy,
    eligibility_objections,
    entry_quantity,
    entry_risk_allowed,
)


def crypto_policy():
    return EntryPolicy.model_validate(
        {
            "contract": "RESOURCE_BOUNDED_PAPER_V1",
            "symbols": [],
            "universe": "ALPACA_ASSETS_V1",
            "crypto": {},
        }
    )


def eligibility(**changes):
    values = dict(
        symbol="BTC/USD",
        asset_class="crypto",
        entry_policy_id="a" * 64,
        fractionable=True,
        observed_at=datetime(2026, 11, 6, 20, tzinfo=UTC),
        raw_digest="b" * 64,
        minimum_quantity="0.000013134",
        quantity_step="0.000000001",
        price_increment="0.000000001",
        time_in_force="GTC",
        crypto_status="ACTIVE",
    )
    values.update(changes)
    return AssetEligibility.model_validate(values)


def test_crypto_requires_explicit_policy_and_exact_pair():
    legacy = EntryPolicy(
        contract="RESOURCE_BOUNDED_PAPER_V1", symbols=(), universe="ALPACA_ASSETS_V1"
    )
    assert "crypto" not in legacy.model_dump(mode="json")
    assert not legacy.permits("BTC/USD")
    approved = crypto_policy()
    assert approved.permits("BTC/USD")
    assert approved.permits("AAPL")
    assert not approved.permits("DOGE/USD")
    assert not approved.permits("BTC/EUR")
    assert approved.id != legacy.id


@pytest.mark.parametrize(
    "changes",
    [
        {"time_in_force": "DAY"},
        {"crypto_status": None},
        {"quantity_step": None},
        {"minimum_quantity": "0"},
        {"symbol": "BTCUSD"},
        {"fractionable": False},
    ],
)
def test_crypto_asset_requires_complete_broker_terms(changes):
    with pytest.raises(ValidationError):
        eligibility(**changes)


def test_broker_minimum_and_increment_are_replayed():
    receipt = eligibility()
    at = receipt.observed_at
    assert eligibility_objections(
        receipt, symbol="BTC/USD", now=at, quantity=Decimal("0.000013133")
    ) == ("ASSET_CRYPTO_MINIMUM_QUANTITY",)
    assert eligibility_objections(
        receipt, symbol="BTC/USD", now=at, quantity=Decimal("0.0000131345")
    ) == ("ASSET_CRYPTO_QUANTITY_INCREMENT",)
    assert (
        eligibility_objections(receipt, symbol="BTC/USD", now=at, quantity=Decimal("0.000013134"))
        == ()
    )


def test_equity_receipt_keeps_legacy_identity_shape():
    receipt = AssetEligibility(
        symbol="AAPL",
        entry_policy_id="a" * 64,
        fractionable=True,
        observed_at=datetime.now(UTC),
        raw_digest="b" * 64,
    )
    assert "quantity_step" not in receipt.model_dump(mode="json")
    assert "crypto_status" not in receipt.model_dump(mode="json")


def test_crypto_size_reduces_at_sleep_boundary_and_does_not_round_up_minimum():
    policy = crypto_policy()
    account = Account(
        account_id="paper",
        currency="USD",
        cash=Decimal(500),
        equity=Decimal(500),
        buying_power=Decimal(1000),
        non_marginable_buying_power=Decimal(500),
    )
    day = datetime(2026, 11, 7, 2, 59, tzinfo=UTC)
    night = datetime(2026, 11, 7, 3, tzinfo=UTC)
    asset = eligibility(observed_at=night)
    day_qty = entry_quantity(
        account, Decimal(75000), policy=policy, fractionable=True, eligibility=asset, at=day
    )
    night_qty = entry_quantity(
        account, Decimal(75000), policy=policy, fractionable=True, eligibility=asset, at=night
    )
    assert day_qty == Decimal("0.000033000")
    assert night_qty == Decimal("0.000016500")
    assert (
        entry_quantity(
            account, Decimal(150000), policy=policy, fractionable=True, eligibility=asset, at=night
        )
        == 0
    )


def test_crypto_risk_cannot_spend_margin_or_unknown_cash_capacity():
    policy = crypto_policy()
    asset = eligibility(entry_policy_id=policy.id)
    account = Account(
        account_id="paper",
        currency="USD",
        cash=Decimal(500),
        equity=Decimal(500),
        buying_power=Decimal(1000),
        non_marginable_buying_power=Decimal(".50"),
    )
    assert not entry_risk_allowed(
        account,
        Decimal("0.00002"),
        Decimal(75000),
        policy=policy,
        eligibility=asset,
        at=asset.observed_at,
    )
    assert not entry_risk_allowed(
        account.model_copy(update={"non_marginable_buying_power": None}),
        Decimal("0.00002"),
        Decimal(75000),
        policy=policy,
        eligibility=asset,
        at=asset.observed_at,
    )


def test_partial_quantity_scope_does_not_claim_reconciliation():
    from quantbot.paper_core.contracts import ReconciliationProof

    proof = ReconciliationProof(
        recorded_at_utc=datetime(2026, 11, 6, 20, tzinfo=UTC),
        evidence_lane="BROKER_PAPER",
        account=Account(account_id="paper", currency="USD", cash=500, equity=500, buying_power=500),
        baseline_id="baseline",
        generation=1,
        fill_ids=(),
        expected_quantities=(("BTC/USD", Decimal(".01")),),
        actual_quantities=(("BTC/USD", Decimal(".009975")),),
        positions=(),
        expected_cash=None,
        cash_complete=False,
        acquisition_id="source",
        complete=False,
        funds_authority="BROKER_PAPER_BALANCE_V1",
        quarantined_symbols=("BTC/USD",),
        custody_evidence_ids=("receipt",),
    )
    assert proof.quantity_scope_shape_valid
    assert not proof.complete and proof.expected_quantities != proof.actual_quantities
    for changes in (
        {"complete": True},
        {"cash_complete": True},
        {"custody_evidence_ids": ()},
        {"actual_quantities": (("BTC/USD", Decimal(".01001")),)},
        {"quarantined_symbols": ("ETH/USD",)},
        {"evidence_lane": "SYNTHETIC_TEST"},
    ):
        assert not proof.model_copy(update=changes).quantity_scope_shape_valid
