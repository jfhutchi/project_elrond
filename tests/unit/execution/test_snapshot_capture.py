"""Deterministic source-only proposal acceptance. No real provider or runtime writes."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.domain import Account, Position, ReconciliationStatus
from quantbot.execution.reconciliation import (
    ReconciliationSnapshot,
    compare_reconciliation,
)
from quantbot.execution.snapshot_capture import (
    capture_account_positions,
    validate_capture_freshness,
)

NOW = datetime(2026, 10, 2, 15, 51, 37, tzinfo=UTC)


def account(equity="2000.00", cash="1000", identity="synthetic-paper"):
    return BrokerAccountSnapshot(
        account=Account(
            account_id=identity,
            cash=Decimal(cash),
            buying_power=Decimal("2000"),
            equity=Decimal(equity),
            currency="USD",
        ),
        account_number="synthetic-number",
        status="ACTIVE",
        order_entry_allowed=True,
        restrictions=(),
    )


def positions(value="1000.00", quantity="1"):
    return (
        Position(
            symbol="SYNTH",
            quantity=Decimal(quantity),
            market_price=Decimal(value) / Decimal(quantity),
            market_value=Decimal(value),
            average_entry_price=Decimal("900"),
        ),
    )


def expected():
    return ReconciliationSnapshot(account=account().account, positions=positions())


class Broker:
    def __init__(self, accounts, marks):
        self.accounts = iter(accounts)
        self.marks = iter(marks)
        self.calls = []

    async def get_account(self):
        self.calls.append("account")
        return next(self.accounts)

    async def get_positions(self):
        self.calls.append("positions")
        return next(self.marks)


async def capture(broker, **kwargs):
    return await capture_account_positions(
        broker, clock=lambda: NOW, clock_healthy=lambda at: True, **kwargs
    )


async def test_clock_losing_timezone_is_refusal_not_exception():
    clock = iter([NOW, NOW.replace(tzinfo=None)])
    broker = Broker([account()], [])
    result = await capture_account_positions(
        broker, clock=lambda: next(clock), clock_healthy=lambda at: at.tzinfo is not None
    )
    assert result.status == "CLOCK_UNVERIFIED" and result.snapshot is None
    assert len(result.attempts) == 1


@pytest.mark.asyncio
async def test_fixed_snapshot_uses_three_reads_and_strict_comparison():
    broker = Broker([account(), account()], [positions()])
    r = await capture(broker)
    assert r.status == "ACQUIRED" and r.snapshot == expected()
    assert broker.calls == ["account", "positions", "account"]
    assert compare_reconciliation(expected(), r.snapshot).status is ReconciliationStatus.RECONCILED
    assert r.provider_as_of_verified is False


@pytest.mark.asyncio
async def test_reproduced_mixed_sample_is_rejected_before_retrying_fresh_capture():
    mixed = ReconciliationSnapshot(account=account("2000.01").account, positions=positions())
    assert (
        compare_reconciliation(expected(), mixed).status
        is ReconciliationStatus.RECONCILIATION_REQUIRED
    )
    broker = Broker(
        [account("2000.01"), account(), account(), account()],
        [positions(), positions()],
    )
    r = await capture(broker)
    assert r.status == "ACQUIRED" and len(r.attempts) == 2
    assert r.attempts[0].reason == "ACCOUNT_CHANGED_DURING_CAPTURE"
    assert r.attempts[0].before.account.equity == Decimal("2000.01")
    assert r.attempts[0].after.account.equity == Decimal("2000.00")
    assert compare_reconciliation(expected(), r.snapshot).status is ReconciliationStatus.RECONCILED
    assert len(broker.calls) == 6


@pytest.mark.asyncio
async def test_coherent_price_change_retains_existing_valuation_explanation():
    r = await capture(Broker([account("2000.01"), account("2000.01")], [positions("1000.01")]))
    c = compare_reconciliation(expected(), r.snapshot)
    assert c.status is ReconciliationStatus.RECONCILED and len(c.valuation_changes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["cash", "quantity", "identity", "persistent_equity"])
async def test_stable_genuine_differences_never_retry_or_rebase(change):
    a = account()
    p = positions()
    if change == "cash":
        a = account("2001.00", "1001")
    elif change == "quantity":
        p = positions(quantity="2")
    elif change == "identity":
        a = account(identity="foreign-synthetic-paper")
    else:
        a = account("2000.01")
        p = positions("1000.004195")
    broker = Broker([a, a], [p])
    r = await capture(broker)
    assert r.status == "ACQUIRED" and len(r.attempts) == 1
    baseline = (
        expected()
        if change != "persistent_equity"
        else ReconciliationSnapshot(account=account().account, positions=p)
    )
    comparison = compare_reconciliation(baseline, r.snapshot)
    assert comparison.status is (
        ReconciliationStatus.ACCOUNT_MISMATCH
        if change == "identity"
        else ReconciliationStatus.RECONCILIATION_REQUIRED
    )
    assert comparison.diffs and len(broker.calls) == 3


@pytest.mark.asyncio
async def test_two_unstable_samples_stop_and_preserve_both_attempts():
    broker = Broker(
        [account(), account("2000.01"), account(), account("2000.02")],
        [positions(), positions()],
    )
    r = await capture(broker)
    assert r.status == "ACQUISITION_UNSTABLE" and r.snapshot is None
    assert len(r.attempts) == 2 and len(broker.calls) == 6


@pytest.mark.asyncio
async def test_internally_inconsistent_samples_are_not_adopted():
    a = account("2000.02")
    broker = Broker([a, a, a, a], [positions(), positions()])
    r = await capture(broker)
    assert r.status == "ACQUISITION_UNSTABLE" and r.snapshot is None
    assert all(a.reason == "INCONSISTENT_EQUITY_MARKS" for a in r.attempts)


@pytest.mark.asyncio
async def test_timeout_has_no_partial_snapshot_or_retry():
    class Slow:
        async def get_account(self):
            await asyncio.Event().wait()

    r = await capture(Slow(), total_timeout_seconds=0.02)
    assert r.status == "ACQUISITION_TIMEOUT" and r.snapshot is None and len(r.attempts) == 1
    assert r.transient_failure is True


@pytest.mark.asyncio
async def test_partial_provider_failure_has_no_snapshot_and_retains_read_stage():
    class Broken:
        async def get_account(self):
            return account()

        async def get_positions(self):
            raise RuntimeError("synthetic sensitive text must not escape")

    r = await capture(Broken())
    assert r.status == "ACQUISITION_FAILED" and r.snapshot is None
    assert r.attempts[0].reason == "POSITION_READ_FAILED"
    assert "sensitive" not in repr(r)
    assert r.transient_failure is False


@pytest.mark.asyncio
async def test_timeout_after_an_unstable_sample_never_becomes_retryable():
    class UnstableThenTimeout(Broker):
        async def get_positions(self):
            if self.calls.count("positions"):
                raise TimeoutError()
            return await super().get_positions()

    r = await capture(
        UnstableThenTimeout([account(), account("2000.01"), account()], [positions()])
    )
    assert r.status == "ACQUISITION_FAILED" and r.transient_failure is False
    assert r.attempts[0].reason == "ACCOUNT_CHANGED_DURING_CAPTURE"


@pytest.mark.asyncio
async def test_duplicate_position_payload_is_partial_failure_not_adoption():
    p = positions()
    r = await capture(Broker([account(), account()], [p + p]))
    assert r.status == "ACQUISITION_FAILED" and r.snapshot is None
    assert r.attempts[0].reason == "MALFORMED_SNAPSHOT"


@pytest.mark.asyncio
async def test_last_read_failure_cannot_return_first_account_as_complete():
    class LastFails:
        def __init__(self):
            self.calls = 0

        async def get_account(self):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("synthetic provider failure")
            return account()

        async def get_positions(self):
            return positions()

    r = await capture(LastFails())
    assert r.status == "ACQUISITION_FAILED" and r.snapshot is None
    assert r.attempts[0].before is not None and r.attempts[0].after is None


@pytest.mark.asyncio
async def test_unstamped_provider_values_never_claim_quote_age_verified():
    # These real domain types have no provider-as-of timestamp. A fresh GET receipt
    # cannot prove the age of an unchanged price; downstream market-data gates remain.
    r = await capture(Broker([account(), account()], [positions()]))
    assert r.provider_as_of_verified is False
    assert not hasattr(r.snapshot.positions[0], "provider_as_of")


@pytest.mark.asyncio
async def test_stale_clock_permits_no_provider_reads():
    broker = Broker([], [])
    r = await capture_account_positions(broker, clock=lambda: NOW, clock_healthy=lambda at: False)
    assert r.status == "CLOCK_UNVERIFIED" and broker.calls == []


@pytest.mark.asyncio
async def test_capture_becomes_stale_without_acquiring_another_snapshot():
    r = await capture(Broker([account(), account()], [positions()]))
    assert (
        validate_capture_freshness(r, now=NOW + timedelta(seconds=121), clock_healthy=True)
        == "STALE_CAPTURE"
    )
    assert (
        validate_capture_freshness(r, now=NOW - timedelta(seconds=1), clock_healthy=True)
        == "CLOCK_REVERSED"
    )
    assert validate_capture_freshness(r, now=NOW, clock_healthy=False) == "CLOCK_UNVERIFIED"


@pytest.mark.asyncio
async def test_response_completion_is_actual_last_read_time():
    clock = [NOW]

    class Timed(Broker):
        async def get_account(self):
            clock[0] += timedelta(seconds=0.1)
            return await super().get_account()

        async def get_positions(self):
            clock[0] += timedelta(seconds=0.1)
            return await super().get_positions()

    r = await capture_account_positions(
        Timed([account(), account()], [positions()]),
        clock=lambda: clock[0],
        clock_healthy=lambda at: True,
    )
    assert r.received_at == NOW + timedelta(seconds=0.3)
    assert r.attempts[0].started_at == NOW and r.attempts[0].completed_at == r.received_at
    assert r.attempts[0].response_times == tuple(
        NOW + timedelta(seconds=s) for s in (0.1, 0.2, 0.3)
    )
    assert r.attempts[0].positions == positions()


@pytest.mark.asyncio
async def test_repeated_capture_has_no_duplicate_or_storage_effects(tmp_path):
    broker = Broker([account()] * 4, [positions()] * 2)
    a = await capture(broker)
    b = await capture(broker)
    assert a.snapshot == b.snapshot and len(list(tmp_path.iterdir())) == 0
    assert len(broker.calls) == 6
    # The seam exposes only GET methods: no submit, cancel, adoption, promotion or persistence.


@pytest.mark.asyncio
async def test_clock_regression_during_capture_refuses():
    times = iter([NOW, NOW, NOW - timedelta(seconds=1)])
    r = await capture_account_positions(
        Broker([account(), account()], [positions()]),
        clock=lambda: next(times),
        clock_healthy=lambda at: True,
    )
    assert r.status == "CLOCK_REVERSED" and r.snapshot is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 3},
        {"total_timeout_seconds": 6},
        {"total_timeout_seconds": float("nan")},
    ],
)
async def test_capture_cannot_expand_approved_bounds(kwargs):
    with pytest.raises(ValueError):
        await capture(Broker([], []), **kwargs)
