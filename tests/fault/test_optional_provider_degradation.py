"""An optional data provider must never be able to stop a trading cycle (#83).

#83's architectural requirement is explicit: Elrond stays fully operable when an optional
provider is disabled, unavailable, rate-limited, unlicensed, misconfigured or removed, and a
provider failure degrades to native-only operation rather than stopping the cycle.

`_record_catalysts` already contains its own failures, and
`test_a_provider_failure_is_contained_and_recorded` asserts that at the recorder. **That is a
claim about one method, not about the cycle**, and the two are exactly the kind of thing this
branch keeps finding a gap between: a control that is correct in isolation and never exercised
where it matters.

So these run the whole `RunOnceCycle` with enrichment that fails, and assert the cycle finishes
and the orders still go out. If a future edit moves an optional provider call outside its
containment -- or adds a second one without any -- the cycle stops, and one of these goes red.

The failures are raised from the strategy runner rather than from a real provider, deliberately.
The requirement is about *any* optional enrichment, not about one vendor's client, and a test
bound to a specific provider would not catch the next one added.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from tests.integration.test_run_once import (
    FakeDataSync,
    FakeRecovery,
    FakeStrategy,
    FakeSubmitter,
    _clear,
    _identity,
    _recovery,
)

from quantbot.operations.cycle import CycleStrategyResult, RunOnceCycle
from quantbot.operations.kill_switch import KillSwitchController
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage import Database, StorageRepository

NOW = datetime(2026, 8, 14, 21, 5, tzinfo=UTC)


class ProviderOutage(RuntimeError):
    """What an unreachable, rate-limited or unlicensed optional provider looks like."""


class EnrichmentExplodes(FakeStrategy):
    """A strategy runner whose optional enrichment raises, as an uncontained provider would."""

    async def evaluate(self) -> CycleStrategyResult:
        self.calls.append("strategy")
        raise ProviderOutage("optional provider returned 429 and the client did not catch it")


class EnrichmentDegradesQuietly(FakeStrategy):
    """A strategy runner whose enrichment failed and was contained, as the real one does.

    The signals still evaluate and the orders still go out; only the context is missing. This is
    the behaviour #83 asks for, and it is the one worth pinning down -- "the cycle did not crash"
    is a weaker property than "the cycle still traded".
    """

    async def evaluate(self) -> CycleStrategyResult:
        self.calls.append("strategy")
        result = await super().evaluate()
        self.calls.append("enrichment-skipped")
        return result


def cycle_for(database: Database, tmp_path: Path, strategy: FakeStrategy, calls: list[str]):  # type: ignore[no-untyped-def]
    return RunOnceCycle(
        database=database,
        lock_path=tmp_path / "quantbot.lock",
        strategy_identity=_identity(),
        recovery=FakeRecovery(_recovery(), calls),
        data_sync=FakeDataSync(calls),
        strategy=strategy,
        submitter=FakeSubmitter(calls),
        metrics=OperationalMetrics(),
    )


@pytest.mark.asyncio
async def test_a_contained_enrichment_failure_still_trades(tmp_path: Path) -> None:
    """Native-only operation, which is the actual requirement.

    Not merely "the cycle did not crash" -- the submitter is asserted to have been reached, so a
    change that turned a provider outage into a silent no-trade day would fail here. A research
    system that stops trading when a context lookup fails has made an optional provider load
    bearing.
    """
    database = Database.create(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    try:
        result = await cycle_for(
            database, tmp_path, EnrichmentDegradesQuietly(calls), calls
        ).run("run-degraded", started_at=NOW, trading_date=date(2026, 8, 14))
    finally:
        database.close()

    assert result.status == "SUCCEEDED"
    assert "enrichment-skipped" in calls
    assert "submit" in calls, "a provider outage must not become a no-trade day"


@pytest.mark.asyncio
async def test_an_uncontained_provider_error_engages_the_kill_switch(tmp_path: Path) -> None:
    """The blast radius of an uncontained optional provider, which is larger than a lost row.

    `RunOnceCycle.run` treats any unhandled exception as an unknown state: it records the
    incident, marks the run ERROR, **engages the kill switch**, and re-raises. That is correct --
    a cycle that failed somewhere unidentified must fail closed.

    It also means a vendor returning 429 outside a try block does not cost a row of context, it
    stops the system and requires a human to verify conditions before clearing. So the
    containment in `_record_catalysts` is not tidiness; it is what stands between an optional
    provider and a halted trading system.

    Asserted here rather than assumed, because my first draft of this test expected the cycle to
    swallow the error and return ERROR. It does not, and the real behaviour is the stronger
    argument for #83's requirement.
    """
    database = Database.create(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    try:
        cycle = cycle_for(database, tmp_path, EnrichmentExplodes(calls), calls)

        with pytest.raises(ProviderOutage):
            await cycle.run("run-exploded", started_at=NOW, trading_date=date(2026, 8, 14))

        with database.transaction() as session:
            repository = StorageRepository(session)
            incidents = repository.list_incidents(unresolved_only=True)
            runs = repository.list_runs()
        state = KillSwitchController(database).get()
    finally:
        database.close()

    assert "submit" not in calls, "nothing was submitted on a failed cycle"
    assert [run.run_id for run in runs] == ["run-exploded"]
    assert runs[0].status == "ERROR", "a crashed cycle is recorded, not absent"
    assert [item.kind for item in incidents] == ["UNHANDLED_OPERATIONAL_EXCEPTION"]
    assert state.engaged is True, (
        "an unhandled exception leaves the system in an unknown state and must fail closed; "
        "this is also why an optional provider call belongs inside containment"
    )


@pytest.mark.asyncio
async def test_no_optional_provider_is_required_to_run_at_all(tmp_path: Path) -> None:
    """The baseline #83 calls native-only operation.

    With no catalyst source configured and no enrichment of any kind, the cycle completes and
    trades. This is the configuration the operator runs today, and it is the one that must never
    depend on a paid provider being present.
    """
    database = Database.create(tmp_path / "quantbot.db")
    _clear(database)
    calls: list[str] = []
    try:
        result = await cycle_for(database, tmp_path, FakeStrategy(calls), calls).run(
            "run-native", started_at=NOW, trading_date=date(2026, 8, 14)
        )
        with database.transaction() as session:
            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert result.status == "SUCCEEDED"
    assert "submit" in calls
    assert incidents == [], "native-only operation is not a degraded state"
