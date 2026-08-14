from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.risk import DrawdownInputError, calculate_drawdown
from quantbot.strategy import load_strategy_config

ROOT = Path(__file__).parents[3]
CONFIG_PATH = ROOT / "config" / "strategy-v1.yaml"


@pytest.mark.parametrize(
    ("drawdown", "multiplier", "entry_halted", "liquidation_required"),
    [
        (Decimal("0"), Decimal("1"), False, False),
        (Decimal("0.049999"), Decimal("1"), False, False),
        (Decimal("0.05"), Decimal("0.75"), False, False),
        (Decimal("0.099999"), Decimal("0.75"), False, False),
        (Decimal("0.10"), Decimal("0.50"), False, False),
        (Decimal("0.149999"), Decimal("0.50"), False, False),
        (Decimal("0.15"), Decimal("0"), True, False),
        (Decimal("0.199999"), Decimal("0"), True, False),
        (Decimal("0.20"), Decimal("0"), True, True),
        (Decimal("0.25"), Decimal("0"), True, True),
    ],
)
def test_drawdown_boundaries(
    drawdown: Decimal,
    multiplier: Decimal,
    entry_halted: bool,
    liquidation_required: bool,
) -> None:
    config = load_strategy_config(CONFIG_PATH)
    high_water = Decimal("100000")

    state = calculate_drawdown(
        current_equity=high_water * (Decimal("1") - drawdown),
        previous_high_water=high_water,
        config=config,
    )

    assert state.high_water_equity == high_water
    assert state.drawdown_fraction == drawdown
    assert state.new_risk_multiplier == multiplier
    assert state.entry_halted is entry_halted
    assert state.liquidation_required is liquidation_required


def test_new_high_updates_high_water_and_resets_drawdown() -> None:
    state = calculate_drawdown(
        current_equity=Decimal("105000"),
        previous_high_water=Decimal("100000"),
        config=load_strategy_config(CONFIG_PATH),
    )

    assert state.current_equity == Decimal("105000")
    assert state.high_water_equity == Decimal("105000")
    assert state.drawdown_fraction == 0
    assert state.new_risk_multiplier == 1
    assert state.entry_halted is False
    assert state.liquidation_required is False


def test_drawdown_recovery_has_no_hidden_hysteresis() -> None:
    config = load_strategy_config(CONFIG_PATH)
    breached = calculate_drawdown(Decimal("79000"), Decimal("100000"), config)
    recovered_below_liquidation = calculate_drawdown(Decimal("81000"), Decimal("100000"), config)
    recovered_below_entry_halt = calculate_drawdown(Decimal("86000"), Decimal("100000"), config)

    assert breached.liquidation_required is True
    assert recovered_below_liquidation.liquidation_required is False
    assert recovered_below_liquidation.entry_halted is True
    assert recovered_below_entry_halt.entry_halted is False
    assert recovered_below_entry_halt.new_risk_multiplier == Decimal("0.50")


@pytest.mark.parametrize(
    ("current", "previous", "message"),
    [
        (Decimal("-1"), Decimal("100"), "current_equity"),
        (Decimal("0"), None, "high-water equity"),
        (Decimal("1"), Decimal("0"), "previous_high_water"),
        (Decimal("1"), Decimal("-1"), "previous_high_water"),
    ],
)
def test_invalid_drawdown_inputs_fail_closed(
    current: Decimal,
    previous: Decimal | None,
    message: str,
) -> None:
    with pytest.raises(DrawdownInputError, match=message):
        calculate_drawdown(current, previous, load_strategy_config(CONFIG_PATH))
