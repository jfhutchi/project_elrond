"""Watch the market session without trading in it (#143).

The daemon wakes once a day. `next_cycle_at` returns `clock.next_close + 5 minutes` and that is
the entire cadence, so between the close of one session and the close of the next, nothing looks
at anything.

The operator's own store is what makes this urgent rather than tidy. On 2026-08-19 a cycle halted
on `RECONCILIATION_FAILED`. The kill switch was cleared the next day, properly, with a recorded
verification that the underlying condition had gone -- and reconciliation then ran clean four
times. **There has been no operational run since.** Ten days of nothing, with no blocking
condition in force, and nothing that noticed. Qualification days that never accrued are the cost.

**What this pass does is exactly steps 2 to 4 of `RunOnceCycle.run`, and nothing after.**

Reconciliation state, kill-switch state, market-data freshness and health. Those are the
authoritative safety checks, they are cheap, and they are the ones whose answers go stale between
one close and the next. The strategy evaluation and order submission that follow them in the
cycle are deliberately absent.

**It cannot trade, structurally rather than by discipline.**

`SessionObserver` has no `OrderSubmitter` and no strategy runner. Not a flag that disables them,
not a branch that skips them -- there is no collaborator to call. A future edit cannot
accidentally submit an order from here, because there is nothing to submit through, and a test
asserting "it did not trade" would be much weaker than a class that has no route to a broker.

That distinction matters more than it looks. The deployed strategy was qualified on daily bars
evaluated after the close. Running its evaluation every fifteen minutes would not be the same
strategy observed more often; it would be a different strategy, with a different signal cadence,
carrying forward qualification evidence it never earned. Continuous *observation* is a runtime
change. Continuous *evaluation* would be a strategy change, and it is not one this makes.

**Every pass writes a row, including the healthy ones.**

A monitor that only records problems cannot distinguish "nothing was wrong" from "nothing ran",
which is precisely the failure above: the absence of incidents for ten days looked exactly like
health. A heartbeat that is only durable when it fails is not a heartbeat.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from quantbot.operations.cycle import DataSyncRunner, RecoveryRunner
from quantbot.operations.health import OperationsModel
from quantbot.operations.metrics import OperationalMetrics
from quantbot.storage.database import Database, encode_utc
from quantbot.storage.schema import session_observations


class ObservationOutcome(StrEnum):
    """What the pass found. Three states, and the third is the one that matters."""

    #: Every check answered and none objected.
    HEALTHY = "HEALTHY"
    #: A check objected. The session is being watched and something is wrong.
    DEGRADED = "DEGRADED"
    #: The kill switch is engaged. Distinct from `DEGRADED` because it is a deliberate stop
    #: rather than a fault, and reading it as a fault is how somebody clears it to make an
    #: alert go away.
    HALTED = "HALTED"


class KillSwitchReader(Protocol):
    """Only the read. This pass has no route to engaging or clearing anything."""

    def get(self) -> object: ...


class SessionObservation(OperationsModel):
    """One pass over the authoritative checks, and what it saw."""

    observation_id: str
    observed_at: datetime
    market_open: bool
    outcome: ObservationOutcome
    #: Why it is not healthy. Empty exactly when the outcome is `HEALTHY`, which the table
    #: enforces as a check constraint rather than trusting this model to be the only writer.
    reasons: tuple[str, ...] = ()
    market_data_age_seconds: Decimal | None = None

    @property
    def healthy(self) -> bool:
        return self.outcome is ObservationOutcome.HEALTHY


def observation_id(observed_at: datetime) -> str:
    """A content-addressed id for one observation.

    Derived from the instant rather than from a counter, so a restart cannot mint a second id
    for a pass that already happened. #143 requires that restart and recovery not reinterpret an
    old event as a new independent one, and a monotonic counter is the mechanism that gets that
    wrong on exactly the restart the requirement names.
    """
    digest = hashlib.sha256(encode_utc(observed_at).encode("utf-8")).hexdigest()
    return f"observation-{digest[:32]}"


@dataclass(slots=True)
class SessionObserver:
    """Run the authoritative checks during a session. Records; never acts.

    Deliberately takes no `OrderSubmitter` and no strategy runner. See the module docstring: the
    inability to trade is structural, and a collaborator added here later would remove it.
    """

    database: Database
    recovery: RecoveryRunner
    data_sync: DataSyncRunner
    kill_switch: KillSwitchReader
    metrics: OperationalMetrics
    max_data_age_seconds: int

    async def observe(self, *, now: datetime, market_open: bool) -> SessionObservation:
        """One pass. Always returns an observation, and always records it."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        moment = now.astimezone(UTC)
        identifier = observation_id(moment)

        if getattr(self.kill_switch.get(), "engaged", False):
            # Checked first and returned immediately. A halted system is not expected to be
            # reconciled or fresh, and reporting those as faults on top of a deliberate stop
            # buries the one fact an operator needs.
            return self._record(
                SessionObservation(
                    observation_id=identifier,
                    observed_at=moment,
                    market_open=market_open,
                    outcome=ObservationOutcome.HALTED,
                    reasons=("KILL_SWITCH_ENGAGED",),
                )
            )

        reasons: list[str] = []
        recovery = await self.recovery.recover(
            f"{identifier}:reconciliation", started_at=moment, run_id=None
        )
        if not recovery.ready:
            reasons.extend(recovery.reasons)

        data = await self.data_sync.sync()
        age = Decimal(str((moment - data.completed_through).total_seconds()))
        if age < 0:
            reasons.append("MARKET_DATA_FROM_FUTURE")
        elif age > Decimal(self.max_data_age_seconds):
            reasons.append("MARKET_DATA_STALE")
        if data.stale_symbols and "MARKET_DATA_STALE" not in reasons:
            reasons.append("MARKET_DATA_STALE")
        if not data.healthy and not reasons:
            reasons.append("MARKET_DATA_UNHEALTHY")
        if not data.stream_connected:
            reasons.append("TRADE_STREAM_DISCONNECTED")
        if not data.risk_healthy:
            reasons.append("RISK_ENGINE_UNHEALTHY")
        reasons.extend(data.reasons)

        deduplicated = tuple(dict.fromkeys(reasons))
        self.metrics.set_gauge(
            "quantbot_market_data_age_seconds", max(age, Decimal("0"))
        )
        return self._record(
            SessionObservation(
                observation_id=identifier,
                observed_at=moment,
                market_open=market_open,
                outcome=(
                    ObservationOutcome.HEALTHY
                    if not deduplicated
                    else ObservationOutcome.DEGRADED
                ),
                reasons=deduplicated,
                market_data_age_seconds=age,
            )
        )

    def _record(self, observation: SessionObservation) -> SessionObservation:
        """Write the heartbeat, including a healthy one.

        A second write under the same id is ignored rather than raised. Two passes at the same
        instant is a scheduler artefact, not a research claim, and an exception here would take
        down the loop whose whole purpose is to keep running.
        """
        with self.database.transaction() as session:
            existing = session.execute(
                session_observations.select().where(
                    session_observations.c.observation_id == observation.observation_id
                )
            ).one_or_none()
            if existing is None:
                session.execute(
                    session_observations.insert().values(
                        observation_id=observation.observation_id,
                        observed_at=encode_utc(observation.observed_at),
                        market_open=1 if observation.market_open else 0,
                        outcome=observation.outcome.value,
                        reasons=",".join(observation.reasons),
                        market_data_age_seconds=(
                            None
                            if observation.market_data_age_seconds is None
                            else str(observation.market_data_age_seconds)
                        ),
                        detail=json.dumps(
                            {"reasons": list(observation.reasons)}, sort_keys=True
                        ),
                    )
                )
        self.metrics.increment("quantbot_session_observations_total")
        return observation


__all__ = [
    "KillSwitchReader",
    "ObservationOutcome",
    "SessionObservation",
    "SessionObserver",
    "observation_id",
]
