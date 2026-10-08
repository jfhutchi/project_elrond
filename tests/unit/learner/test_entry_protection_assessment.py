from importlib import import_module
from types import SimpleNamespace

import pytest

from quantbot.learner.five_minute_dry_run import ReadOnlyStore, operational_objections
from tests.unit.operations.test_position_protection_supervisor import (
    ACCOUNT_ID,
    NOW,
    FakeBroker,
    account,
    position,
    seed_local,
    supervisor,
)
from tests.unit.operations.test_position_protection_supervisor import (
    database as database,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("price,healthy", [("100", True), ("75", False)])
async def test_real_assessment_refresh_preserves_rules_and_cannot_send_legacy_orders(
    database,
    price,
    healthy,
):
    refresh = getattr(
        import_module("quantbot.learner.exploration_execution"), "refresh_entry_protection", None
    )
    assert callable(refresh), "entry needs a genuine fresh protection assessment"
    positions = (position("SPY", quantity="0.024862", price=price),)
    seed_local(database, positions)
    broker = FakeBroker(account(), positions)
    protection = supervisor(database, broker)

    class ForbiddenSubmitter:
        async def submit(self, *args, **kwargs):
            pytest.fail("entry assessment must never send a legacy protective order")

        async def submit_durable(self, *args, **kwargs):
            pytest.fail("entry assessment must never send a legacy protective order")

    original_submitter = ForbiddenSubmitter()
    protection.submitter = original_submitter
    await refresh(SimpleNamespace(protector=lambda: protection), now=NOW)
    assert protection.submitter is original_submitter
    readonly = ReadOnlyStore(database.path)
    try:
        reasons = operational_objections(readonly, account_id=ACCOUNT_ID, now=NOW, position_count=1)
    finally:
        readonly.close()
    assert "PAPER_PROTECTION_SNAPSHOT_NOT_CURRENT" not in reasons
    assert ("PAPER_POSITION_PROTECTION_UNVERIFIED" not in reasons) is healthy
    assert "PAPER_KILL_SWITCH_ENGAGED_OR_UNAVAILABLE" in reasons


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["account", "positions"])
async def test_failed_real_protection_probe_never_manufactures_fresh_healthy_state(
    database, operation,
):
    from quantbot.brokers import BrokerRequestError
    from quantbot.learner.exploration_execution import refresh_entry_protection
    positions = (position("SPY"),)
    seed_local(database, positions)
    failure = BrokerRequestError("private provider diagnostic", status_code=503)
    broker = FakeBroker(account(), positions,
                        account_error=failure if operation == "account" else None,
                        position_error=failure if operation == "positions" else None)
    protection = supervisor(database, broker)
    await refresh_entry_protection(SimpleNamespace(protector=lambda: protection), now=NOW)
    readonly = ReadOnlyStore(database.path)
    try:
        reasons = operational_objections(readonly, account_id=ACCOUNT_ID, now=NOW, position_count=1)
    finally:
        readonly.close()
    assert "PAPER_POSITION_PROTECTION_UNVERIFIED" in reasons
    assert "PAPER_KILL_SWITCH_ENGAGED_OR_UNAVAILABLE" in reasons
