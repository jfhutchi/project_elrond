"""Evidence-only paper qualification tracking; no day is synthesized."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from quantbot.storage import Database, StorageRepository


class QualificationPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    minimum_trading_days: int = Field(default=30, ge=30)
    minimum_meaningful_trades: int = Field(default=30, ge=30)


class QualificationAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    paper_qualified: bool
    verified_trading_days: int
    meaningful_trades: int
    unresolved_incidents: int
    reasons: tuple[str, ...]


class QualificationTracker:
    def __init__(
        self,
        database: Database,
        strategy_id: str,
        policy: QualificationPolicy | None = None,
    ) -> None:
        self._database = database
        self._strategy_id = strategy_id
        self._policy = policy or QualificationPolicy()

    def record_real_trading_day(
        self,
        trading_date: date,
        *,
        qualified: bool,
        detail: dict[str, object],
    ) -> bool:
        with self._database.transaction() as session:
            return StorageRepository(session).record_qualification_day(
                self._strategy_id,
                trading_date,
                qualified=qualified,
                detail=detail,
            )

    def assess(self, *, meaningful_trades: int) -> QualificationAssessment:
        if meaningful_trades < 0:
            raise ValueError("meaningful_trades must be nonnegative")
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            trading_days = repository.count_qualification_days(self._strategy_id)
            unresolved = len(repository.list_incidents(unresolved_only=True))
        reasons: list[str] = []
        if trading_days < self._policy.minimum_trading_days:
            reasons.append("INSUFFICIENT_REAL_TRADING_DAYS")
        if meaningful_trades < self._policy.minimum_meaningful_trades:
            reasons.append("INSUFFICIENT_MEANINGFUL_TRADES")
        if unresolved:
            reasons.append("UNRESOLVED_INCIDENTS")
        return QualificationAssessment(
            paper_qualified=not reasons,
            verified_trading_days=trading_days,
            meaningful_trades=meaningful_trades,
            unresolved_incidents=unresolved,
            reasons=tuple(reasons),
        )
