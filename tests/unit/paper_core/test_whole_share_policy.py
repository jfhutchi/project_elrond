"""A whole-share minimum changes notional targeting, never the existing loss budget."""

from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.domain import Account
from quantbot.paper_core.release import PreparedRelease


def policy_module():
    from quantbot.paper_core import entry_policy

    return entry_policy


@pytest.mark.parametrize(
    "price,cash,bp,allowed",
    [
        ("12.32", "96.92", "96.92", True),
        ("93.66", "96.92", "96.92", False),
        ("97", "96.92", "200", False),
        ("124", "500", "500", False),
    ],
)
def test_one_share_minimum_preserves_cash_reserve_and_loss_budget(price, cash, bp, allowed):
    module = policy_module()
    policy = module.EntryPolicy(symbols=("AAPL", "F"))
    account = Account(account_id="paper", equity=495, cash=cash, buying_power=bp, currency="USD")
    quantity = module.entry_quantity(account, Decimal(price), policy=policy, fractionable=False)
    assert quantity == 1
    assert (
        module.entry_risk_allowed(account, quantity, Decimal(price), minimum_whole_share=True)
        is allowed
    )
    assert not module.entry_risk_allowed(
        account, quantity * 2, Decimal(price), minimum_whole_share=True
    )


def test_fractional_path_and_whole_flooring_keep_normal_target():
    module = policy_module()
    account = Account(
        account_id="paper", equity=100000, cash=100000, buying_power=100000, currency="USD"
    )
    policy = module.EntryPolicy(symbols=("F",))
    assert module.entry_quantity(account, Decimal("12"), policy=policy, fractionable=False) == 41
    assert module.entry_quantity(
        account, Decimal("12"), policy=policy, fractionable=True
    ) == Decimal("41.25")
    assert module.entry_quantity(
        account, Decimal("12"), policy=None, fractionable=False
    ) == Decimal("41.666666")


def test_fractional_target_leaves_room_for_normal_equity_mark_movement():
    module = policy_module()
    policy = module.EntryPolicy(symbols=("AAPL",))
    account = Account(
        account_id="paper", equity=495, cash="96.92", buying_power="96.92", currency="USD"
    )
    quantity = module.entry_quantity(account, Decimal(342), policy=policy, fractionable=True)
    current = account.model_copy(update={"equity": Decimal(494)})
    assert module.entry_risk_allowed(current, quantity, Decimal(342))
    assert quantity * 342 < account.equity * Decimal(".005") * Decimal(".991")


def test_legacy_manifest_hash_survives_optional_policy_contract():
    path = (
        Path(__file__).resolve().parents[3]
        / "docs/evidence/paper-core/submission-2026-10-15/executable-manifest.json"
    )
    old = PreparedRelease.model_validate_json(path.read_text(encoding="utf-8"))
    assert old.id == "aaa40bdbc8f49b8f63ad736740cbffb0fc00f9d14f96e344e0aba4d68c6d78d0"
    assert old.config.entry_policy is None
    assert "entry_policy" not in old.config.model_dump(mode="json")
