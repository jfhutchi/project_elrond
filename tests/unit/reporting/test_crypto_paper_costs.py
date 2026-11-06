"""Coin fees must not crash reporting or turn stock fee estimates into crypto net."""

import pytest

from tests.unit.reporting.test_paper_costs import fixture, report


def crypto_fixture():
    data = fixture()
    trip = data["trips"][1]
    buy, sell = data["fills"][2:]
    buy["symbol"] = sell["symbol"] = "BTC/USD"
    sell["quantity"] = "0.09"
    trip.update(
        pnl_basis="WEIGHTED_ENTRY_BASIS_WITH_ASSET_FEES_V1",
        asset_fee_quantity="0.01",
        asset_fee_cost_basis="0.1",
        asset_fee_allocation_ids=["verified-allocation"],
        cashflow_pnl="-0.091",
        gross_pnl="0.009",
    )
    return data


def test_coin_fee_report_keeps_cashflow_and_price_performance_separate():
    day = report(crypto_fixture())["days"][1]
    assert day["gross_realized"] == "0.009"
    assert day["cashflow_realized"] == "-0.091"
    assert day["asset_fee_cost_basis"] == "0.1"
    assert day["fee_scenario"] is None
    assert day["net_after_observed_fees"] is None
    assert day["scope_reason"] == "CRYPTO_COST_FINALITY_UNVERIFIED"
    assert day["trades"][0]["asset_fee_quantity"] == "0.01"


@pytest.mark.parametrize(
    "field,value",
    [
        ("cashflow_pnl", "0"),
        ("asset_fee_cost_basis", "0.2"),
        ("asset_fee_quantity", "0.02"),
        ("asset_fee_allocation_ids", []),
        ("pnl_basis", "UNKNOWN"),
    ],
)
def test_coin_fee_report_rejects_unreconciled_basis(field, value):
    data = crypto_fixture()
    data["trips"][1][field] = value
    with pytest.raises(ValueError, match="ASSET_FEE_BASIS_MISMATCH"):
        report(data)


def test_crypto_without_posted_coin_fee_does_not_inherit_stock_fee_scenario():
    data = fixture()
    for fill in data["fills"][2:]:
        fill["symbol"] = "BTC/USD"
    day = report(data)["days"][1]
    assert day["fee_scenario"] is None
    assert day["scope_reason"] == "CRYPTO_COST_FINALITY_UNVERIFIED"
