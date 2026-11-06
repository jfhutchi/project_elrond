"""A pre-I/O retry retains one prepaid POST without relaxing account limits."""

import sqlite3

import pytest
from tests.fixtures.core_m3 import Clocks

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core import account_control as control


class Provider:
    def __init__(self):
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url))
        return BrokerTransportResponse(status_code=200, headers={}, content=b"{}")


def transport(tmp_path, monkeypatch):
    path = tmp_path / "control.db"
    control.initialize_account_control(path)
    monkeypatch.setattr(control, "observed_host_identity", lambda: "h" * 64)
    monkeypatch.setattr(control, "observed_host_epoch", lambda: "e" * 64)
    config = control.AccountRequestControl(
        control_path=str(path),
        account_scope="a" * 64,
        host_identity="h" * 64,
        host_epoch="e" * 64,
    )
    clocks, provider = Clocks(), Provider()
    return control.BudgetedTransport(provider, config, "trader", clocks), provider, clocks


@pytest.mark.asyncio
async def test_repeated_pre_io_reservations_keep_one_slot_and_one_post(tmp_path, monkeypatch):
    wrapped, provider, clocks = transport(tmp_path, monkeypatch)
    for _ in range(45):
        wrapped.reserve_send("same-client")
    budget = wrapped.budgets["trading"]
    assert budget.status(now_monotonic=clocks.mono)["role_used"] == {"trader": 1}
    for _ in range(39):
        assert budget.acquire("trader", purpose="RECOVERY GET", now_monotonic=clocks.mono)
    assert not budget.acquire("trader", purpose="RECOVERY GET", now_monotonic=clocks.mono)
    wrapped.reserve_send("same-client")  # Reusing its own slot works at the role limit.
    await wrapped.request(
        "POST",
        "https://paper-api.alpaca.markets/v2/orders",
        json_body={"client_order_id": "same-client"},
    )
    with pytest.raises(control.LocalRequestBudgetRefusal):
        await wrapped.request(
            "POST",
            "https://paper-api.alpaca.markets/v2/orders",
            json_body={"client_order_id": "same-client"},
        )
    assert provider.requests == [("POST", "https://paper-api.alpaca.markets/v2/orders")]
    assert budget.status(now_monotonic=clocks.mono)["role_used"] == {"trader": 40}


@pytest.mark.asyncio
async def test_expired_reservation_refused_at_cap_remains_reusable_after_window(
    tmp_path, monkeypatch
):
    wrapped, provider, clocks = transport(tmp_path, monkeypatch)
    wrapped.reserve_send("same-client")
    budget = wrapped.budgets["trading"]
    clocks.advance(61)
    for _ in range(40):
        assert budget.acquire("trader", purpose="RECOVERY GET", now_monotonic=clocks.mono)
    with pytest.raises(control.LocalRequestBudgetRefusal):
        await wrapped.request(
            "POST",
            "https://paper-api.alpaca.markets/v2/orders",
            json_body={"client_order_id": "same-client"},
        )
    assert not provider.requests
    clocks.advance(61)
    wrapped.reserve_send("same-client")
    with sqlite3.connect(budget.path) as db:
        assert db.execute("SELECT count(*) FROM charges WHERE consumed=0").fetchone()[0] == 1
    await wrapped.request(
        "POST",
        "https://paper-api.alpaca.markets/v2/orders",
        json_body={"client_order_id": "same-client"},
    )
    assert len(provider.requests) == 1


def test_reused_reservation_cannot_cross_scope_role_or_epoch(tmp_path):
    path = tmp_path / "control.db"
    control.initialize_account_control(path)
    budget = control.RequestBudget(path, "scope", "epoch")
    identity = budget.reserve("trader", purpose="POST_RESERVATION client", now_monotonic=100)
    assert identity is not None
    for foreign, role in (
        (control.RequestBudget(path, "other", "epoch"), "trader"),
        (control.RequestBudget(path, "scope", "epoch"), "guardian"),
        (control.RequestBudget(path, "scope", "other"), "trader"),
    ):
        assert (
            foreign.reserve(
                role, purpose="POST_RESERVATION client", now_monotonic=100, reuse_identity=identity
            )
            is None
        )
