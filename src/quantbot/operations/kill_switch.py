"""Durable operator and automatic hard-failure kill-switch controls."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from quantbot.storage import Database, KillSwitchState, StorageRepository


class ReadinessEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    paper_mode: bool
    account_verified: bool
    broker_healthy: bool
    data_healthy: bool
    risk_healthy: bool
    reconciliation_successful: bool

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if not self.paper_mode:
            reasons.append("PAPER_MODE_REQUIRED")
        if not self.account_verified:
            reasons.append("ACCOUNT_VERIFICATION_REQUIRED")
        if not self.broker_healthy:
            reasons.append("BROKER_UNHEALTHY")
        if not self.data_healthy:
            reasons.append("MARKET_DATA_UNHEALTHY")
        if not self.risk_healthy:
            reasons.append("RISK_ENGINE_UNHEALTHY")
        if not self.reconciliation_successful:
            reasons.append("RECONCILIATION_REQUIRED")
        return tuple(reasons)


#: The unhealthy reason each unprobed condition replaces, so a condition nobody asked about is
#: not reported as one that answered badly.
UNPROBED_SUPERSEDES = {
    "BROKER": "BROKER_UNHEALTHY",
    "ACCOUNT": "ACCOUNT_VERIFICATION_REQUIRED",
    "MARKET_DATA": "MARKET_DATA_UNHEALTHY",
    "RISK": "RISK_ENGINE_UNHEALTHY",
    "RECONCILIATION": "RECONCILIATION_REQUIRED",
}

_MEASURED_TOKEN = object()


@dataclass(frozen=True, slots=True)
class MeasuredReadiness:
    """Readiness established by probing, plus a record of what was and was not probed.

    Lives here rather than beside `measure_readiness` so the token stays genuinely private to
    the module that owns the gate. `readiness.py` mints one through `issue_measured_readiness`;
    nothing else can, because nothing else can see the token.

    `probes` and `unprobed` are not decoration. "Ready" from six probes and "ready" from four
    probes and two assumptions are different facts, and an operator reading a refusal needs to
    know which conditions were actually established.
    """

    evidence: ReadinessEvidence
    measured_at: datetime
    probes: tuple[str, ...]
    unprobed: tuple[str, ...]
    detail: tuple[str, ...]

    def __init__(
        self,
        *,
        evidence: ReadinessEvidence,
        measured_at: datetime,
        probes: Sequence[str],
        unprobed: Sequence[str],
        detail: Sequence[str],
        token: object,
    ) -> None:
        if token is not _MEASURED_TOKEN:
            raise TypeError(
                "MeasuredReadiness comes from measure_readiness(); clearance evidence cannot "
                "be constructed, only measured"
            )
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "measured_at", measured_at)
        object.__setattr__(self, "probes", tuple(probes))
        object.__setattr__(self, "unprobed", tuple(unprobed))
        object.__setattr__(self, "detail", tuple(detail))

    @property
    def blocking(self) -> tuple[str, ...]:
        """Every reason to refuse a clear: unmet conditions and unmeasured ones alike.

        An unprobed condition is not a satisfied condition. Failing closed on it is the only
        safe direction, because an unmeasured condition reported as satisfied is precisely the
        defect this type exists to remove.

        A condition that was not probed reports `_UNVERIFIED` and **not** the corresponding
        unhealthy reason. `ReadinessEvidence` needs a boolean and an unprobed condition is
        recorded as `False`, which would otherwise surface as "the broker is unhealthy" when
        the truth is "nobody asked it". Those call for different operator actions -- restart the
        broker versus configure an adapter -- and reporting both makes the real one harder to
        find. Both still block.
        """
        superseded = {UNPROBED_SUPERSEDES[name] for name in self.unprobed}
        unmet = tuple(
            reason for reason in self.evidence.reasons if reason not in superseded
        )
        return unmet + tuple(f"{name}_UNVERIFIED" for name in self.unprobed)

    @property
    def ready(self) -> bool:
        return not self.blocking


def issue_measured_readiness(
    *,
    evidence: ReadinessEvidence,
    measured_at: datetime,
    probes: Sequence[str],
    unprobed: Sequence[str],
    detail: Sequence[str],
) -> MeasuredReadiness:
    """Mint measured readiness. Called only by `readiness.measure_readiness`.

    Deliberately not marked private: `readiness.py` has to reach it, and an underscore name
    imported across modules is a convention pretending to be a boundary. The boundary is that
    this function is the only route to the token, and it is called from exactly one place --
    which is checked by a test rather than trusted.
    """
    return MeasuredReadiness(
        evidence=evidence,
        measured_at=measured_at,
        probes=probes,
        unprobed=unprobed,
        detail=detail,
        token=_MEASURED_TOKEN,
    )


class KillSwitchClearBlocked(RuntimeError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("kill switch clear blocked: " + ", ".join(reasons))


class KillSwitchController:
    def __init__(self, database: Database) -> None:
        self._database = database

    def get(self) -> KillSwitchState:
        with self._database.transaction() as session:
            return StorageRepository(session).get_kill_switch_state()

    def engage(self, *, reason: str, updated_at: datetime) -> KillSwitchState:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("kill switch reason must be nonempty")
        with self._database.transaction() as session:
            return StorageRepository(session).set_kill_switch(
                True,
                reason=normalized,
                updated_at=updated_at,
            )

    def engage_hard_failure(self, failure: str, *, updated_at: datetime) -> KillSwitchState:
        normalized = failure.strip().upper()
        if not normalized:
            raise ValueError("hard failure must be nonempty")
        return self.engage(reason=normalized, updated_at=updated_at)

    def clear(
        self,
        *,
        reason: str,
        measured: MeasuredReadiness,
        updated_at: datetime,
    ) -> KillSwitchState:
        """Resume trading, but only against readiness that was measured rather than asserted.

        Takes `MeasuredReadiness` rather than `ReadinessEvidence`. The evidence type is an
        ordinary value object anyone can build with six `True`s, and three of its fields used to
        come straight from environment variables -- so the gate was reading its input from a
        place the thing being gated could set. `MeasuredReadiness` carries a module-private
        token and only `measure_readiness()` can produce one, which turns "clearance must be
        verified" from a convention into something a caller cannot skip.

        `blocking` covers unmeasured conditions as well as unmet ones. An unprobed condition is
        not a satisfied condition, and treating it as one is the failure this parameter exists
        to prevent.
        """
        normalized = reason.strip()
        if not normalized:
            raise ValueError("kill switch reason must be nonempty")
        if not isinstance(measured, MeasuredReadiness):
            raise TypeError(
                "clearing the kill switch needs readiness from measure_readiness(); a "
                "ReadinessEvidence assembled by a caller is a claim, not a measurement"
            )
        if measured.blocking:
            raise KillSwitchClearBlocked(measured.blocking)
        with self._database.transaction() as session:
            return StorageRepository(session).set_kill_switch(
                False,
                reason=normalized,
                updated_at=updated_at,
            )
