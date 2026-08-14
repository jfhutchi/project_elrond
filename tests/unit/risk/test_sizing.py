from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from quantbot.risk import (
    EntrySizingRequest,
    PortfolioRiskState,
    RiskApproval,
    RiskRejection,
    SizingCapName,
    calculate_drawdown,
    size_entry,
)
from quantbot.strategy import StrategyConfig, load_strategy_config

ROOT = Path(__file__).parents[3]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"


def strategy_config(**updates: Any) -> StrategyConfig:
    payload = load_strategy_config(CONFIG_PATH).model_dump(mode="python")
    payload.update(updates)
    return StrategyConfig.model_validate(payload)


def portfolio(**updates: Any) -> PortfolioRiskState:
    payload: dict[str, object] = {
        "account_snapshot_id": "snapshot-1",
        "target_symbol": "AAPL",
        "equity": Decimal("100000"),
        "buying_power": Decimal("100000"),
        "committed_open_risk": Decimal("0"),
        "committed_gross_exposure": Decimal("0"),
        "committed_symbol_market_value": Decimal("0"),
        "committed_position_count": 0,
        "is_new_position": True,
        "open_risk_available": True,
        "reasons": (),
    }
    payload.update(updates)
    return PortfolioRiskState.model_validate(payload)


def request(
    *,
    reference_price: Decimal = Decimal("50"),
    stop_distance: Decimal = Decimal("4"),
    portfolio_state: PortfolioRiskState | None = None,
    drawdown_fraction: Decimal = Decimal("0"),
) -> EntrySizingRequest:
    config = load_strategy_config(CONFIG_PATH)
    return EntrySizingRequest(
        symbol="AAPL",
        reference_price=reference_price,
        stop_distance=stop_distance,
        portfolio=portfolio_state or portfolio(),
        drawdown=calculate_drawdown(
            Decimal("100000") * (Decimal("1") - drawdown_fraction),
            Decimal("100000"),
            config,
        ),
    )


def test_known_half_percent_risk_example_uses_whole_share_floor() -> None:
    result = size_entry(request(), load_strategy_config(CONFIG_PATH))

    assert isinstance(result, RiskApproval)
    assert result.approved is True
    assert result.symbol == "AAPL"
    assert result.quantity == Decimal("125")
    assert result.quantity == result.quantity.to_integral_value()
    assert result.stop_distance == Decimal("4")
    assert result.effective_risk_fraction == Decimal("0.005")
    assert result.risk_budget == Decimal("500")
    assert result.caps.per_trade_risk == Decimal("125")
    assert result.caps.portfolio_open_risk == Decimal("1250")
    assert result.caps.position_value == Decimal("200")
    assert result.caps.gross_exposure == Decimal("2000")
    assert result.caps.buying_power == Decimal("2000")
    assert result.limiting_caps == (SizingCapName.PER_TRADE_RISK,)
    assert result.projected_position_value == Decimal("6250")
    assert result.projected_gross_exposure == Decimal("6250")
    assert result.projected_open_risk == Decimal("500")


@pytest.mark.parametrize(
    ("updates", "expected_quantity", "expected_cap"),
    [
        (
            {"committed_open_risk": Decimal("4800")},
            Decimal("50"),
            SizingCapName.PORTFOLIO_OPEN_RISK,
        ),
        (
            {"committed_symbol_market_value": Decimal("9000")},
            Decimal("20"),
            SizingCapName.POSITION_VALUE,
        ),
        (
            {"committed_gross_exposure": Decimal("99000")},
            Decimal("20"),
            SizingCapName.GROSS_EXPOSURE,
        ),
        (
            {"buying_power": Decimal("500")},
            Decimal("10"),
            SizingCapName.BUYING_POWER,
        ),
    ],
)
def test_each_portfolio_cap_can_limit_quantity(
    updates: dict[str, object],
    expected_quantity: Decimal,
    expected_cap: SizingCapName,
) -> None:
    result = size_entry(
        request(portfolio_state=portfolio(**updates)),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskApproval)
    assert result.quantity == expected_quantity
    assert result.limiting_caps == (expected_cap,)


def test_tied_limiting_caps_are_reported_in_formula_order() -> None:
    result = size_entry(
        request(
            portfolio_state=portfolio(
                committed_symbol_market_value=Decimal("9000"),
                committed_gross_exposure=Decimal("99000"),
            )
        ),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskApproval)
    assert result.quantity == Decimal("20")
    assert result.limiting_caps == (
        SizingCapName.POSITION_VALUE,
        SizingCapName.GROSS_EXPOSURE,
    )


