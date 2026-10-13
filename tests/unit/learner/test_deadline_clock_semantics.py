"""Exercise the real clock guard with immediate synthetic broker reads only.

These coroutines contain no pending I/O. Driving them directly avoids requiring a
network socket just to create Windows asyncio's loop; no host or broker is queried.
This does not substitute for asynchronous integration acceptance.
"""

from datetime import timedelta

import pytest

from quantbot.domain import MarketClock
from quantbot.storage import StorageRepository
from tests.unit.learner.test_paper import NOW
from tests.unit.learner.test_paper import setup as paper_setup
from tests.unit.learner.test_paper_deadlines import prepare


@pytest.mark.parametrize(
    ("provider_lead_us", "response_delay_us", "expected"),
    [
        (174850, 0, ("DEADLINE_BROKER_CLOCK_STALE",)),
        (1, 0, ("DEADLINE_BROKER_CLOCK_STALE",)),
        (0, 0, ()),
        (174850, 200000, ()),
    ],
)
def test_actual_deadline_guard_uses_post_response_time_without_future_grace(
    tmp_path, provider_lead_us, response_delay_us, expected
):
    fixture = paper_setup.__wrapped__(tmp_path)
    context = next(fixture)
    database, _, _ = context
    _, broker, supervisor = prepare(context)
    with database.transaction() as session:
        stop = StorageRepository(session).get_kill_switch_state()

    async def synthetic_clock():
        timestamp = broker.now + timedelta(microseconds=provider_lead_us)
        broker.now += timedelta(microseconds=response_delay_us)
        return MarketClock(
            timestamp=timestamp,
            is_open=True,
            next_open=NOW + timedelta(days=1),
            next_close=NOW.replace(hour=20),
        )

    broker.get_market_clock = synthetic_clock
    operation = supervisor.run_once()
    try:
        with pytest.raises(StopIteration) as complete:
            operation.send(None)
        result = complete.value.value
        assert result.reasons == expected
        assert result.healthy is (not expected)
        assert broker.submitted == [] and broker.cancelled == []
        with database.transaction() as session:
            assert StorageRepository(session).get_kill_switch_state() == stop
    finally:
        operation.close()
        fixture.close()
