"""Identity-bound PAPER gateway for concrete risk-managed learner runners.

This module neither promotes a model nor supplies risk approval. A runner must compute
real sizing/veto decisions and implement horizon/session exits independently of entry
eligibility. The factory keeps the operational runtime's lock, recovery, data sync and
ExecutionCoordinator. Read-only eligibility also works on schema-28 operational stores;
it does not open them through the newer learner Database schema or migrate them.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from contextlib import closing
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from quantbot.domain import BrokerEnvironment, OrderIntent, StrategyIdentity, TradingMode
from quantbot.execution import ExecutionCoordinator, SubmissionResult
from quantbot.operations.cycle import (
    CycleStrategyResult,
    DataSyncRunner,
    OrderSubmitter,
    RunOnceCycle,
)
from quantbot.operations.paper_clock import ClockContract
from quantbot.operations.qualification import QualificationTracker
from quantbot.research.manifest import content_id
from quantbot.research.promotion import PromotionState, Stage
from quantbot.risk import DrawdownState, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import Database

if TYPE_CHECKING:
    from quantbot.runtime import Runtime

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_ENTRY_STAGES = frozenset(
    {Stage.PAPER_OBSERVATION, Stage.PAPER_QUALIFIED, Stage.LIVE_REVIEW_ELIGIBLE}
)


class PaperModelBinding(BaseModel):
    """Frozen model and execution contract; changing any field creates a new identity.

    ``code_hash`` must describe the executable learner/runner implementation, not a
    human label. ``execution_config`` must include the feature/horizon, selection,
    sizing, risk and exit configuration actually used by the concrete runner.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    model_id: str = Field(min_length=1)
    artifact_hash: Digest
    code_hash: Digest
    execution_config_hash: Digest

    @classmethod
    def from_config(
        cls,
        *,
        model_id: str,
        artifact_hash: str,
        code_hash: str,
        execution_config: Mapping[str, object],
    ) -> PaperModelBinding:
        return cls(
            model_id=model_id,
            artifact_hash=artifact_hash,
            code_hash=code_hash,
            execution_config_hash=content_id(execution_config),
        )

    @property
    def configuration_hash(self) -> str:
        return content_id({"gateway": "learner-paper-v1", **self.model_dump()})

    def strategy_identity(self, *, git_commit: str, deployed_at: datetime) -> StrategyIdentity:
        """Construct expected identity only; never register or deploy it."""
        digest = self.configuration_hash
        return StrategyIdentity(
            strategy_id=f"learner-{digest}",
            version=digest,
            git_commit=git_commit,
            configuration_hash=digest,
            deployment_timestamp=deployed_at,
        )