@pytest.mark.parametrize(
    ("drawdown", "quantity"),
    [
        (Decimal("0.05"), Decimal("93")),
        (Decimal("0.10"), Decimal("62")),
    ],
)
def test_drawdown_reduces_new_risk_without_increasing_quantity(
    drawdown: Decimal,
    quantity: Decimal,
) -> None:
    result = size_entry(
        request(drawdown_fraction=drawdown),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskApproval)
    assert result.quantity == quantity


def test_fifteen_percent_drawdown_blocks_entry() -> None:
    result = size_entry(
        request(drawdown_fraction=Decimal("0.15")),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskRejection)
    assert result.reasons == ("DRAWDOWN_ENTRY_HALT",)


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"committed_open_risk": Decimal("5000")},
            "PORTFOLIO_OPEN_RISK_LIMIT_REACHED",
        ),
        (
            {"committed_symbol_market_value": Decimal("10000")},
            "POSITION_VALUE_LIMIT_REACHED",
        ),
        (
            {"committed_gross_exposure": Decimal("100000")},
            "GROSS_EXPOSURE_LIMIT_REACHED",
        ),
        ({"buying_power": Decimal("0")}, "BUYING_POWER_EXHAUSTED"),
    ],
)
def test_exhausted_cap_returns_specific_rejection(
    updates: dict[str, object],
    reason: str,
) -> None:
    result = size_entry(
        request(portfolio_state=portfolio(**updates)),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskRejection)
    assert result.reasons == (reason, "QUANTITY_BELOW_ONE_SHARE")
    assert result.caps is not None


def test_position_limit_applies_only_to_new_symbols() -> None:
    new_position = size_entry(
        request(
            portfolio_state=portfolio(
                committed_position_count=20,
                is_new_position=True,
            )
        ),
        load_strategy_config(CONFIG_PATH),
    )
    existing_position = size_entry(
        request(
            portfolio_state=portfolio(
                committed_position_count=20,
                is_new_position=False,
            )
        ),
        strategy_config(allow_pyramiding=True),
    )

    assert isinstance(new_position, RiskRejection)
    assert new_position.reasons == ("POSITION_LIMIT_REACHED",)
    assert isinstance(existing_position, RiskApproval)


def test_default_configuration_rejects_pyramiding() -> None:
    result = size_entry(
        request(portfolio_state=portfolio(is_new_position=False)),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskRejection)
    assert result.reasons == ("PYRAMIDING_DISABLED",)


def test_invalid_inputs_return_all_reasons_in_stable_order() -> None:
    invalid_portfolio = portfolio().model_copy(
        update={
            "equity": Decimal("-1"),
            "buying_power": Decimal("-2"),
            "committed_open_risk": Decimal("-3"),
            "committed_gross_exposure": Decimal("-4"),
            "committed_symbol_market_value": Decimal("-5"),
            "committed_position_count": -1,
            "open_risk_available": False,
            "reasons": ("OPEN_RISK_UNAVAILABLE", "UNSUPPORTED_SHORT_POSITION"),
        }
    )
    invalid_request = request(
        reference_price=Decimal("-1"),
        stop_distance=Decimal("0"),
        portfolio_state=invalid_portfolio,
    )

    result = size_entry(invalid_request, load_strategy_config(CONFIG_PATH))

    assert isinstance(result, RiskRejection)
    assert result.caps is None
    assert result.reasons == (
        "NON_POSITIVE_EQUITY",
        "NON_POSITIVE_REFERENCE_PRICE",
        "NON_POSITIVE_STOP_DISTANCE",
        "NEGATIVE_BUYING_POWER",
        "NEGATIVE_COMMITTED_OPEN_RISK",
        "NEGATIVE_COMMITTED_GROSS_EXPOSURE",
        "NEGATIVE_COMMITTED_SYMBOL_VALUE",
        "INVALID_POSITION_COUNT",
        "OPEN_RISK_UNAVAILABLE",
        "UNSUPPORTED_SHORT_POSITION",
    )


def test_sub_share_quantity_is_rejected() -> None:
    result = size_entry(
        request(reference_price=Decimal("100"), stop_distance=Decimal("1000")),
        load_strategy_config(CONFIG_PATH),
    )

    assert isinstance(result, RiskRejection)
    assert result.reasons == ("QUANTITY_BELOW_ONE_SHARE",)


def test_sizing_request_must_match_portfolio_target_symbol() -> None:
    with pytest.raises(ValueError, match="portfolio target symbol"):
        EntrySizingRequest(
            symbol="MSFT",
            reference_price=Decimal("50"),
            stop_distance=Decimal("4"),
            portfolio=portfolio(),
            drawdown=calculate_drawdown(
                Decimal("100000"),
                Decimal("100000"),
                load_strategy_config(CONFIG_PATH),
            ),
        )
