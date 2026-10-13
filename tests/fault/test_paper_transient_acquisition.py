"""Actual recovery, capture, writer and storage; no external broker or orders."""

from datetime import timedelta
from pathlib import Path
from typing import cast

import pytest
from tests.fault.test_reconciliation_halt import (
    RecoveryBroker,
    _account,
    _prepare_database,
    _settings,
)
from tests.integration.test_run_once import (
    NOW,
    FakeDataSync,
    FakeStrategy,
    FakeSubmitter,
    _identity,
)

from quantbot.brokers import BrokerAdapter
from quantbot.brokers.base import BrokerProtocolError, BrokerRequestError, BrokerTransportError
from quantbot.domain import BrokerEnvironment, IntentState, TradingMode
from quantbot.execution import StartupRecovery
from quantbot.operations.cycle import RunOnceCycle
from quantbot.operations.deployment import RuntimeContext, RuntimeRole
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.metrics import OperationalMetrics
from quantbot.operations.runners import LedgerSyncingRecovery
from quantbot.storage import Database, StorageRepository


@pytest.mark.asyncio
async def test_stale_unsubmitted_intent_is_not_replayed_after_pause_and_store_reopen(
    tmp_path: Path,
):
    from tests.integration.test_run_once import _intent

    from quantbot.operations.cycle import CycleStrategyResult

    stale = _intent().model_copy(
        update={
            "intent_id": "stale-unsubmitted",
            "client_order_id": "stale-client",
            "signal_date": NOW.date() - timedelta(days=1),
            "created_at": NOW - timedelta(days=1),
        }
    )
    database = _prepare_database(tmp_path, _account())
    with database.transaction() as session:
        StorageRepository(session).create_order_intent(stale)
    broker = ReadFailureBroker(TimeoutError())
    calls = []
    refused = await cycle(database, tmp_path, broker, calls).run(
        "stale-paused", started_at=NOW, trading_date=NOW.date()
    )
    assert refused.status == "BLOCKED" and refused.orders_submitted == 0
    database.close()
    database = Database(tmp_path / "quantbot.db")
    broker.error = None

    class FreshNoOrders:
        async def evaluate(self):
            calls.append("fresh-strategy")
            return CycleStrategyResult(submissions=(), signal_count=0)

    runner = cycle(database, tmp_path, broker, calls)
    runner._strategy = FreshNoOrders()
    fresh = await runner.run("stale-reopened", started_at=NOW, trading_date=NOW.date())
    assert fresh.status == "SUCCEEDED" and fresh.orders_submitted == 0
    assert calls == ["sync", "fresh-strategy"] and broker.mutations == []
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert repo.get_order_intent(stale.intent_id) == stale
        assert not repo.list_broker_orders()
    database.close()


class ReadFailureBroker(RecoveryBroker):
    def __init__(self, error):
        super().__init__(account=_account())
        self.error = error
        self.reads = 0
        self.manual_stop = None

    async def get_positions(self):
        self.reads += 1
        if self.manual_stop:
            self.manual_stop()
        if self.error:
            raise self.error
        return await super().get_positions()