class PaperEligibility(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    reasons: tuple[str, ...]
    stage: str | None = None

    @property
    def allowed(self) -> bool:
        return not self.reasons


class PaperEntryBlocked(RuntimeError):
    def __init__(self, decision: PaperEligibility) -> None:
        self.decision = decision
        super().__init__(", ".join(decision.reasons))


def assess_paper_entry(
    operational_store: str | Path,
    *,
    binding: PaperModelBinding,
    identity: StrategyIdentity,
) -> PaperEligibility:
    """Read both durable records in one read-only snapshot; never accept gate claims.

    This reports promotion eligibility, not permission to submit: all ordinary runtime,
    broker, market, sizing and veto controls still apply. Counts are never manufactured
    here, and PAPER_OBSERVATION intentionally does not require PAPER_QUALIFIED.
    """
    expected = binding.strategy_identity(
        git_commit=identity.git_commit, deployed_at=identity.deployment_timestamp
    )
    if identity != expected:
        return PaperEligibility(reasons=("PAPER_MODEL_IDENTITY_MISMATCH",))
    reasons: list[str] = []
    stage: str | None = None
    try:
        uri = Path(operational_store).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN")
            deployment_row = connection.execute(
                "SELECT strategy_id, version, git_commit, configuration_hash, "
                "deployment_timestamp FROM strategy_deployments "
                "WHERE strategy_id = ? AND version = ?",
                (identity.strategy_id, identity.version),
            ).fetchone()
            promotion_row = connection.execute(
                "SELECT strategy_id, stage, strategy_version, configuration_hash, reason, "
                "actor, updated_at, hypothesis_id, hypothesis_version, objective_identity, "
                "evidence_identity FROM promotion_state WHERE strategy_id = ?",
                (identity.strategy_id,),
            ).fetchone()
        if deployment_row is None:
            reasons.append("PAPER_DEPLOYMENT_MISSING")
        elif StrategyIdentity.model_validate(dict(deployment_row)) != identity:
            reasons.append("PAPER_DEPLOYMENT_IDENTITY_MISMATCH")
        if promotion_row is None:
            reasons.append("PAPER_PROMOTION_MISSING")
        else:
            promotion = PromotionState.model_validate(dict(promotion_row))
            stage = promotion.stage.value
            if (
                promotion.strategy_version != identity.version
                or promotion.configuration_hash != identity.configuration_hash
            ):
                reasons.append("PAPER_PROMOTION_IDENTITY_MISMATCH")
            if promotion.stage not in _ENTRY_STAGES:
                reasons.append("PAPER_OBSERVATION_NOT_EARNED")
    except (OSError, sqlite3.Error, ValidationError, ValueError):
        reasons.append("PAPER_GATE_STATE_UNAVAILABLE")
    return PaperEligibility(reasons=tuple(reasons), stage=stage)


class RiskManagedPaperRunner(Protocol):
    async def evaluate(self) -> CycleStrategyResult:
        """Compute entries and exits with actual sizing, market eligibility and vetoes."""
        ...

    async def evaluate_exits(self) -> CycleStrategyResult:
        """Compute horizon/session/risk exits even when model promotion is revoked."""
        ...


class PromotionGatedStrategy:
    def __init__(
        self,
        operational_store: str | Path,
        runner: RiskManagedPaperRunner,
        *,
        binding: PaperModelBinding,
        identity: StrategyIdentity,
    ) -> None:
        self._store, self._runner = operational_store, runner
        self._binding, self._identity = binding, identity
        self.entry_eligible_at_evaluation = False

    async def evaluate(self) -> CycleStrategyResult:
        decision = assess_paper_entry(self._store, binding=self._binding, identity=self._identity)
        self.entry_eligible_at_evaluation = decision.allowed
        if decision.allowed:
            return await self._runner.evaluate()
        result = await self._runner.evaluate_exits()
        if any(request.purpose is OrderPurpose.ENTRY for request in result.submissions):
            raise PaperEntryBlocked(PaperEligibility(reasons=("EXIT_ONLY_RUNNER_RETURNED_ENTRY",)))
        # Preserve exact outer gate diagnostics even when the entry runner is
        # skipped. Exit behavior and the historical CycleResult schema remain intact.
        return result.model_copy(update={"entry_gate_reasons": decision.reasons})


class _PaperQualificationTracker(QualificationTracker):
    """Keep exit-only maintenance from becoming evidence for a promoted candidate."""

    def __init__(
        self,
        database: Database,
        strategy: PromotionGatedStrategy,
        *,
        binding: PaperModelBinding,
        identity: StrategyIdentity,
    ) -> None:
        super().__init__(database, identity.strategy_id)
        self._strategy, self._binding, self._identity = strategy, binding, identity

    def record_real_trading_day(
        self,
        trading_date: date,
        *,
        qualified: bool,
        detail: dict[str, object],
    ) -> bool:
        eligibility = assess_paper_entry(
            self._database.path, binding=self._binding, identity=self._identity
        )
        observed = qualified and self._strategy.entry_eligible_at_evaluation and eligibility.allowed
        return super().record_real_trading_day(
            trading_date,
            qualified=observed,
            detail={
                **detail,
                "paper_entry_eligible": observed,
                "paper_gate_reasons": eligibility.reasons,
            },
        )


class PromotionGatedSubmitter:
    def __init__(
        self,
        operational_store: str | Path,
        submitter: OrderSubmitter,
        *,
        binding: PaperModelBinding,
        identity: StrategyIdentity,
    ) -> None:
        self._store, self._submitter = operational_store, submitter
        self._binding, self._identity = binding, identity

    async def submit(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None = None,
    ) -> SubmissionResult:
        if purpose is OrderPurpose.ENTRY:
            if intent.strategy_id != self._identity.strategy_id:
                raise PaperEntryBlocked(
                    PaperEligibility(reasons=("PAPER_INTENT_IDENTITY_MISMATCH",))
                )
            decision = assess_paper_entry(
                self._store, binding=self._binding, identity=self._identity
            )
            if not decision.allowed:
                raise PaperEntryBlocked(decision)
        return await self._submitter.submit(
            intent,
            purpose=purpose,
            market_eligible=market_eligible,
            drawdown=drawdown,
            risk_approved=risk_approved,
            vetoes=vetoes,
        )


def build_learner_paper_cycle(
    runtime: Runtime,
    runner: RiskManagedPaperRunner,
    *,
    binding: PaperModelBinding,
    identity: StrategyIdentity,
    max_data_age_seconds: int = 900,
    data_sync: DataSyncRunner | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    submission_wrapper: Callable[[PromotionGatedSubmitter], OrderSubmitter] | None = None,
    clock_contract: ClockContract='STRICT_V0',
) -> RunOnceCycle:
    """Compose a configured runtime; do not open, migrate, deploy or start one.

    The caller must provide a concrete intraday risk runner and an intraday-configured
    runtime/data synchronizer. This gateway does not turn the daily runtime into an
    intraday strategy. Continue the independent position protection service too.
    The caller binds the run id to its runner before invoking the returned cycle.
    """
    return LearnerPaperCycle(
        runtime,
        runner,
        binding=binding,
        identity=identity,
        max_data_age_seconds=max_data_age_seconds,
        data_sync=data_sync,
        clock=clock,
        submission_wrapper=submission_wrapper,
        clock_contract=clock_contract,
    )


def _validate_runtime(
    runtime: Runtime,
    runner: RiskManagedPaperRunner,
    *,
    binding: PaperModelBinding,
    identity: StrategyIdentity,
) -> None:
    if (
        runtime.settings.TRADING_MODE is not TradingMode.PAPER
        or runtime.settings.BROKER_ENVIRONMENT is not BrokerEnvironment.PAPER
    ):
        raise ValueError("learner execution requires PAPER mode and PAPER broker environment")
    if not isinstance(runtime.coordinator, ExecutionCoordinator):
        raise TypeError("learner PAPER cycle requires the existing ExecutionCoordinator")
    if not callable(getattr(runner, "evaluate_exits", None)):
        raise TypeError("learner PAPER runner must implement independent evaluate_exits")
    if identity.git_commit != runtime.running_git_commit:
        raise ValueError("learner deployment must match the running checkout commit")
    expected = binding.strategy_identity(
        git_commit=identity.git_commit, deployed_at=identity.deployment_timestamp
    )
    if expected != identity:
        raise ValueError("learner deployment must match the model and execution configuration")


class LearnerPaperCycle(RunOnceCycle):
    """Existing operational lifecycle, with eligibility-bound qualification recording."""

    def __init__(
        self,
        runtime: Runtime,
        runner: RiskManagedPaperRunner,
        *,
        binding: PaperModelBinding,
        identity: StrategyIdentity,
        max_data_age_seconds: int,
        data_sync: DataSyncRunner | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        submission_wrapper: Callable[[PromotionGatedSubmitter], OrderSubmitter] | None = None,
        clock_contract: ClockContract='STRICT_V0',
    ) -> None:
        _validate_runtime(runtime, runner, binding=binding, identity=identity)
        from quantbot.learner.paper_deadlines import DeadlineGatedSubmitter

        if runtime.settings.EXPECTED_ACCOUNT_ID is None:
            raise ValueError("learner PAPER cycle requires a pinned account")
        strategy = PromotionGatedStrategy(
            runtime.database.path, runner, binding=binding, identity=identity
        )
        retained_submitter = PromotionGatedSubmitter(
            runtime.database.path,
            DeadlineGatedSubmitter(
                runtime.database, runtime.coordinator,
                expected_account_id=runtime.settings.EXPECTED_ACCOUNT_ID, clock=clock,
                clock_contract=clock_contract,
            ),
            binding=binding, identity=identity,
        )
        super().__init__(
            database=runtime.database,
            lock_path=runtime.paths.lock,
            strategy_identity=identity,
            recovery=runtime.recovery,
            data_sync=data_sync or runtime.data_sync,
            strategy=strategy,
            submitter=(retained_submitter if submission_wrapper is None
                       else submission_wrapper(retained_submitter)),
            metrics=runtime.metrics,
            runtime=runtime.runtime_context(),
            max_data_age_seconds=max_data_age_seconds,
        )
        # RunOnceCycle owns when a real cycle records a day; only narrow its qualification.
        self._qualification = _PaperQualificationTracker(
            runtime.database,
            strategy,
            binding=binding,
            identity=identity,
        )
