"""Real entry/deadline paths with immediate synthetic providers, without sockets."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from quantbot.execution import OrderSubmissionBlocked
from quantbot.operations.paper_clock import (
    ClockCheck,
    ClockSample,
    PaperClockProbe,
    SynchronizationHealth,
    assess_paper_clock,
)
from quantbot.storage import StorageRepository
from tests.unit.learner.test_paper import setup as paper_fixture
from tests.unit.learner.test_paper_deadlines import fill_entry, prepare
from tests.unit.learner.test_paper_runner import context as runner_fixture


def immediate(operation):
    try:
        with pytest.raises(StopIteration) as complete:
            operation.send(None)
        return complete.value.value
    finally:
        operation.close()


class SyntheticProbe:
    monotonic=staticmethod(lambda:101.0)

    def __init__(self,broker,reason=None):
        self.broker,self.reason=broker,reason

    async def verify(self):
        if self.reason:
            return ClockCheck(None,(self.reason,))
        now=self.broker.now
        from quantbot.domain import MarketClock

        samples = []
        for wall, mono in (
            (now - timedelta(seconds=0.3), 100.0),
            (now - timedelta(seconds=0.1), 100.2),
        ):
            after = wall + timedelta(seconds=0.1)
            samples.append(
                ClockSample(
                    wall,
                    after,
                    mono,
                    mono + 0.1,
                    MarketClock(
                        timestamp=after + timedelta(seconds=0.17485),
                        is_open=self.broker.open,
                        next_open=now + timedelta(days=1),
                        next_close=now.replace(hour=20),
                    ),
                )
            )
        health = SynchronizationHealth(
            now - timedelta(seconds=0.4),
            now - timedelta(minutes=1),
            0,
            5,
            0,
            1024,
            "time.windows.com,0x9",
        )
        return assess_paper_clock(
            tuple(samples), health, now=now, monotonic_now=101.0, started_monotonic=100.0
        )


@pytest.fixture
def context(tmp_path):
    fixture=runner_fixture.__wrapped__(tmp_path,SimpleNamespace(param='relative-clock'))
    database,broker,runner,supervisor=next(fixture)
    probe=SyntheticProbe(broker)
    runner.clock_probe=probe
    supervisor.clock_probe=probe
    yield database,broker,runner,supervisor
    fixture.close()


def test_both_paths_accept_verified_small_lead_without_global_clear_or_broker_order(context):
    database,broker,runner,supervisor=context
    with database.transaction() as session:
        original=StorageRepository(session).get_kill_switch_state()
    deadline=immediate(supervisor.run_once())
    assert deadline.healthy,deadline.reasons
    result=immediate(runner.evaluate())
    assert len(result.submissions)==1,runner.last_reasons
    request=result.submissions[0]
    assert request.risk_approved and request.vetoes.allowed
    assert broker.submitted==[] and broker.cancelled==[]
    with database.transaction() as session:
        repo = StorageRepository(session)
        assert repo.get_kill_switch_state() == original
        clock_records = [
            r for r in repo.list_signals() if r.payload.get("kind") == "PAPER_CLOCK_EVIDENCE_V1"
        ]
        assert clock_records and clock_records[-1].payload["document"]["offset_lower_seconds"] > 0
    with pytest.raises(OrderSubmissionBlocked) as blocked:
        immediate(supervisor.coordinator.submit(request.intent,purpose=request.purpose,
            market_eligible=request.market_eligible,drawdown=request.drawdown,
            risk_approved=request.risk_approved,vetoes=request.vetoes))
    assert 'KILL_SWITCH_ENGAGED' in blocked.value.decision.reasons
    assert broker.submitted==[]


def test_unverified_sync_refuses_entry_and_deadline(context):
    _,broker,runner,supervisor=context
    assert immediate(supervisor.run_once()).healthy
    failed=SyntheticProbe(broker,'PAPER_CLOCK_SYNC_UNVERIFIED')
    runner.clock_probe=failed
    assert immediate(runner.evaluate()).submissions==()
    assert runner.last_reasons==('PAPER_CLOCK_SYNC_UNVERIFIED',)
    supervisor.clock_probe=failed
    broker.now+=timedelta(seconds=10)
    assert immediate(supervisor.run_once()).reasons==('PAPER_CLOCK_SYNC_UNVERIFIED',)
    assert broker.submitted==[]


def test_old_clock_contract_heartbeat_cannot_authorize_relative_entry(context):
    _,broker,runner,supervisor=context
    supervisor.clock_contract='STRICT_V0'
    assert immediate(supervisor.run_once()).healthy
    assert immediate(runner.evaluate()).submissions==()
    assert runner.last_reasons==('DEADLINE_CLOCK_CONTRACT_MISMATCH',)
    assert broker.submitted==[]


def test_future_price_features_remain_refused(context):
    _,broker,runner,supervisor=context
    assert immediate(supervisor.run_once()).healthy
    runner.predictions=tuple(p.model_copy(update={'features':p.features.model_copy(
        update={'feature_at':broker.now+timedelta(microseconds=1)})}) for p in runner.predictions)
    assert immediate(runner.evaluate()).submissions==()
    assert 'PAPER_PREDICTION_STALE' in runner.last_reasons
    assert broker.submitted==[]


def test_live_broker_never_enters_relative_clock_acquisition(context):
    _,broker,runner,supervisor=context
    assert immediate(supervisor.run_once()).healthy
    broker.environment='LIVE'
    runner.clock_probe=PaperClockProbe(broker,clock=lambda:broker.now)
    assert immediate(runner.evaluate()).submissions==()
    assert runner.last_reasons==('PAPER_CLOCK_MODE_REQUIRED',)
    assert broker.submitted==[]


def test_conservative_owned_exit_uses_actual_local_event_time(tmp_path):
    fixture=paper_fixture.__wrapped__(tmp_path)
    setup=next(fixture)
    record,broker,supervisor=prepare(setup)
    fill_entry(setup,record,broker)
    supervisor.clock_contract='RELATIVE_V1'
    supervisor.clock_probe=SyntheticProbe(broker)
    broker.now=record.deadline_at-timedelta(seconds=.15)
    local_now=broker.now
    try:
        result=immediate(supervisor.run_once())
        assert result.healthy,result.reasons
        assert result.exits_submitted==1 and len(broker.submitted)==1
        assert broker.submitted[0].created_at==local_now
        assert broker.submitted[0].side.value=='SELL'
    finally:
        fixture.close()
