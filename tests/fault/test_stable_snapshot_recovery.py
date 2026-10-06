"""Production recovery seams; synthetic provider data, real strict comparison/store."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import cast

import pytest
from tests.fault.test_reconciliation_halt import RecoveryBroker, _prepare_database, _settings

from quantbot.brokers import BrokerAccountSnapshot, BrokerAdapter
from quantbot.brokers.base import BrokerRequestError
from quantbot.domain import Account, Position, ReconciliationStatus
from quantbot.execution import StartupRecovery
from quantbot.operations.runners import LedgerSyncingRecovery
from quantbot.storage import AlreadyLockedError, SingleWriterLock, StorageRepository

NOW = datetime(2026, 10, 2, 15, 51, 37, tzinfo=UTC)


def account(equity: str = "2000.00") -> Account:
    return Account(
        account_id="paper-1",
        cash=Decimal("1000"),
        buying_power=Decimal("2000"),
        equity=Decimal(equity),
        currency="USD",
    )


def positions() -> tuple[Position, ...]:
    return (
        Position(
            symbol="SYNTH",
            quantity=Decimal("1"),
            market_price=Decimal("1000"),
            market_value=Decimal("1000"),
            average_entry_price=Decimal("900"),
        ),
    )


class SequencedBroker(RecoveryBroker):
    def __init__(self, values: list[str]) -> None:
        super().__init__(account=account(), positions=positions())
        self.values = iter(values)
        self.account_reads = 0

    async def get_account(self) -> BrokerAccountSnapshot:
        self.account_reads += 1
        self.account = account(next(self.values, "2000.00"))
        return await super().get_account()


async def test_mixed_capture_retries_before_strict_comparison_and_keeps_attempt(tmp_path):
    database = _prepare_database(tmp_path, account(), positions())
    broker = SequencedBroker(["2000.01", "2000.00", "2000.00", "2000.00"])
    try:
        result = await StartupRecovery(database, cast(BrokerAdapter, broker), _settings()).recover(
            "stable-recovery", started_at=NOW
        )
        assert result.ready and result.reconciliation.status is ReconciliationStatus.RECONCILED
        assert broker.account_reads == 4 and broker.mutations == []
        with database.transaction() as session:
            repository = StorageRepository(session)
            assert repository.get_latest_account_snapshot("paper-1").snapshot_id == "local-1"
            record = repository.get_reconciliation("stable-recovery")
            captures = record.summary["snapshot_acquisition"]
            assert captures[0]["attempts"][0]["reason"] == "ACCOUNT_CHANGED_DURING_CAPTURE"
            assert len(captures[0]["attempts"]) == 2
    finally:
        database.close()


async def test_two_unstable_attempts_are_durable_refusal_not_reconciled(tmp_path):
    database = _prepare_database(tmp_path, account(), positions())
    broker = SequencedBroker(["2000.00", "2000.01", "2000.00", "2000.02"])
    try:
        result = await StartupRecovery(database, cast(BrokerAdapter, broker), _settings()).recover(
            "unstable-recovery", started_at=NOW
        )
        assert not result.ready
        assert "SNAPSHOT_ACQUISITION_UNSTABLE" in result.reasons
        assert not result.reconciliation.allows_new_orders and broker.mutations == []
        with database.transaction() as session:
            record = StorageRepository(session).get_reconciliation("unstable-recovery")
            assert len(record.summary["snapshot_acquisition"][0]["attempts"]) == 2
    finally:
        database.close()


@pytest.mark.parametrize("change", ["cash", "quantity", "identity", "equity"])
async def test_stable_genuine_difference_is_not_rebased_by_capture(tmp_path, change):
    local_account = account()
    local_positions = positions()
    actual_account = local_account
    actual_positions = local_positions
    if change == "cash":
        actual_account = actual_account.model_copy(
            update={"cash": Decimal("1001"), "equity": Decimal("2001")}
        )
    elif change == "quantity":
        actual_positions = (
            local_positions[0].model_copy(
                update={"quantity": Decimal("2"), "market_price": Decimal("500")}
            ),
        )
    elif change == "identity":
        actual_account = actual_account.model_copy(update={"account_id": "foreign-synthetic"})
    else:
        actual_positions = (
            local_positions[0].model_copy(
                update={
                    "market_price": Decimal("1000.004195"),
                    "market_value": Decimal("1000.004195"),
                }
            ),
        )
        local_positions = actual_positions
        actual_account = actual_account.model_copy(update={"equity": Decimal("2000.01")})
    database = _prepare_database(tmp_path, local_account, local_positions)
    broker = RecoveryBroker(account=actual_account, positions=actual_positions)
    try:
        result = await StartupRecovery(database, cast(BrokerAdapter, broker), _settings()).recover(
            "genuine", started_at=NOW
        )
        assert not result.ready and not result.reconciliation.allows_new_orders
        assert any(
            d.key
            == (
                "account_id"
                if change == "identity"
                else "equity"
                if change == "equity"
                else "SYNTH"
                if change == "quantity"
                else "cash"
            )
            for d in result.reconciliation.diffs
        )
        with database.transaction() as session:
            original = StorageRepository(session).get_latest_account_snapshot("paper-1")
            assert original.snapshot_id == "local-1" and original.account == local_account
        assert broker.mutations == []
    finally:
        database.close()


async def test_ledger_ingestion_does_not_adopt_unstable_sample(tmp_path):
    database = _prepare_database(tmp_path, account(), positions())
    broker = SequencedBroker(["2000.00", "2000.01", "2000.00", "2000.02"])
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    try:
        captures = []
        reasons = await recovery._ingest(captured_at=NOW, run_id=None, captures=captures)
        assert reasons == ("SNAPSHOT_ACQUISITION_UNSTABLE",)
        assert len(captures) == 1 and len(captures[0].attempts) == 2
        with database.transaction() as session:
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper-1").snapshot_id
                == "local-1"
            )
    finally:
        database.close()


async def test_ingestion_stamps_accepted_response_completion(tmp_path):
    from datetime import timedelta

    clock = [NOW]
    flat_account = account().model_copy(update={"cash": Decimal("2000")})

    class Timed(RecoveryBroker):
        async def get_account(self):
            clock[0] += timedelta(seconds=0.1)
            return await super().get_account()

        async def get_positions(self):
            clock[0] += timedelta(seconds=0.1)
            return ()

    database = _prepare_database(tmp_path, flat_account)
    broker = Timed(account=flat_account)
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
        clock=lambda: clock[0],
    )
    try:
        assert await recovery._ingest(captured_at=NOW, run_id=None) == ()
        with database.transaction() as session:
            record = StorageRepository(session).get_latest_account_snapshot("paper-1")
            assert record.captured_at == NOW + timedelta(seconds=0.3)
    finally:
        database.close()


@pytest.mark.parametrize("reset_shared_budget", [False, True])
async def test_ingestion_and_comparison_share_one_capture_budget(
    tmp_path, monkeypatch, reset_shared_budget
):
    import quantbot.execution.recovery as startup
    import quantbot.operations.runners as runners

    elapsed = [1000.0]
    monkeypatch.setattr(runners, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(startup, "monotonic", lambda: elapsed[0])
    flat_account = account().model_copy(update={"cash": Decimal("2000")})

    class Immediate(RecoveryBroker):
        account_reads = 0
        position_reads = 0

        async def get_account(self):
            self.account_reads += 1
            return await super().get_account()

        async def get_positions(self):
            self.position_reads += 1
            return ()

    database = _prepare_database(tmp_path, flat_account)
    broker = Immediate(account=flat_account)
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    real_ingest = recovery._ingest
    after_ingest = []

    async def completed_ingest(**kwargs):
        reasons = await real_ingest(**kwargs)
        assert reasons == ()
        after_ingest.append((broker.account_reads, broker.position_reads))
        # Simulate expensive ledger work AFTER the genuine successful capture.
        # Keep asyncio's real clock and the production capture budget untouched.
        elapsed[0] += runners.CAPTURE_BUDGET_SECONDS + 0.01
        return reasons

    monkeypatch.setattr(recovery, "_ingest", completed_ingest)
    if reset_shared_budget:
        real_recover = recovery._recovery.recover

        async def dropped_deadline(*args, **kwargs):
            # Negative control: reproduce resetting the budget at the comparison.
            kwargs.pop("_capture_deadline", None)
            return await real_recover(*args, **kwargs)

        monkeypatch.setattr(recovery._recovery, "recover", dropped_deadline)
    try:
        result = await recovery.recover("shared-budget", started_at=NOW)
        with database.transaction() as session:
            captures = (
                StorageRepository(session)
                .get_reconciliation("shared-budget")
                .summary["snapshot_acquisition"]
            )

        def shared_budget_assertions():
            assert not result.ready and "SNAPSHOT_ACQUISITION_TIMEOUT" in result.reasons
            assert len(captures) == 2 and captures[0]["status"] == "ACQUIRED"
            assert captures[1]["status"] == "ACQUISITION_TIMEOUT"
            assert (broker.account_reads, broker.position_reads) == after_ingest[0] == (2, 1)

        if reset_shared_budget:
            assert result.ready
            assert (broker.account_reads, broker.position_reads) == (4, 2)
            with pytest.raises(AssertionError):
                shared_budget_assertions()
        else:
            shared_budget_assertions()
    finally:
        database.close()


async def test_slow_capture_leaves_protection_free_and_releases_writer_on_timeout(tmp_path):
    entered = asyncio.Event()

    class Hangs(RecoveryBroker):
        async def get_account(self):
            entered.set()
            await asyncio.Event().wait()

    database = _prepare_database(tmp_path, account(), positions())
    broker = Hangs(account=account(), positions=positions())
    recovery = StartupRecovery(database, cast(BrokerAdapter, broker), _settings())
    writer = tmp_path / "writer.lock"

    async def entry():
        with SingleWriterLock(writer):
            return await recovery.recover(
                "lock-timeout", started_at=NOW, _capture_deadline=monotonic() + 0.08
            )

    task = asyncio.create_task(entry())
    try:
        await entered.wait()
        # Existing protection lock is distinct and SQL is not held during the GET.
        with SingleWriterLock(tmp_path / "writer.protection.lock"):
            with database.transaction() as session:
                assert StorageRepository(session).get_latest_account_snapshot("paper-1")
        # Deadlines retain the same writer serialization; contention fails fast.
        with pytest.raises(AlreadyLockedError):
            with SingleWriterLock(writer):
                pass
        result = await task
        assert not result.ready and "SNAPSHOT_ACQUISITION_TIMEOUT" in result.reasons
        with SingleWriterLock(writer):
            pass
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        database.close()


async def test_provider_error_retains_status_without_secret_message(tmp_path):
    class Forbidden(RecoveryBroker):
        async def get_positions(self):
            raise BrokerRequestError("synthetic-sensitive-token", status_code=403)

    database = _prepare_database(tmp_path, account(), positions())
    broker = Forbidden(account=account(), positions=positions())
    try:
        result = await StartupRecovery(database, cast(BrokerAdapter, broker), _settings()).recover(
            "forbidden", started_at=NOW
        )
        assert not result.ready
        with database.transaction() as session:
            evidence = (
                StorageRepository(session)
                .get_reconciliation("forbidden")
                .summary["snapshot_acquisition"]
            )
            assert evidence[0]["attempts"][0]["http_status"] == 403
            assert "synthetic-sensitive-token" not in repr(evidence)
    finally:
        database.close()


def test_both_paper_bindings_include_recovery_source(monkeypatch):
    from pathlib import Path

    from quantbot.learner.five_minute_paper import five_minute_code_hash
    from quantbot.learner.paper_runner import one_minute_code_hash

    before = (one_minute_code_hash(), five_minute_code_hash())
    original = Path.read_bytes

    def altered(path):
        data = original(path)
        return data + b"# synthetic source mutation" if path.name == "snapshot_capture.py" else data

    monkeypatch.setattr(Path, "read_bytes", altered)
    after = (one_minute_code_hash(), five_minute_code_hash())
    assert before[0] != after[0] and before[1] != after[1]


async def test_entry_prepares_outside_deadline_writer_lock_and_halts_on_timeout(
    tmp_path, monkeypatch
):
    from tests.integration.test_run_once import FakeDataSync, FakeStrategy, FakeSubmitter, _identity

    import quantbot.operations.runners as runners
    from quantbot.operations.cycle import RunOnceCycle
    from quantbot.operations.metrics import OperationalMetrics

    monkeypatch.setattr(runners, "CAPTURE_BUDGET_SECONDS", 0.08)
    entered = asyncio.Event()

    class Hangs(RecoveryBroker):
        async def get_account(self):
            entered.set()
            await asyncio.Event().wait()

    database = _prepare_database(tmp_path, account(), positions())
    broker = Hangs(account=account(), positions=positions())
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    writer = tmp_path / "entry.lock"
    calls = []
    cycle = RunOnceCycle(
        database=database,
        lock_path=writer,
        strategy_identity=_identity(),
        recovery=recovery,
        data_sync=FakeDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )
    task = asyncio.create_task(
        cycle.run("prepared-timeout", started_at=NOW, trading_date=NOW.date())
    )
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with SingleWriterLock(writer):
            with database.transaction() as session:
                assert StorageRepository(session).get_latest_account_snapshot("paper-1")
        with SingleWriterLock(tmp_path / "protection.lock"):
            pass
        result = await asyncio.wait_for(task, 1)
        assert result.status == "HALTED" and result.orders_submitted == 0
        assert "SNAPSHOT_ACQUISITION_TIMEOUT" in result.reasons
        assert calls == [] and broker.mutations == []
        with database.transaction() as session:
            repository = StorageRepository(session)
            assert repository.get_kill_switch_state().engaged
            assert repository.get_latest_account_snapshot("paper-1").snapshot_id == "local-1"
            evidence = repository.get_reconciliation("prepared-timeout:reconciliation").summary
            assert evidence["snapshot_acquisition"][0]["status"] == "ACQUISITION_TIMEOUT"
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        database.close()


async def test_prepared_ledger_uses_two_independent_samples_and_preserves_difference(tmp_path):
    flat = account().model_copy(update={"cash": Decimal("2000")})

    class Changed(RecoveryBroker):
        reads = 0

        async def get_account(self):
            self.reads += 1
            if self.reads > 2:
                self.account = flat.model_copy(
                    update={"cash": Decimal("2001"), "equity": Decimal("2001")}
                )
            return await super().get_account()

    database = _prepare_database(tmp_path, flat)
    broker = Changed(account=flat)
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    try:
        prepared = await recovery.prepare_snapshots()
        assert len(prepared) == 2 and broker.reads == 4
        result = await recovery.recover(
            "prepared-diff", started_at=NOW, _prepared_captures=prepared
        )
        assert not result.ready and broker.reads == 4
        assert any(d.key == "cash" for d in result.reconciliation.diffs)
        assert broker.mutations == []
    finally:
        database.close()


async def test_repeated_acquisition_failures_keep_separate_durable_records_and_pending_intents(
    tmp_path,
):
    from tests.integration.test_run_once import _intent

    from quantbot.domain import IntentState

    class Forbidden(RecoveryBroker):
        async def get_positions(self):
            raise BrokerRequestError("synthetic private detail", status_code=403)

    database = _prepare_database(tmp_path, account(), positions())
    broker = Forbidden(account=account(), positions=positions())
    recovery = StartupRecovery(database, cast(BrokerAdapter, broker), _settings())
    try:
        with database.transaction() as session:
            repo = StorageRepository(session)
            intent = _intent()
            repo.create_order_intent(intent)
            repo.transition_order_intent(
                intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            repo.transition_order_intent(
                intent.intent_id, IntentState.SUBMITTING, expected_current=IntentState.ORDER_CREATED
            )
        for run in ("failed-a", "failed-b"):
            result = await recovery.recover(run, started_at=NOW)
            assert not result.ready and result.unresolved_intent_ids == (intent.intent_id,)
        with database.transaction() as session:
            repo = StorageRepository(session)
            first = repo.get_reconciliation("failed-a")
            second = repo.get_reconciliation("failed-b")
            assert first.diffs[0].diff_id != second.diffs[0].diff_id
            assert repo.get_order_intent(intent.intent_id).state is IntentState.SUBMITTING
        assert broker.mutations == []
    finally:
        database.close()


async def test_prepared_position_change_and_staleness_cannot_rebase_ledger(tmp_path):
    from dataclasses import replace
    from datetime import timedelta

    flat = account().model_copy(update={"cash": Decimal("2000")})
    database = _prepare_database(tmp_path, flat)
    broker = RecoveryBroker(account=account(), positions=positions())
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    try:
        prepared = await recovery.prepare_snapshots()
        result = await recovery.recover("ledger-moved", started_at=NOW, _prepared_captures=prepared)
        assert not result.ready and "POSITION_LEDGER_MISMATCH" in result.reasons
        stale = tuple(
            replace(c, received_at=datetime.now(UTC) - timedelta(seconds=121)) for c in prepared
        )
        result = await recovery.recover("stale-prepared", started_at=NOW, _prepared_captures=stale)
        assert not result.ready and "SNAPSHOT_STALE_CAPTURE" in result.reasons
        with database.transaction() as session:
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper-1").snapshot_id
                == "local-1"
            )
        assert broker.mutations == []
    finally:
        database.close()


async def test_failed_preparation_is_journaled_without_unrelated_ledger_reads(tmp_path):
    from quantbot.execution.snapshot_capture import Capture

    class Outage(RecoveryBroker):
        async def get_fills(self, **kwargs):
            raise AssertionError("known acquisition failure must not fetch fills")

        async def get_open_orders(self):
            raise AssertionError("known acquisition failure must not fetch orders")

    database = _prepare_database(tmp_path, account(), positions())
    broker = Outage(account=account(), positions=positions())
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    try:
        result = await recovery.recover(
            "prepared-outage",
            started_at=NOW,
            _prepared_captures=(Capture("ACQUISITION_TIMEOUT", None, None, ()),),
        )
        assert not result.ready and "SNAPSHOT_ACQUISITION_TIMEOUT" in result.reasons
        with database.transaction() as session:
            assert StorageRepository(session).get_reconciliation("prepared-outage")
    finally:
        database.close()


async def test_rejected_ingestion_capture_retains_genuine_differences(tmp_path):
    flat = account().model_copy(update={"cash": Decimal("2000")})
    database = _prepare_database(tmp_path, flat)
    broker = RecoveryBroker(account=account("2000.02"), positions=positions())
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
    )
    try:
        captures = await recovery.prepare_snapshots()
        assert len(captures) == 1 and captures[0].status == "ACQUISITION_UNSTABLE"
        result = await recovery.recover(
            "rejected-diffs", started_at=NOW, _prepared_captures=captures
        )
        assert not result.ready and any(d.key == "cash" for d in result.reconciliation.diffs)
        with database.transaction() as session:
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper-1").snapshot_id
                == "local-1"
            )
    finally:
        database.close()


@pytest.mark.parametrize("elapsed,reason", [(121, "STALE_CAPTURE"), (-1, "CLOCK_REVERSED")])
async def test_capture_rechecked_after_settled_order_reads_before_adoption(
    tmp_path, elapsed, reason
):
    from datetime import timedelta

    from quantbot.domain import BrokerOrder, OrderSide, OrderType, TimeInForce

    clock = [NOW]
    flat = account().model_copy(update={"cash": Decimal("2000")})
    working = BrokerOrder(
        broker_order_id="synthetic-old",
        client_order_id="synthetic-client",
        symbol="SYNTH",
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=Decimal("1"),
        filled_quantity=Decimal("0"),
        status="new",
        submitted_at=NOW,
    )

    class Settles(RecoveryBroker):
        async def get_order(self, order_id):
            clock[0] += timedelta(seconds=elapsed)
            return working.model_copy(update={"status": "canceled"})

    database = _prepare_database(tmp_path, flat)
    with database.transaction() as session:
        from tests.integration.test_run_once import _intent

        repo = StorageRepository(session)
        repo.create_order_intent(
            _intent().model_copy(
                update={
                    "client_order_id": working.client_order_id,
                    "symbol": working.symbol,
                    "side": working.side,
                    "created_at": NOW,
                    "signal_date": NOW.date(),
                }
            )
        )
        repo.save_broker_order(working)
    broker = Settles(account=flat)
    recovery = LedgerSyncingRecovery(
        database,
        cast(BrokerAdapter, broker),
        StartupRecovery(database, cast(BrokerAdapter, broker), _settings()),
        clock=lambda: clock[0],
    )
    try:
        assert await recovery._ingest(captured_at=NOW, run_id=None) == ("SNAPSHOT_" + reason,)
        with database.transaction() as session:
            assert (
                StorageRepository(session).get_latest_account_snapshot("paper-1").snapshot_id
                == "local-1"
            )
    finally:
        database.close()
