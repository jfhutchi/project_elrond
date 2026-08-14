"""Durable operator and automatic hard-failure kill-switch controls."""

from __future__ import annotations

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
        evidence: ReadinessEvidence,
        updated_at: datetime,
    ) -> KillSwitchState:
        normalized = reason.strip()
        if not normalized:
            raise ValueError("kill switch reason must be nonempty")
        if evidence.reasons:
            raise KillSwitchClearBlocked(evidence.reasons)
        with self._database.transaction() as session:
            return StorageRepository(session).set_kill_switch(
                False,
                reason=normalized,
                updated_at=updated_at,
            )
