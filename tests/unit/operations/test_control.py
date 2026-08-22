"""The operator control surface: what a browser may do to the kill switch, and what it may not.

This is a network-reachable control over the thing that stops trading, so the tests are about
the refusals as much as the actions. Each asserts the named refusal rather than "an error
happened" -- a 403 where a 409 belongs would mean the clear was rejected for the wrong reason
and the operator would go looking in the wrong place.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from quantbot.brokers.base import BrokerHealth
from quantbot.domain import Bar, ReconciliationStatus
from quantbot.operations.control import CLEAR, ENGAGE, ControlRefused, OperatorControl
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.readiness import measure_readiness
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 21, 14, 30, tzinfo=UTC)
ACCOUNT = "paper-1"
TOKEN = "a-sufficiently-long-control-token"


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "quantbot.db")
    yield db
    db.close()


class FakeBroker:
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    async def health_check(self) -> BrokerHealth:
        return BrokerHealth(
            healthy=self._healthy,
            provider="alpaca",
            environment="paper",
            account_id=ACCOUNT,
            reasons=() if self._healthy else ("BROKER_UNAVAILABLE",),
        )


def seed(database: Database, *, reconciled_at: datetime) -> None:
    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_reconciliation(
            "reconciliation-1",
            status=ReconciliationStatus.RECONCILED,
            started_at=reconciled_at - timedelta(minutes=1),
            completed_at=reconciled_at,
            diffs=(),
        )
        repository.save_bars(
            [
                Bar(
                    symbol="SPY",
                    timestamp=NOW - timedelta(hours=20),
                    open="100",
                    high="110",
                    low="95",
                    close="105",
                    volume="1000",
                    adjustment="1",
                )
            ],
            provider="alpaca",
        )


def control(database: Database, *, healthy: bool = True) -> OperatorControl:
    async def measure():
        return await measure_readiness(
            database,
            FakeBroker(healthy=healthy),
            paper_mode=True,
            expected_account_id=ACCOUNT,
            risk_caps_valid=True,
            now=NOW,
        )

    return OperatorControl(
        KillSwitchController(database), measure, token=TOKEN, clock=lambda: NOW
    )


@pytest.mark.asyncio
async def test_engaging_is_unconditional(database: Database) -> None:
    """A stop that can refuse is a stop that fails when it matters.

    No readiness is consulted and none should be: stopping is the direction an operator reaches
    for when something looks wrong, and the system being unhealthy is the reason they are
    reaching, not a reason to decline.
    """
    # A ledger in the worst state the clear path knows about.
    surface = control(database, healthy=False)

    result = await surface.handle(ENGAGE, reason="something looks wrong", presented_token=TOKEN)

    assert result.status == 200
    assert result.payload["engaged"] is True
    assert "something looks wrong" in str(result.payload["reason"])
    assert KillSwitchController(database).get().engaged is True


@pytest.mark.asyncio
async def test_clearing_succeeds_only_when_the_conditions_actually_hold(
    database: Database,
) -> None:
    """The permitting case. Without it every refusal below could be a control that never works."""
    seed(database, reconciled_at=NOW - timedelta(hours=2))
    surface = control(database)

    result = await surface.handle(CLEAR, reason="checked the ledger", presented_token=TOKEN)

    assert result.status == 200
    assert result.payload["engaged"] is False
    assert "BROKER" in result.payload["probes"]
    assert KillSwitchController(database).get().engaged is False


@pytest.mark.asyncio
async def test_clearing_is_refused_when_a_condition_fails_and_says_which(
    database: Database,
) -> None:
    """The refusal an operator has to be able to act on.

    Answering only "no" is how somebody starts looking for a way around the control, so the
    blocking reasons and the probe detail come back. A 409 rather than a 500: the request was
    understood and answered, and the answer is that conditions do not permit it.
    """
    seed(database, reconciled_at=NOW - timedelta(days=30))
    surface = control(database, healthy=False)

    result = await surface.handle(CLEAR, reason="resume please", presented_token=TOKEN)

    assert result.status == 409
    assert result.payload["refused"] is True
    assert "BROKER_UNHEALTHY" in result.payload["blocking"]
    assert "RECONCILIATION_REQUIRED" in result.payload["blocking"]
    assert result.payload["detail"], "a refusal without detail is one nobody can act on"
    assert KillSwitchController(database).get().engaged is True


@pytest.mark.asyncio
async def test_a_wrong_or_missing_token_is_refused_identically(database: Database) -> None:
    """Distinguishing them would tell a caller which half they got right."""
    seed(database, reconciled_at=NOW - timedelta(hours=2))
    surface = control(database)

    for presented in (None, "", "not-the-token-but-long-enough"):
        with pytest.raises(ControlRefused) as refused:
            await surface.handle(CLEAR, reason="let me in", presented_token=presented)
        assert refused.value.status == 403
        assert str(refused.value) == "not authorized"

    assert KillSwitchController(database).get().engaged is True, "nothing moved"


@pytest.mark.asyncio
async def test_an_unauthorized_request_never_reaches_the_switch(database: Database) -> None:
    """Authorization is checked before anything is measured or changed.

    Engaging is unconditional for an authorized operator; it must not be unconditional for
    anyone who can reach the port.
    """
    seed(database, reconciled_at=NOW - timedelta(hours=2))
    KillSwitchController(database).engage(reason="baseline", updated_at=NOW)
    surface = control(database)

    with pytest.raises(ControlRefused):
        await surface.handle(ENGAGE, reason="stop everything", presented_token="wrong-token-here")

    state = KillSwitchController(database).get()
    assert state.reason == "baseline", "an unauthorized engage must not even rewrite the reason"


@pytest.mark.asyncio
async def test_an_unauthorized_clear_never_probes_the_broker(database: Database) -> None:
    """Authorization is checked before anything is measured, not merely before anything changes.

    Probing first would let anyone who can reach the port drive a broker round trip on demand,
    and would leak whether the system is healthy through the timing of a refusal. Asserted
    directly rather than by mutation, because the ordering is the property and a mutation that
    reorders two statements is easy to write wrong -- my first attempt at one simplified to a
    no-op and "survived", which proved nothing.
    """
    seed(database, reconciled_at=NOW - timedelta(hours=2))
    probed = []

    async def measure():
        probed.append(NOW)
        raise AssertionError("readiness must not be measured for an unauthorized caller")

    surface = OperatorControl(
        KillSwitchController(database), measure, token=TOKEN, clock=lambda: NOW
    )

    with pytest.raises(ControlRefused) as refused:
        await surface.handle(CLEAR, reason="resume", presented_token="wrong-but-long-enough")

    assert refused.value.status == 403
    assert probed == [], "the broker was contacted for a caller with no token"


@pytest.mark.asyncio
async def test_a_reason_is_required_for_both_actions(database: Database) -> None:
    """An unexplained halt is one the next person clears out of frustration."""
    seed(database, reconciled_at=NOW - timedelta(hours=2))
    surface = control(database)

    for action in (ENGAGE, CLEAR):
        with pytest.raises(ControlRefused, match="reason is required"):
            await surface.handle(action, reason="   ", presented_token=TOKEN)


@pytest.mark.asyncio
async def test_only_the_two_kill_switch_actions_exist(database: Database) -> None:
    """A control surface grows by accretion unless the list is short and deliberate.

    There is no submit-order, no disable-risk, no resolve-incident. Each of those is a decision
    that should cost more than a click.
    """
    surface = control(database)

    for action in ("submit_order", "disable_risk", "resolve_incident", "promote"):
        with pytest.raises(ControlRefused, match="unknown action"):
            await surface.handle(action, reason="why not", presented_token=TOKEN)


def test_a_short_token_is_refused_at_construction(database: Database) -> None:
    """This guards the control that stops trading; a guessable token is not a guard."""

    async def measure():  # pragma: no cover - never reached
        raise AssertionError("should not be called")

    with pytest.raises(ValueError, match="at least 16 characters"):
        OperatorControl(
            KillSwitchController(database), measure, token="short", clock=lambda: NOW
        )