def cycle(database, tmp_path, broker, calls, *, mode="PAPER", settings=None):
    recovery = StartupRecovery(
        database, cast(BrokerAdapter, broker), settings or _settings(), clock=lambda: NOW
    )
    return RunOnceCycle(
        database=database,
        lock_path=tmp_path / "writer.lock",
        strategy_identity=_identity(),
        recovery=LedgerSyncingRecovery(
            database, cast(BrokerAdapter, broker), recovery, clock=lambda: NOW
        ),
        data_sync=FakeDataSync(calls),
        strategy=FakeStrategy(calls),
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
        runtime=RuntimeContext(
            role=RuntimeRole.COORDINATOR,
            store_identity=database.store_identity,
            trading_mode=mode,
            process_started_at=NOW,
            git_commit="synthetic",
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError(),
        BrokerRequestError("synthetic", status_code=429),
        BrokerRequestError("synthetic", status_code=503),
    ],
)
async def test_identified_transient_pauses_then_restarts_from_fresh_gates(tmp_path: Path, error):
    database = _prepare_database(tmp_path, _account())
    broker = ReadFailureBroker(error)
    calls = []
    before = KillSwitchController(database).get()
    result = await cycle(database, tmp_path, broker, calls).run(
        "transient", started_at=NOW, trading_date=NOW.date()
    )
    assert result.status == "BLOCKED" and result.orders_submitted == 0
    assert calls == [] and KillSwitchController(database).get() == before
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert repo.get_run("transient").detail["retryable"] is True
        assert not repo.list_incidents(unresolved_only=True)
        assert repo.list_broker_orders() == []
    broker.error = None
    database.close()
    database = Database(tmp_path / "quantbot.db")
    fresh = await cycle(database, tmp_path, broker, calls).run(
        "fresh", started_at=NOW, trading_date=NOW.date()
    )
    assert fresh.status == "SUCCEEDED" and calls == ["sync", "strategy", "submit"]
    assert broker.reads >= 3 and broker.mutations == []
    assert KillSwitchController(database).get() == before
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", ["position", "malformed"])
async def test_final_read_timeout_does_not_mask_partial_position_fault(tmp_path: Path, partial):
    from tests.unit.execution.test_snapshot_capture import positions

    class LastReadFails(RecoveryBroker):
        def __init__(self):
            marks = positions()
            super().__init__(
                account=_account(), positions=marks if partial == "position" else marks + marks
            )
            self.account_reads = 0

        async def get_account(self):
            self.account_reads += 1
            if self.account_reads == 2:
                raise TimeoutError()
            return await super().get_account()

    database = _prepare_database(tmp_path, _account())
    result = await cycle(database, tmp_path, LastReadFails(), []).run(
        "partial-hard", started_at=NOW, trading_date=NOW.date()
    )
    assert result.status == "HALTED" and KillSwitchController(database).get().engaged
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "http503", "http429"])
async def test_real_http_adapter_health_and_capture_transient_recover_without_replay(
    tmp_path: Path, failure
):
    import httpx
    from pydantic import SecretStr

    from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
    from quantbot.brokers.transports import HttpxBrokerTransport

    database = _prepare_database(tmp_path, _account())
    failed = [True]
    methods = []

    async def handler(request):
        methods.append(request.method)
        if failed[0]:
            if failure == "timeout":
                raise httpx.ReadTimeout("synthetic", request=request)
            return httpx.Response(int(failure[4:]), json={})
        if request.url.path == "/v2/account":
            return httpx.Response(
                200,
                json={
                    "id": "paper-1",
                    "account_number": "synthetic",
                    "status": "ACTIVE",
                    "cash": "10000",
                    "buying_power": "20000",
                    "equity": "10000",
                    "currency": "USD",
                    "trading_blocked": False,
                    "transfers_blocked": False,
                    "account_blocked": False,
                    "trade_suspended_by_user": False,
                },
            )
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        broker = AlpacaPaperBrokerAdapter(
            api_key=SecretStr("synthetic"),
            api_secret=SecretStr("synthetic"),
            transport=HttpxBrokerTransport(client),
            max_retries=0,
        )
        calls = []
        result = await cycle(database, tmp_path, broker, calls).run(
            "http-refused", started_at=NOW, trading_date=NOW.date()
        )
        assert result.status == "BLOCKED" and calls == []
        assert not KillSwitchController(database).get().engaged
        failed[0] = False
        result = await cycle(database, tmp_path, broker, calls).run(
            "http-fresh", started_at=NOW, trading_date=NOW.date()
        )
        assert result.status == "SUCCEEDED" and calls == ["sync", "strategy", "submit"]
        assert methods and set(methods) == {"GET"}
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["operator manual stop", "RECONCILIATION_FAILED"])
async def test_transient_does_not_change_existing_manual_or_legacy_latch(tmp_path: Path, reason):
    database = _prepare_database(tmp_path, _account())
    before = KillSwitchController(database).engage(reason=reason, updated_at=NOW)
    broker = ReadFailureBroker(TimeoutError())
    calls = []
    await cycle(database, tmp_path, broker, calls).run(
        "latched-timeout", started_at=NOW, trading_date=NOW.date()
    )
    assert KillSwitchController(database).get() == before and calls == []
    broker.error = None
    result = await cycle(database, tmp_path, broker, calls).run(
        "still-latched", started_at=NOW, trading_date=NOW.date()
    )
    assert (
        result.status == "HALTED" and KillSwitchController(database).get() == before and calls == []
    )
    database.close()


@pytest.mark.asyncio
async def test_manual_stop_race_during_preparation_is_retained(tmp_path: Path):
    database = _prepare_database(tmp_path, _account())
    broker = ReadFailureBroker(TimeoutError())
    broker.manual_stop = lambda: KillSwitchController(database).engage(
        reason="operator race", updated_at=NOW + timedelta(seconds=1)
    )
    result = await cycle(database, tmp_path, broker, []).run(
        "race", started_at=NOW, trading_date=NOW.date()
    )
    assert result.orders_submitted == 0
    assert KillSwitchController(database).get().reason == "operator race"
    assert KillSwitchController(database).get().updated_at == NOW + timedelta(seconds=1)
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        BrokerTransportError("unknown"),
        BrokerProtocolError("malformed"),
        BrokerRequestError("denied", status_code=403),
        ValueError("unknown"),
    ],
)
async def test_unknown_malformed_or_nonretryable_errors_still_latch(tmp_path: Path, error):
    database = _prepare_database(tmp_path, _account())
    broker = ReadFailureBroker(error)
    result = await cycle(database, tmp_path, broker, []).run(
        "hard", started_at=NOW, trading_date=NOW.date()
    )
    assert result.status == "HALTED" and KillSwitchController(database).get().engaged
    database.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["cash", "identity", "risk", "unresolved", "health", "live"])
async def test_timeout_cannot_mask_known_hard_faults(tmp_path: Path, fault):
    database = _prepare_database(tmp_path, _account())
    broker = ReadFailureBroker(TimeoutError())
    settings = _settings()
    mode = "PAPER"
    if fault == "cash":
        broker.account = _account(cash="9999")
    elif fault == "identity":
        broker.account = _account(account_id="other")
    elif fault == "risk":
        settings = settings.model_copy(update={"RISK_ENGINE_HEALTHY": False})
    elif fault == "health":
        broker.healthy = False
    elif fault == "live":
        mode = "LIVE"
        settings = settings.model_copy(
            update={"TRADING_MODE": TradingMode.LIVE, "BROKER_ENVIRONMENT": BrokerEnvironment.LIVE}
        )
    else:
        from tests.integration.test_run_once import _intent

        with database.transaction() as session:
            repo = StorageRepository(session)
            repo.create_order_intent(_intent())
            repo.transition_order_intent(
                _intent().intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            repo.transition_order_intent(
                _intent().intent_id,
                IntentState.SUBMITTING,
                expected_current=IntentState.ORDER_CREATED,
            )
    calls = []
    result = await cycle(database, tmp_path, broker, calls, mode=mode, settings=settings).run(
        "mixed", started_at=NOW, trading_date=NOW.date()
    )
    assert (
        result.status == "HALTED" and KillSwitchController(database).get().engaged and calls == []
    )
    database.close()
