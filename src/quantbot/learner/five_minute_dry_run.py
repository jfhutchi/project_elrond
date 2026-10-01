"""Read-only five-minute eligibility diagnostics. No runtime or broker construction.

An intent here is a hypothetical record, never a domain OrderIntent. Observations
retain their original model, cost floor and evidence class; this does not commission
an execution contract or earn promotion. Missing broker/portfolio measurements veto.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from quantbot.domain import StrategyIdentity
from quantbot.learner.five_minute import FiveMinuteFeatureRow
from quantbot.learner.five_minute_forward import FiveMinutePrediction, forecast
from quantbot.learner.intraday import _aware
from quantbot.learner.paper import PaperEligibility, PaperModelBinding, assess_paper_entry
from quantbot.learner.paper_deadlines import supervisor_objections
from quantbot.learner.paper_runner import PaperExecutionPolicy
from quantbot.operations.runners import _position_open_times
from quantbot.research.manifest import content_id
from quantbot.risk import (
    EntrySizingRequest,
    PositionRiskInput,
    RiskApproval,
    build_portfolio_risk_state,
    calculate_drawdown,
    size_entry,
)
from quantbot.risk.vetoes import (
    PortfolioVetoInputs,
    PortfolioVetoPolicy,
    evaluate_portfolio_vetoes,
)
from quantbot.storage import Database, StorageRepository
from quantbot.strategy import StrategyConfig


@dataclass(frozen=True)
class DryRunContext:
    source_features: FiveMinuteFeatureRow | None = None
    new_position_count: int | None = None
    sizing: EntrySizingRequest | None = None
    eligibility: PaperEligibility | None = None
    operational_reasons: tuple[str, ...] = ("PAPER_OPERATIONAL_STATE_UNAVAILABLE",)
    broker_verified_at: datetime | None = None
    session_exit_at: datetime | None = None
    veto_inputs: PortfolioVetoInputs | None = None
    veto_policy: PortfolioVetoPolicy | None = None
    seen_intents: tuple[str, ...] = ()


@dataclass(frozen=True)
class HypotheticalIntent:
    intent_id: str
    symbol: str
    quantity: Decimal
    reference_price: Decimal
    stop_price: Decimal
    deadline_at: datetime
    order_authority: Literal[False] = False
    exit_authority: Literal[False] = False


@dataclass(frozen=True)
class DryRunResult:
    prediction_id: str
    intent_id: str
    evaluated_at: datetime
    reasons: tuple[str, ...]
    intent: HypotheticalIntent | None
    sizing_status: str
    veto_status: str
    orders: Literal[0] = 0
    order_authority: Literal[False] = False
    exit_authority: Literal[False] = False
    evidence_class: Literal["ELIGIBILITY_DRY_RUN_NOT_TRADING_EVIDENCE"] = (
        "ELIGIBILITY_DRY_RUN_NOT_TRADING_EVIDENCE"
    )


def evaluate(
    model: dict[str, Any],
    prediction: FiveMinutePrediction,
    *,
    now: datetime,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    context: DryRunContext,
) -> DryRunResult:
    """Evaluate actual immutable forecast plus measured controls, without submitting.

    Only callers with all measurements may obtain a hypothetical intent. A successful
    diagnostic is still not permission to trade. All holdings remain in aggregate risk;
    this function never assumes protection/exit ownership of grandfathered positions.
    """
    now = _aware(now)
    # Revalidate frozen value objects; model_copy must not evade literal constraints.
    prediction = FiveMinutePrediction.model_validate(prediction.model_dump())
    policy = PaperExecutionPolicy.model_validate(policy.model_dump())
    reasons: list[str] = []
    if prediction.features.symbol != "DIA" or prediction.features.symbol not in config.universe:
        reasons.append("PAPER_SYMBOL_OUTSIDE_APPROVED_ROSTER")
    if context.source_features is None:
        reasons.append("FIVE_MINUTE_SOURCE_LINEAGE_UNVERIFIED")
    elif context.source_features != prediction.features:
        reasons.append("FIVE_MINUTE_SOURCE_LINEAGE_MISMATCH")
    if context.new_position_count is None or context.new_position_count < 0:
        reasons.append("PAPER_ENTRY_OWNERSHIP_UNVERIFIED")
    elif context.new_position_count >= 1:
        reasons.append("PAPER_MODEL_POSITION_LIMIT")
    if policy.stop_loss_bps > 100:
        reasons.append("PAPER_STOP_EXCEEDS_APPROVED_PILOT")
    try:
        (expected,) = forecast(
            model, prediction.model_id, (prediction.features,), recorded_at=prediction.recorded_at
        )
        if expected != prediction:
            reasons.append("FIVE_MINUTE_FROZEN_CONTRACT_MISMATCH")
    except (KeyError, TypeError, ValueError):
        reasons.append("FIVE_MINUTE_FROZEN_CONTRACT_MISMATCH")
    prediction_id = content_id(prediction)
    intent_id = content_id(
        {
            "dry_run": "five-minute-v1",
            "model": prediction.model_id,
            "cutoff": prediction.features.feature_at,
            "symbol": prediction.features.symbol,
            "policy": policy,
            "strategy": config,
        }
    )
    if intent_id in context.seen_intents:
        reasons.append("PAPER_DUPLICATE_DRY_RUN_INTENT")
    if not prediction.recorded_at <= now < prediction.features.target_at:
        reasons.append("PAPER_PREDICTION_NOT_CURRENT")
    age = (now - prediction.features.feature_at).total_seconds()
    if not 0 <= age <= policy.max_reference_age_seconds:
        reasons.append("PAPER_REFERENCE_STALE_OR_FUTURE")
    if prediction.score <= max(prediction.fixed_cost_floor, policy.round_trip_cost_bps / 10000):
        reasons.append("PAPER_SCORE_BELOW_COST_FLOOR")
    if context.eligibility is None:
        reasons.append("PAPER_GATE_STATE_UNAVAILABLE")
    else:
        reasons.extend(context.eligibility.reasons)
    reasons.extend(context.operational_reasons)
    if context.broker_verified_at is None or not (
        0 <= (now - _aware(context.broker_verified_at)).total_seconds() <= 120
    ):
        reasons.append("PAPER_BROKER_STATE_UNVERIFIED")
    if context.session_exit_at is None:
        reasons.append("PAPER_SESSION_CALENDAR_UNAVAILABLE")
    elif (
        prediction.features.target_at > _aware(context.session_exit_at)
        or now >= context.session_exit_at
    ):
        reasons.append("PAPER_HORIZON_EXCEEDS_SESSION_EXIT")
    sizing_status, veto_status = "UNMEASURED", "UNMEASURED"
    sized = None
    reference = Decimal(str(prediction.features.reference_close))
    if context.sizing is None:
        reasons.append("PAPER_SIZING_INPUTS_UNAVAILABLE")
    else:
        request = context.sizing
        reference = Decimal(str(prediction.features.reference_close))
        if (
            request.symbol != prediction.features.symbol
            or request.reference_price != reference
            or request.stop_distance != reference * Decimal(policy.stop_loss_bps) / 10000
        ):
            reasons.append("PAPER_SIZING_CONTRACT_MISMATCH")
        else:
            # Retain every tighter base control and never raise max_positions.
            bounded = StrategyConfig.model_validate(
                {
                    **config.model_dump(),
                    "risk_per_trade_bps": min(25, config.risk_per_trade_bps),
                    "max_position_value_bps": min(500, config.max_position_value_bps),
                }
            )
            sized = size_entry(request, bounded)
            sizing_status = "PASSED" if isinstance(sized, RiskApproval) else "REJECTED"
            if not isinstance(sized, RiskApproval):
                reasons.extend(sized.reasons)
    if context.veto_inputs is None or context.veto_policy is None:
        reasons.append("PAPER_VETO_INPUTS_UNAVAILABLE")
    elif isinstance(sized, RiskApproval) and context.sizing is not None:
        v = context.veto_inputs
        if (
            v.symbol != prediction.features.symbol
            or v.order_quantity != sized.quantity
            or v.order_net_exposure != sized.quantity * reference
            or v.observed_at != now
            or v.equity != context.sizing.portfolio.equity
        ):
            reasons.append("PAPER_VETO_INPUTS_MISMATCH")
        else:
            veto = evaluate_portfolio_vetoes(v, context.veto_policy)
            veto_status = "PASSED" if veto.allowed else "REJECTED"
            reasons.extend(veto.reasons)
    intent = None
    if not reasons and isinstance(sized, RiskApproval):
        # Match existing realistic slippage + commission check after integer sizing.
        modeled_cost = Decimal(
            2 * max(5, config.slippage_bps)
        ) / 10000 + 2 * config.commission_per_order / (sized.quantity * reference)
        if Decimal(str(prediction.score)) <= modeled_cost:
            reasons.append("PAPER_SCORE_BELOW_MODELED_COST")
        else:
            intent = HypotheticalIntent(
                intent_id,
                prediction.features.symbol,
                sized.quantity,
                reference,
                reference - sized.stop_distance,
                prediction.features.target_at,
            )
    return DryRunResult(
        prediction_id,
        intent_id,
        now,
        tuple(dict.fromkeys(reasons)),
        intent,
        sizing_status,
        veto_status,
    )


class ReadOnlyStore:
    """Existing repository readers on an OS-enforced read-only SQLite connection."""

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.engine = create_engine("sqlite://", creator=self.connect)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            try:
                yield session
            finally:
                session.rollback()

    def close(self) -> None:
        self.engine.dispose()


def operational_objections(
    store: ReadOnlyStore,
    *,
    account_id: str,
    now: datetime,
    position_count: int | None = None,
) -> tuple[str, ...]:
    """Read real recovery/protection/deadline gates. No readiness defaults or writes.

    Snapshot-age bound is 120 seconds for this prospective dry-run. It does not
    change scheduled-service stale thresholds or claim a healthy older cycle failed.
    """
    now = _aware(now)
    reasons = list(supervisor_objections(cast(Database, store), account_id=account_id, now=now))
    with closing(store.connect()) as connection:
        connection.row_factory = sqlite3.Row
        kill = connection.execute("SELECT engaged FROM kill_switch_state WHERE id=1").fetchone()
        if kill is None or kill["engaged"]:
            reasons.append("PAPER_KILL_SWITCH_ENGAGED_OR_UNAVAILABLE")
        recon = connection.execute(
            "SELECT completed_at,status FROM reconciliation_runs ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
        if recon is None or recon["status"] != "RECONCILED":
            reasons.append("PAPER_RECONCILIATION_REQUIRED")
        elif not 0 <= (now - datetime.fromisoformat(recon["completed_at"])).total_seconds() <= 120:
            reasons.append("PAPER_RECONCILIATION_SNAPSHOT_NOT_CURRENT")
        protection = connection.execute(
            "SELECT completed_at,outcome,unprotected_count,position_count,evaluated_count, "
            "exit_required_count FROM position_protection_cycles WHERE account_id=? "
            "ORDER BY completed_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if (
            protection is None
            or protection["outcome"] != "HEALTHY"
            or (protection["unprotected_count"] or protection["exit_required_count"])
        ):
            reasons.append("PAPER_POSITION_PROTECTION_UNVERIFIED")
        elif position_count is not None and (
            protection["position_count"] != position_count
            or protection["evaluated_count"] != position_count
        ):
            reasons.append("PAPER_PROTECTION_POSITION_COUNT_MISMATCH")
        elif (
            not 0
            <= (now - datetime.fromisoformat(protection["completed_at"])).total_seconds()
            <= 120
        ):
            reasons.append("PAPER_PROTECTION_SNAPSHOT_NOT_CURRENT")
        pending = connection.execute(
            "SELECT COUNT(*) FROM order_intents WHERE state IN ('SUBMITTING','SUBMISSION_UNKNOWN')"
        ).fetchone()[0]
        if pending:
            reasons.append("PAPER_UNKNOWN_SUBMISSION_REQUIRES_RECOVERY")
    return tuple(reasons)


def read_eligibility(
    store: ReadOnlyStore, binding: PaperModelBinding, identity: StrategyIdentity
) -> PaperEligibility:
    return assess_paper_entry(store.path, binding=binding, identity=identity)


def read_sizing_inputs(
    store: ReadOnlyStore,
    *,
    account_id: str,
    prediction: FiveMinutePrediction,
    config: StrategyConfig,
    policy: PaperExecutionPolicy,
    now: datetime,
) -> tuple[EntrySizingRequest | None, tuple[str, ...]]:
    """Use durable account/fill/stop observations; never bootstrap missing stops.

    This is an offline diagnostic, not a broker refresh. Unknown restrictions, quotes,
    reservations, calendar and veto measurements remain separately unverified.
    """
    now = _aware(now)
    reasons: list[str] = []
    with store.transaction() as session:
        repo = StorageRepository(session)
        account = repo.get_latest_account_snapshot(account_id)
        if account is None:
            return None, ("PAPER_ACCOUNT_SNAPSHOT_UNAVAILABLE",)
        if not 0 <= (now - account.captured_at).total_seconds() <= 120:
            reasons.append("PAPER_ACCOUNT_SNAPSHOT_NOT_CURRENT")
        if account.captured_at > now:
            return None, tuple(reasons)
        opening = _position_open_times([f for f in repo.list_fills() if f.occurred_at <= now])
        positions = []
        for position in account.positions:
            stop = None
            entered_at = opening.get(position.symbol)
            if entered_at is not None:
                for signal in reversed(repo.list_signals(symbol=position.symbol)):
                    if signal.occurred_at > now:
                        continue
                    state = signal.payload.get("position_state")
                    if (
                        isinstance(state, dict)
                        and state.get("entered_at") == entered_at.isoformat()
                    ):
                        stop = Decimal(str(state["active_stop"]))
                        break
            positions.append(PositionRiskInput(position=position, active_stop=stop))
        history = [
            r for r in repo.list_equity_snapshots(account_id=account_id) if r.captured_at <= now
        ]
        if config.drawdown_lookback_sessions > 0:
            history = history[-config.drawdown_lookback_sessions :]
        if not history:
            return None, (*reasons, "PAPER_EQUITY_HISTORY_UNAVAILABLE")
        high_water = max(r.equity for r in history)
        # Pending BUYs cannot be represented as zero reservations. Refuse instead.
        with closing(store.connect()) as connection:
            pending = connection.execute(
                "SELECT COUNT(*) FROM broker_orders WHERE side='BUY' "
                "AND UPPER(status) NOT IN ('FILLED','CANCELED','EXPIRED','REJECTED','REPLACED')"
            ).fetchone()[0]
            if pending or any(i.side.value == "BUY" for i in repo.list_unresolved_order_intents()):
                reasons.append("PAPER_PENDING_BUY_RESERVATIONS_UNMEASURED")
        portfolio = build_portfolio_risk_state(
            account.snapshot_id,
            account.account,
            prediction.features.symbol,
            positions,
            (),
            buying_power_reserve_bps=policy.buying_power_reserve_bps,
        )
        reference = Decimal(str(prediction.features.reference_close))
        return EntrySizingRequest(
            symbol=prediction.features.symbol,
            reference_price=reference,
            stop_distance=reference * Decimal(policy.stop_loss_bps) / 10000,
            portfolio=portfolio,
            drawdown=calculate_drawdown(account.account.equity, high_water, config),
        ), tuple(reasons)
