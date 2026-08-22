"""Earning trust in stages, with the last step reserved for a human (#16).

There is no `LIVE` stage in this enum. `LIVE_REVIEW_ELIGIBLE` is terminal, and the transition
table has no entry leading anywhere past it. That is how "no research agent can transition a
strategy into live trading" is implemented: not by a permission check that could be misconfigured
but by the absence of a destination.

**`RESEARCH_SURVIVOR` is the gate that had to be strengthened.** Passing a pre-registered test is
not enough, because cycle 11 produced two candidates that did exactly that and were both wrong:

* Vol targeting as alpha -- Sharpe 0.92 to 1.09, winning on all 12 parameter sets, break-even
  cost 113x the real spread. It died on a Jobson-Korkie statistic correcting for both series
  being the same asset (z=1.01) and on cross-asset generality (6 of 12, a coin flip).
* The overnight effect -- IWM t=3.47, GLD t=3.82, clearing the bar. It died when the t-test was
  correctly paired: SPY 2.74 to 0.63, and 1 of 18 assets clearing.

Both were pre-registered. Both passed their stated tests. The flaw was in the statistic, not the
protocol, so robustness and regime diagnostics would not have caught either. Survivorship here
therefore requires the #7 deterministic checks *and* that the test used matches the dependence
structure of the data, which is checked rather than asserted.

**Paper qualification counts authentic forward observations and nothing else.** A backtest,
a literature claim, an exploratory worker result and an underpowered outcome all increment the
counter by zero. The account currently holds a handful of trading days against a thirty-day
window; a narrative treating seventeen research cycles as progress toward deployment is confusing
backtest volume with forward evidence, and the counter is what makes that mechanically
impossible.

**And the observations are read, not accepted.** `ForwardObservation` refuses a non-paper role,
which stops a backtest being counted, but for a while it did not stop a caller assembling thirty
perfectly well-formed observations of days nobody traded. `count_forward_days` now takes
`LedgerObservations`, which only `observe_forward_days` can produce, and it checks that at runtime
rather than only in the annotation -- a list satisfies every static reading of the old signature,
which was the whole attack. Three durable facts have to agree before a day counts: the deployment
record matches this exact configuration, the trading system marked the session qualified, and the
trades are reached through `order_intents`. Forward evidence is the one category here that cannot
be regenerated: a bad backtest can be re-run, a trading day that did not happen cannot be
un-invented.

**A material change restarts the window.** Not a warning -- the observations were of a different
strategy, so they are not observations of this one.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.research.builder import ExperimentOutcome, OutcomeVerdict
from quantbot.research.critic import hidden_beta
from quantbot.research.mandate import (
    EconomicObjective,
    MandateError,
    Objective,
    load_economic_objective,
)
from quantbot.research.manifest import VALID_FOR, ExecutionPath
from quantbot.research.memory import ResearchMemory
from quantbot.research.power import (
    Estimand,
    PowerAssessment,
    luck_threshold,
    quantize,
)
from quantbot.research.power import assess as assess_power
from quantbot.research.registry import DataRole, HypothesisRegistry
from quantbot.storage.repositories import PromotionEvent, PromotionRecord, StorageRepository

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Stage(StrEnum):
    CANDIDATE = "CANDIDATE"
    REGISTERED = "REGISTERED"
    #: Passes its registered test *and* the deterministic integrity checks from #7.
    RESEARCH_SURVIVOR = "RESEARCH_SURVIVOR"
    #: Produces signals, submits no orders.
    SHADOW = "SHADOW"
    PAPER_OBSERVATION = "PAPER_OBSERVATION"
    PAPER_QUALIFIED = "PAPER_QUALIFIED"
    #: May be shown to the operator. Still not live, and there is nowhere further to go.
    LIVE_REVIEW_ELIGIBLE = "LIVE_REVIEW_ELIGIBLE"


ALLOWED_PROMOTIONS: Mapping[Stage, frozenset[Stage]] = MappingProxyType(
    {
        Stage.CANDIDATE: frozenset({Stage.REGISTERED}),
        Stage.REGISTERED: frozenset({Stage.RESEARCH_SURVIVOR, Stage.CANDIDATE}),
        Stage.RESEARCH_SURVIVOR: frozenset({Stage.SHADOW, Stage.REGISTERED}),
        Stage.SHADOW: frozenset({Stage.PAPER_OBSERVATION, Stage.RESEARCH_SURVIVOR}),
        Stage.PAPER_OBSERVATION: frozenset({Stage.PAPER_QUALIFIED, Stage.SHADOW}),
        Stage.PAPER_QUALIFIED: frozenset({Stage.LIVE_REVIEW_ELIGIBLE, Stage.PAPER_OBSERVATION}),
        # Terminal. There is no live stage in this enum, so no sequence of transitions reaches
        # one. Only the operator, outside this system, decides what happens next.
        Stage.LIVE_REVIEW_ELIGIBLE: frozenset(),
    }
)

#: Minimums for paper qualification. Both must be met, by authentic forward evidence.
MINIMUM_FORWARD_DAYS = 30
MINIMUM_FORWARD_TRADES = 30


class PromotionRefused(ValueError):
    """Raised when a strategy has not earned the stage it is being moved to."""

    def __init__(self, strategy_id: str, stage: Stage, reason: str) -> None:
        self.strategy_id = strategy_id
        self.stage = stage
        self.reason = reason
        super().__init__(f"{strategy_id} cannot reach {stage.value}: {reason}")


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ForwardObservation(FrozenModel):
    """One genuine trading day on the paper account. Not reconstructible from history."""

    strategy_id: Text
    trading_date: date
    trades: int = Field(ge=0)
    #: Must be FORWARD_PAPER. Anything else is not forward evidence, by definition.
    role: DataRole
    #: Identity of the code and config that traded that day. A change invalidates the window.
    strategy_version: Text
    configuration_hash: Text

    @model_validator(mode="after")
    def validate_observation(self) -> Self:
        if self.role is not DataRole.FORWARD_PAPER:
            raise ValueError(
                f"a forward observation comes from the paper account, not from "
                f"{self.role.value}; a backtest cannot increment this counter"
            )
        return self


#: Held by `LedgerObservations` so the class cannot be constructed outside this module. The same
#: device as `ProtectedAccess` in `runner.py`, for the same reason: a rule that forward evidence
#: must come from the ledger is a rule, and a type that only the ledger reader can produce is a
#: mechanism. `promote()` stays a pure function -- it takes data, not a session -- and still
#: cannot be handed observations somebody typed.
_LEDGER_TOKEN = object()


class LedgerObservations:
    """Forward observations read out of the durable trading ledger, and nothing else.

    `ForwardObservation` already refuses a non-`FORWARD_PAPER` role, which stops a backtest being
    counted. It does not stop a caller constructing one by hand, and forward evidence is the only
    category in this project that cannot be regenerated: a bad backtest can be re-run, a trading
    day that did not happen cannot be un-invented. CLAUDE.md forbids fabricating forward
    observations, fills and trading days; this makes it unavailable rather than forbidden.
    """

    __slots__ = ("_observations",)

    def __init__(self, observations: Sequence[ForwardObservation], *, token: object) -> None:
        if token is not _LEDGER_TOKEN:
            raise TypeError(
                "LedgerObservations comes from observe_forward_days() reading the durable "
                "ledger; forward evidence cannot be constructed, only observed"
            )
        self._observations = tuple(observations)

    def __iter__(self) -> Iterator[ForwardObservation]:
        return iter(self._observations)

    def __len__(self) -> int:
        return len(self._observations)


def observe_forward_days(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
    configuration_hash: str,
) -> LedgerObservations:
    """Derive forward observations for one strategy identity from the durable ledger.

    Three ledger facts have to agree before a day counts:

    1. `strategy_deployments` records this `(strategy_id, version)` with **this**
       `configuration_hash`. A deployment under a different configuration is a different
       strategy, and its days belong to that one.
    2. `qualification_days` marks the date qualified. That row is written by the trading
       system's own daily check, so a day the system did not consider a real session cannot
       become one here.
    3. Fills are counted through `order_intents`, so an order no strategy submitted has no route
       into the trade count.

    Returns empty rather than raising when the identity has never deployed. Empty fails closed --
    `promote()` refuses with zero days -- whereas raising would tempt a caller into an
    ``except: pass`` that silently skipped the check. A freshly changed identity legitimately has
    no deployment yet, which is exactly the case the material-change rule wants to read as zero.
    """
    repository = StorageRepository(session)
    deployment = repository.get_strategy_deployment(strategy_id, strategy_version)
    if deployment is None or deployment.configuration_hash != configuration_hash:
        return LedgerObservations((), token=_LEDGER_TOKEN)

    trades_by_date = repository.fills_by_signal_date(strategy_id)
    observations = [
        ForwardObservation(
            strategy_id=strategy_id,
            trading_date=day.trading_date,
            trades=trades_by_date.get(day.trading_date, 0),
            role=DataRole.FORWARD_PAPER,
            strategy_version=strategy_version,
            configuration_hash=configuration_hash,
        )
        for day in repository.list_qualification_days(strategy_id, qualified=True)
    ]
    return LedgerObservations(observations, token=_LEDGER_TOKEN)


def count_forward_days(
    observations: LedgerObservations, *, strategy_version: str, configuration_hash: str
) -> tuple[int, int]:
    """Days and trades observed for exactly this strategy identity.

    Observations of a different version or configuration are excluded rather than counted with
    a caveat: they were observations of a different strategy, so they are not observations of
    this one. That is the material-change rule, applied by filtering rather than by trusting a
    caller to reset a counter.

    The `LedgerObservations` check is at runtime and not only in the annotation, because an
    annotation is not a control: a plain list of hand-built `ForwardObservation`s satisfies every
    static reading of this signature and would count perfectly well. That is the entire attack.
    """
    if not isinstance(observations, LedgerObservations):
        raise TypeError(
            "forward days are counted from LedgerObservations returned by observe_forward_days();"
            " a sequence assembled by a caller is not forward evidence, whatever it contains"
        )
    matching = [
        observation
        for observation in observations
        if observation.strategy_version == strategy_version
        and observation.configuration_hash == configuration_hash
    ]
    days = len({observation.trading_date for observation in matching})
    return days, sum(observation.trades for observation in matching)


def survivor_objections(outcome: ExperimentOutcome) -> tuple[str, ...]:
    """Why a favourable result is not yet a survivor.

    Passing a pre-registered test is not enough. Cycle 11 produced two candidates that passed
    theirs and were both wrong, because the flaw was in the statistic rather than the protocol.
    """
    objections: list[str] = []
    if outcome.verdict is not OutcomeVerdict.SURVIVED:
        objections.append(f"the experiment verdict is {outcome.verdict.value}")
    if outcome.probes_failed:
        objections.append("adversarial probes objected: " + ", ".join(outcome.probes_failed))
    missing = [probe for probe in outcome.plan.probes if probe not in outcome.probes_run]
    if missing:
        objections.append("adversarial probes did not run: " + ", ".join(missing))
    if outcome.plan.execution_path is not ExecutionPath.PRODUCTION_ENGINE:
        objections.append(
            "the result came from "
            f"{outcome.plan.execution_path.value}, so it is a standalone model rather than a "
            "measurement of this system"
        )

    plan = outcome.plan.statistics
    if plan.comparison not in VALID_FOR[plan.test]:
        objections.append(
            f"{plan.test.value} is not valid for {plan.comparison} data; the flaw would be in "
            "the statistic rather than the protocol"
        )
    if outcome.test_statistic is None:
        objections.append("no test statistic was reported")
    elif abs(outcome.test_statistic) < plan.luck_threshold_z:
        objections.append(
            f"|{outcome.test_statistic}| does not clear the {plan.luck_threshold_z} bar frozen "
            f"for {plan.cumulative_trials} cumulative trials"
        )
    return tuple(objections)


class PromotionState(FrozenModel):
    """Where a strategy has got to, and why it last moved."""

    strategy_id: Text
    stage: Stage
    strategy_version: Text
    configuration_hash: Text
    reason: Text
    actor: Text
    updated_at: datetime

    @property
    def eligible_for_human_review(self) -> bool:
        return self.stage is Stage.LIVE_REVIEW_ELIGIBLE


class DeploymentRole(StrEnum):
    """What a paper deployment is *for*, derived rather than declared (#53).

    Keeping the refuted momentum rotation trading is useful and legitimate: it exercises the
    daemon, produces real fills, and is where slippage, reconciliation and recovery evidence
    comes from. What it must not do is convert that operational usefulness into progress toward
    a label about edge, and the count-based gate plus a state machine that runs beside the
    broker made exactly that path available -- refuted mechanism, plus real days, plus thirty
    trades, equals something that reads like qualification.

    Derived from durable state rather than stored as a column nobody updates. A deployment is an
    edge candidate exactly while a frozen registration stands behind it and nothing has refuted
    the claim; the moment either stops being true the answer changes on its own, which a flag
    set at deployment time would not.
    """

    #: Runs paper orders to exercise the machinery. Accrues operational evidence and no other kind.
    OPERATIONAL_BASELINE = "OPERATIONAL_BASELINE"
    #: A live, non-refuted registered claim is accumulating forward evidence against it.
    EDGE_CANDIDATE = "EDGE_CANDIDATE"


class ForwardVerdict(StrEnum):
    """What the forward account actually says about the claim (#47).

    `PAPER_QUALIFIED` used to be reachable on 30 authentic days and 30 trades alone. Those are
    *authenticity* facts: they establish that real sessions happened and that nobody invented
    them. They establish nothing about whether the strategy made money, and a strategy can lose
    on every one of those 30 trades and still satisfy the count. The stage name was a stronger
    claim than the mechanism behind it, and a dashboard, a later agent or an operator reading
    `PAPER_QUALIFIED` will read it as "the paper evidence supports this".
    """

    #: Below the authenticity floor. Not a result; the window is still open.
    FORWARD_OBSERVING = "FORWARD_OBSERVING"
    #: Enough days happened and the sample cannot resolve the frozen effect either way.
    #: Emphatically not a refutation, for the reason `UNDERPOWERED` is never `REFUTED` here.
    FORWARD_UNDERPOWERED = "FORWARD_UNDERPOWERED"
    #: The point estimate is against the claim. Still not a refutation on its own.
    FORWARD_NEGATIVE = "FORWARD_NEGATIVE"
    #: It ran, it is powered, and the number cannot be read as support.
    FORWARD_INCONCLUSIVE = "FORWARD_INCONCLUSIVE"
    #: The only verdict that reaches `PAPER_QUALIFIED`.
    FORWARD_EDGE_SUPPORTED = "FORWARD_EDGE_SUPPORTED"


#: A regression needs three points and a Sharpe needs a dispersion to divide by. Below this
#: there is no statistic at all, which is a different state from an unfavourable one.
MINIMUM_PAIRED_SESSIONS = 3

#: Sessions a year, when no frozen claim declares its own sampling frequency.
DEFAULT_SESSIONS_PER_YEAR = 252


class ForwardStatistics(FrozenModel):
    """The forward account measured against its benchmark, session by session.

    Every field is per **session**, never per fill. Thirty fills across three sessions are three
    observations of the market and one or two of the strategy's judgement; counting them as
    thirty is the manufacture of sample size that `DependenceAssumptions` exists to charge for
    elsewhere, and this is the gate most exposed to it because the trade count sits right there
    in the qualification rule.
    """

    benchmark_symbol: Text
    #: Consecutive session pairs where both an account equity mark and a benchmark close exist.
    paired_sessions: int = Field(ge=MINIMUM_PAIRED_SESSIONS)
    #: Annualised information ratio of the excess return series -- the benchmark-relative form,
    #: because a strategy earning 4% while its benchmark earns 8% has positive P&L and negative
    #: value, and absolute P&L cannot tell those apart.
    excess_sharpe: Decimal
    #: t on the mean excess return, compared against the frozen luck bar rather than against 1.96.
    excess_t: Decimal
    #: Annualised excess return in basis points. The mandate's "meaningfully better" threshold
    #: is written in these units, because a Sharpe cannot answer "is this worth the operational
    #: risk of running an autonomous system" and a number of basis points a year can.
    excess_bps_per_year: Decimal
    #: Cycle 15 in one number: the deployed rotation is beta 0.71 with alpha t=0.05, and SPY held
    #: at 0.71x reproduces it with no trading at all. A positive excess that is entirely beta is
    #: not an edge, so beta-adjusted alpha has to clear the bar as well as the raw excess.
    beta: Decimal
    alpha_bps_per_session: Decimal
    alpha_t: Decimal
    max_drawdown_bps: Decimal


_FORWARD_TOKEN = object()


@dataclass(frozen=True, slots=True)
class ForwardEvidence:
    """Forward economic evidence, derived from the ledger and unconstructible by a caller.

    The same device as `LedgerObservations` above and `MeasuredReadiness` in `operations`, for
    the same reason. `docs/research-architecture.md` records that every gate defect found in
    this project so far had one shape -- *a gate reads its input from the thing it is supposed
    to constrain* -- and an assessment a caller can assemble is a claim about the evidence
    rather than the evidence.

    `unassessed` is **blocking**, not a diagnostic. A check that did not run is not a check that
    passed; #7 and the kill switch both carry the same field for the same reason.
    """

    strategy_id: str
    strategy_version: str
    configuration_hash: str
    assessed_at: datetime
    #: Which frozen objective this was judged against. A verdict that cannot name its mandate is
    #: a verdict whose success criterion could have been chosen after the fact.
    objective_identity: str
    role: DeploymentRole
    forward_days: int
    forward_trades: int
    luck_threshold_z: Decimal
    statistics: ForwardStatistics | None
    power: PowerAssessment | None
    blocking: tuple[str, ...]
    unassessed: tuple[str, ...]
    verdict: ForwardVerdict

    def __init__(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        configuration_hash: str,
        assessed_at: datetime,
        objective_identity: str,
        role: DeploymentRole,
        forward_days: int,
        forward_trades: int,
        luck_threshold_z: Decimal,
        statistics: ForwardStatistics | None,
        power: PowerAssessment | None,
        blocking: Sequence[str],
        unassessed: Sequence[str],
        verdict: ForwardVerdict,
        token: object,
    ) -> None:
        if token is not _FORWARD_TOKEN:
            raise TypeError(
                "ForwardEvidence comes from assess_forward_evidence() reading the durable "
                "ledger; forward economic evidence cannot be constructed, only measured"
            )
        object.__setattr__(self, "strategy_id", strategy_id)
        object.__setattr__(self, "strategy_version", strategy_version)
        object.__setattr__(self, "configuration_hash", configuration_hash)
        object.__setattr__(self, "assessed_at", assessed_at)
        object.__setattr__(self, "objective_identity", objective_identity)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "forward_days", forward_days)
        object.__setattr__(self, "forward_trades", forward_trades)
        object.__setattr__(self, "luck_threshold_z", luck_threshold_z)
        object.__setattr__(self, "statistics", statistics)
        object.__setattr__(self, "power", power)
        object.__setattr__(self, "blocking", tuple(blocking))
        object.__setattr__(self, "unassessed", tuple(unassessed))
        object.__setattr__(self, "verdict", verdict)

    @property
    def supported(self) -> bool:
        return self.verdict is ForwardVerdict.FORWARD_EDGE_SUPPORTED

    def explain(self) -> str:
        """The verdict with every reason behind it, including the checks that did not run."""
        reasons = [*self.blocking, *(f"not assessed: {item}" for item in self.unassessed)]
        headline = f"{self.role.value}/{self.verdict.value}"
        if not reasons:
            return headline
        return f"{headline}: " + "; ".join(reasons)


def _session_equity(
    session: Session, *, account_id: str, dates: frozenset[date]
) -> dict[date, Decimal]:
    """The last equity mark of each qualified session, keyed by its **Eastern** session date.

    Snapshots are stamped in UTC and the account is marked after the close, so 20:05 in New York
    is 00:05 UTC the next day. Keying on the UTC date credits a session that had not happened --
    the off-by-one `docs/research-architecture.md` already records for fill attribution, and the
    reason trades there are attributed by `signal_date` rather than by timestamp.
    """
    marks: dict[date, Decimal] = {}
    for snapshot in StorageRepository(session).list_equity_snapshots(account_id=account_id):
        session_date = snapshot.captured_at.astimezone(XNYS_TIMEZONE).date()
        if session_date in dates:
            # Rows arrive ordered by captured_at, so the last mark of the session wins.
            marks[session_date] = snapshot.equity
    return marks


def _benchmark_closes(
    session: Session, *, symbol: str, dates: frozenset[date]
) -> dict[date, Decimal]:
    closes: dict[date, Decimal] = {}
    for bar in StorageRepository(session).list_bars(symbol=symbol):
        session_date = bar.timestamp.astimezone(XNYS_TIMEZONE).date()
        if session_date in dates:
            closes[session_date] = bar.close
    return closes


def _paired_returns(
    ordered_dates: Sequence[date],
    equity: Mapping[date, Decimal],
    benchmark: Mapping[date, Decimal],
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """Consecutive-session returns for both series, kept only where both sides exist.

    Paired by construction. An unpaired comparison of a strategy against the index it holds is
    the error that made cycle 11's overnight premium look significant at t=2.74 where the paired
    figure is 0.63 (`REFUTED.md` #17).
    """
    strategy_returns: list[float] = []
    benchmark_returns: list[float] = []
    for previous, current in zip(ordered_dates, ordered_dates[1:], strict=False):
        equity_before = equity.get(previous)
        equity_after = equity.get(current)
        close_before = benchmark.get(previous)
        close_after = benchmark.get(current)
        if equity_before is None or equity_after is None:
            continue
        if close_before is None or close_after is None:
            continue
        if equity_before <= 0 or close_before <= 0:
            continue
        strategy_returns.append(float(equity_after / equity_before - 1))
        benchmark_returns.append(float(close_after / close_before - 1))
    return tuple(strategy_returns), tuple(benchmark_returns)


def _max_drawdown_bps(ordered_dates: Sequence[date], equity: Mapping[date, Decimal]) -> Decimal:
    marks = [equity[day] for day in ordered_dates if day in equity]
    if not marks:
        return Decimal("0")
    high_water = marks[0]
    worst = Decimal("0")
    for mark in marks:
        high_water = max(high_water, mark)
        if high_water > 0:
            worst = max(worst, (high_water - mark) / high_water)
    return quantize(worst * 10000)


def _forward_statistics(
    *,
    benchmark_symbol: str,
    strategy_returns: Sequence[float],
    benchmark_returns: Sequence[float],
    drawdown_bps: Decimal,
    sessions_per_year: int,
) -> ForwardStatistics | None:
    count = len(strategy_returns)
    if count < MINIMUM_PAIRED_SESSIONS:
        return None
    excess = [
        strategy - benchmark
        for strategy, benchmark in zip(strategy_returns, benchmark_returns, strict=True)
    ]
    mean_excess = statistics.fmean(excess)
    spread = statistics.stdev(excess)
    if spread == 0:
        # A constant excess has no sampling distribution to test against, and reporting it as an
        # infinite t is the single most flattering arithmetic available at this gate.
        return None
    report = hidden_beta(strategy_returns, benchmark_returns)
    return ForwardStatistics(
        benchmark_symbol=benchmark_symbol,
        paired_sessions=count,
        excess_sharpe=quantize(mean_excess / spread * math.sqrt(sessions_per_year)),
        excess_t=quantize(mean_excess / (spread / math.sqrt(count))),
        excess_bps_per_year=quantize(mean_excess * sessions_per_year * 10000),
        beta=quantize(report.beta),
        alpha_bps_per_session=quantize(report.alpha_per_period * 10000),
        alpha_t=quantize(report.alpha_t),
        max_drawdown_bps=drawdown_bps,
    )


def _forward_verdict(
    *,
    days: int,
    trades: int,
    power: PowerAssessment | None,
    report: ForwardStatistics | None,
    blocking: Sequence[str],
    unassessed: Sequence[str],
) -> ForwardVerdict:
    if days < MINIMUM_FORWARD_DAYS or trades < MINIMUM_FORWARD_TRADES:
        return ForwardVerdict.FORWARD_OBSERVING
    if power is not None and not power.cleared:
        # Consulted before the point estimate, deliberately. A window that could not have
        # resolved the claim says nothing about the claim, and reading power off the result is
        # the post-hoc power fallacy -- the same collapse of `UNDERPOWERED` into `REFUTED` that
        # research memory and the director's transition table both refuse structurally.
        return ForwardVerdict.FORWARD_UNDERPOWERED
    if report is not None and (report.excess_sharpe <= 0 or report.alpha_bps_per_session <= 0):
        return ForwardVerdict.FORWARD_NEGATIVE
    if blocking or unassessed:
        return ForwardVerdict.FORWARD_INCONCLUSIVE
    return ForwardVerdict.FORWARD_EDGE_SUPPORTED


def assess_forward_evidence(
    session: Session,
    *,
    strategy_id: str,
    strategy_version: str,
    configuration_hash: str,
    account_id: str,
    objective: EconomicObjective,
    assessed_at: datetime,
    hypothesis_id: str | None = None,
    hypothesis_version: int = 1,
) -> ForwardEvidence:
    """Read what the forward account says about the frozen claim, and refuse to flatter it.

    The 30-day / 30-trade minimum stays exactly where it was, as an **authenticity floor**. What
    changes is that clearing it is no longer sufficient: `PAPER_QUALIFIED` now additionally needs
    a measured, benchmark-relative, adequately powered, positive result on a claim that was
    frozen before the window opened.

    The order of precedence is the epistemic content of the function:

    1. below the floor -> `FORWARD_OBSERVING`. The window is open, not answered.
    2. the sample cannot resolve the frozen effect -> `FORWARD_UNDERPOWERED`, decided *before*
       the point estimate is consulted.
    3. the point estimate is against the claim -> `FORWARD_NEGATIVE`.
    4. anything else short of support -> `FORWARD_INCONCLUSIVE`.

    Two things this deliberately does **not** do, named rather than quietly omitted:

    * **It does not invent a success metric.** A deployment with no frozen `claim` can never
      reach `FORWARD_EDGE_SUPPORTED`, whatever its P&L. That is the rule against choosing
      between CAGR, Sharpe and drawdown after seeing the forward number, applied at the only
      gate that can currently break it -- and it is also what stops the deployed momentum
      rotation, whose mechanism is refuted as `REFUTED.md` #22, from accumulating toward a label
      that reads like edge qualification.
    * **It does not attribute the return to the registered mechanism beyond market beta.** Beta
      and alpha are measured, so "the excess was just index exposure" is caught; "the excess came
      from an exposure the hypothesis never named" is not. Recorded here rather than in a commit
      message, because a reader of a `FORWARD_EDGE_SUPPORTED` verdict needs to know its edges.

    Costs need no separate term. These are realised equity marks from filled paper orders, so
    the spread is already inside the numbers -- the one respect in which forward evidence is
    cheaper to judge honestly than a backtest.

    The benchmark and the drawdown limit come from the frozen `objective` rather than from
    arguments, and so does what "meaningfully better" means. Those three are precisely the knobs
    a caller could turn to make a candidate pass: a different benchmark, a looser drawdown, a
    smaller improvement threshold. Freezing them one level above the experiment is #54's whole
    point -- a system that can search definitions of success will find one.

    Neither the claim nor the luck bar is an argument. Both are read here: the effect
    specification comes from the frozen registration named by `hypothesis_id`, and the burden
    comes from the registry as it stands *now* rather than as it stood when the hypothesis was
    filed. A caller able to hand this function an `EffectSpecification` could declare a large
    expected effect, and a large expected effect needs a small sample -- which is the fail-open
    direction, and the exact shape `docs/research-architecture.md` catalogues five times.
    """
    registry = HypothesisRegistry(session)
    registration = (
        registry.get(hypothesis_id, hypothesis_version) if hypothesis_id is not None else None
    )
    claim = registration.draft.effect if registration is not None else None
    cumulative_trials = registry.cumulative_trials()

    observations = observe_forward_days(
        session,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        configuration_hash=configuration_hash,
    )
    days, trades = count_forward_days(
        observations,
        strategy_version=strategy_version,
        configuration_hash=configuration_hash,
    )
    ordered_dates = sorted({observation.trading_date for observation in observations})
    wanted = frozenset(ordered_dates)
    equity = _session_equity(session, account_id=account_id, dates=wanted)
    benchmark = _benchmark_closes(session, symbol=objective.benchmark_symbol, dates=wanted)
    strategy_returns, benchmark_returns = _paired_returns(ordered_dates, equity, benchmark)

    threshold = luck_threshold(cumulative_trials)
    blocking: list[str] = []
    unassessed: list[str] = []
    refuted = False

    if days < MINIMUM_FORWARD_DAYS:
        blocking.append(f"{days} of {MINIMUM_FORWARD_DAYS} authentic forward days")
    if trades < MINIMUM_FORWARD_TRADES:
        blocking.append(f"{trades} of {MINIMUM_FORWARD_TRADES} forward trades")

    report = _forward_statistics(
        benchmark_symbol=objective.benchmark_symbol,
        strategy_returns=strategy_returns,
        benchmark_returns=benchmark_returns,
        drawdown_bps=_max_drawdown_bps(ordered_dates, equity),
        sessions_per_year=(
            claim.dependence.observations_per_year
            if claim is not None
            else DEFAULT_SESSIONS_PER_YEAR
        ),
    )
    if report is None:
        blocking.append(
            f"{len(strategy_returns)} paired sessions against {objective.benchmark_symbol}, "
            f"below the {MINIMUM_PAIRED_SESSIONS} any statistic needs"
        )

    power: PowerAssessment | None = None
    if claim is None:
        blocking.append(
            "no frozen economic claim is attached to this deployment, so there is nothing for "
            "forward evidence to support; a success metric chosen after seeing the P&L is not "
            "a frozen estimand"
        )
    else:
        power = assess_power(
            hypothesis_id=hypothesis_id or strategy_id,
            version=hypothesis_version,
            stage="FORWARD_QUALIFICATION",
            specification=claim,
            observations_available=max(len(strategy_returns), 1),
            cumulative_trials=cumulative_trials,
            assessed_at=assessed_at,
        )
        refutations = ResearchMemory(session).refutations_of(hypothesis_id or "")
        refuted = bool(refutations)
        if refutations:
            blocking.append(
                "the claim behind this deployment is refuted by "
                + ", ".join(record.record_id for record in refutations)
                + "; authentic paper days do not rehabilitate a mechanism by existing, and a "
                "new question about it needs a new registration rather than a reinterpretation "
                "of a deployment already running"
            )
        if claim.estimand is not Estimand.SHARPE:
            unassessed.append(
                f"the frozen estimand is {claim.estimand.value}, which this gate cannot express "
                "as a forward excess Sharpe; the window was not judged against it"
            )
        elif report is not None and report.excess_sharpe < claim.minimum_practical:
            blocking.append(
                f"a forward excess Sharpe of {report.excess_sharpe} is below the "
                f"{claim.minimum_practical} the registration called the minimum worth acting on"
            )

    repository = StorageRepository(session)
    incidents = [
        incident_id
        for owner, incident_id, severity in repository.unresolved_incidents_with_strategy()
        if severity.upper() in NON_INFORMATIONAL and owner in (None, strategy_id)
    ]
    if incidents:
        blocking.append(
            f"{len(incidents)} unresolved integrity incident(s) stand over this evidence window"
        )

    if objective.objective is not Objective.BENCHMARK_RELATIVE_GROWTH:
        # The measurement below is a benchmark-relative one. Reporting it against a mandate
        # asking for something else would be answering a question nobody asked and calling it
        # agreement.
        unassessed.append(
            f"{objective.identity} maximises {objective.objective.value}, and this gate measures "
            "benchmark-relative growth; the window was not judged against the frozen objective"
        )
    if report is not None:
        if report.max_drawdown_bps > objective.max_drawdown_bps:
            blocking.append(
                f"forward drawdown of {report.max_drawdown_bps}bps breaches the "
                f"{objective.max_drawdown_bps}bps ceiling in {objective.identity}"
            )
        if report.excess_bps_per_year < objective.minimum_meaningful_improvement_bps:
            blocking.append(
                f"{report.excess_bps_per_year}bps a year of benchmark-relative return is below "
                f"the {objective.minimum_meaningful_improvement_bps}bps {objective.identity} "
                "calls meaningful; a difference too small to be worth the operational risk is "
                "not an edge whatever its t-statistic"
            )

    if report is not None:
        if abs(report.excess_t) < threshold:
            blocking.append(
                f"an excess-return t of {report.excess_t} does not clear the {threshold} bar "
                f"frozen for {cumulative_trials} cumulative trials"
            )
        if abs(report.alpha_t) < threshold:
            blocking.append(
                f"an alpha t of {report.alpha_t} against beta {report.beta} does not clear the "
                f"{threshold} bar; a positive excess that is index exposure is not an edge"
            )

    role = (
        DeploymentRole.EDGE_CANDIDATE
        if registration is not None and not refuted
        else DeploymentRole.OPERATIONAL_BASELINE
    )
    return ForwardEvidence(
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        configuration_hash=configuration_hash,
        assessed_at=assessed_at,
        objective_identity=objective.identity,
        role=role,
        forward_days=days,
        forward_trades=trades,
        luck_threshold_z=threshold,
        statistics=report,
        power=power,
        blocking=blocking,
        unassessed=unassessed,
        verdict=_forward_verdict(
            days=days,
            trades=trades,
            power=power,
            report=report,
            blocking=blocking,
            unassessed=unassessed,
        ),
        token=_FORWARD_TOKEN,
    )


def promote(
    state: PromotionState,
    target: Stage,
    *,
    actor: str,
    reason: str,
    now: datetime,
    outcome: ExperimentOutcome | None = None,
    observations: LedgerObservations | None = None,
    evidence: ForwardEvidence | None = None,
    objective: EconomicObjective | None = None,
    integrity_clear: bool = True,
) -> PromotionState:
    """Move a strategy up, refusing every stage it has not earned.

    There is no `target` this function accepts that results in live trading, because no such
    stage exists to name.

    `PAPER_QUALIFIED` needs two different things and they are not substitutes. The day and trade
    counts are an **authenticity** floor -- real sessions happened and nobody invented them --
    and `evidence` is the **economic** question those sessions were supposed to answer. Thirty
    losing trades satisfy the first completely.
    """
    if target not in ALLOWED_PROMOTIONS[state.stage]:
        raise PromotionRefused(
            state.strategy_id,
            target,
            f"{state.stage.value} does not lead to {target.value}",
        )
    if not integrity_clear:
        raise PromotionRefused(
            state.strategy_id, target, "an unresolved evidence-integrity violation stands"
        )

    if target is Stage.RESEARCH_SURVIVOR:
        if outcome is None:
            raise PromotionRefused(state.strategy_id, target, "no experiment outcome was supplied")
        objections = survivor_objections(outcome)
        if objections:
            raise PromotionRefused(state.strategy_id, target, "; ".join(objections))

    if target is Stage.PAPER_QUALIFIED:
        if observations is None:
            raise PromotionRefused(
                state.strategy_id,
                target,
                "no ledger-derived forward observations were supplied; call "
                "observe_forward_days() rather than assembling them",
            )
        days, trades = count_forward_days(
            observations,
            strategy_version=state.strategy_version,
            configuration_hash=state.configuration_hash,
        )
        if days < MINIMUM_FORWARD_DAYS or trades < MINIMUM_FORWARD_TRADES:
            raise PromotionRefused(
                state.strategy_id,
                target,
                f"{days} authentic forward days and {trades} trades against "
                f"{MINIMUM_FORWARD_DAYS} and {MINIMUM_FORWARD_TRADES} required",
            )
        if evidence is None:
            raise PromotionRefused(
                state.strategy_id,
                target,
                "no measured forward evidence was supplied; call assess_forward_evidence() "
                "rather than reading the day and trade counts as an economic result",
            )
        if not isinstance(evidence, ForwardEvidence):
            # The annotation is not the control. A duck-typed stand-in with `supported = True`
            # satisfies every static reading of this signature, and that is the attack -- the
            # same one `count_forward_days` refuses a plain list for.
            raise TypeError(
                "forward evidence comes from assess_forward_evidence(); an object assembled by "
                "a caller is not a measurement, whatever it reports"
            )
        if (evidence.strategy_version, evidence.configuration_hash) != (
            state.strategy_version,
            state.configuration_hash,
        ):
            raise PromotionRefused(
                state.strategy_id,
                target,
                f"the evidence measures {evidence.strategy_version}/"
                f"{evidence.configuration_hash} and this strategy is now "
                f"{state.strategy_version}/{state.configuration_hash}",
            )
        if not evidence.supported:
            raise PromotionRefused(state.strategy_id, target, evidence.explain())

    if target is Stage.LIVE_REVIEW_ELIGIBLE:
        # The last rung before a human looks. It is the one place the mandate's *ratification*
        # matters rather than merely its contents: everything below can be judged against a
        # transcribed objective, but putting a candidate in front of the operator as satisfying
        # their objective requires that they said it was theirs.
        if objective is None:
            raise PromotionRefused(
                state.strategy_id,
                target,
                "no economic objective was supplied; a candidate cannot be offered for human "
                "review without naming the mandate it satisfies",
            )
        objections = objective.live_review_objections()
        if objections:
            raise PromotionRefused(state.strategy_id, target, "; ".join(objections))

    return state.model_copy(
        update={"stage": target, "reason": reason, "actor": actor, "updated_at": now}
    )


def demote(
    state: PromotionState, target: Stage, *, actor: str, reason: str, now: datetime
) -> PromotionState:
    """Drop a strategy back, or pause it, on an integrity violation.

    Demotion is not gated: a system that can be slow to stop trusting something is worse than
    one that is occasionally too quick.
    """
    if target not in ALLOWED_PROMOTIONS[state.stage] and _rank(target) >= _rank(state.stage):
        raise PromotionRefused(
            state.strategy_id, target, f"{target.value} is not below {state.stage.value}"
        )
    return state.model_copy(
        update={"stage": target, "reason": reason, "actor": actor, "updated_at": now}
    )


def material_change(
    state: PromotionState, *, strategy_version: str, configuration_hash: str, now: datetime
) -> PromotionState:
    """Record a change of identity, which restarts every qualification window.

    Not a warning. The observations collected so far were of a different strategy, so they are
    not observations of this one, and `count_forward_days` will exclude them.
    """
    if (state.strategy_version, state.configuration_hash) == (
        strategy_version,
        configuration_hash,
    ):
        return state
    demoted = min(state.stage, Stage.RESEARCH_SURVIVOR, key=_rank)
    return state.model_copy(
        update={
            "stage": demoted,
            "strategy_version": strategy_version,
            "configuration_hash": configuration_hash,
            "reason": "material change; forward qualification restarted",
            "actor": "promotion-ladder",
            "updated_at": now,
        }
    )


_ORDER = tuple(Stage)


def _rank(stage: Stage) -> int:
    return _ORDER.index(stage)


def forward_progress(
    observations: LedgerObservations, *, strategy_version: str, configuration_hash: str
) -> Mapping[str, Decimal]:
    """How far qualification actually is, for the dashboard and for honesty."""
    days, trades = count_forward_days(
        observations, strategy_version=strategy_version, configuration_hash=configuration_hash
    )
    return {
        "forward_days": Decimal(days),
        "forward_trades": Decimal(trades),
        "days_required": Decimal(MINIMUM_FORWARD_DAYS),
        "trades_required": Decimal(MINIMUM_FORWARD_TRADES),
    }


__all__ = [
    "ALLOWED_PROMOTIONS",
    "MINIMUM_FORWARD_DAYS",
    "MINIMUM_FORWARD_TRADES",
    "MINIMUM_PAIRED_SESSIONS",
    "DeploymentRole",
    "ForwardEvidence",
    "ForwardObservation",
    "ForwardStatistics",
    "ForwardVerdict",
    "IntegritySweep",
    "LedgerObservations",
    "NON_INFORMATIONAL",
    "PromotionRefused",
    "PromotionState",
    "Stage",
    "assess_forward_evidence",
    "count_forward_days",
    "demote",
    "demote_on_integrity_incidents",
    "forward_progress",
    "material_change",
    "observe_forward_days",
    "promote",
    "survivor_objections",
]


#: Severities that are not merely informational. Keyed on severity rather than on an allowlist
#: of incident kinds, because `incidents.kind` is free text and an allowlist fails **open** on
#: every kind nobody remembered to add -- a new integrity check would ship, raise incidents, and
#: quietly demote nothing. Severity is the field every raiser already has to choose.
NON_INFORMATIONAL = frozenset({"WARNING", "ERROR", "CRITICAL", "FATAL"})

#: An integrity incident drops a strategy out of the forward track entirely rather than one rung.
#: "A system that can be slow to stop trusting something is worse than one that is occasionally
#: too quick" -- and a strategy whose ledger is in doubt should not be accumulating qualification
#: days while the doubt stands.
INTEGRITY_FLOOR = Stage.RESEARCH_SURVIVOR


@dataclass(frozen=True, slots=True)
class IntegritySweep:
    """What an integrity sweep did, and what it could not reach.

    `unattributed` is not a diagnostic afterthought. An incident raised outside a run has no
    strategy to demote, so this sweep cannot act on it -- and reporting only what it demoted
    would let an operator read a clean sweep as a clean system. The count belongs in the same
    return value as the action so the two cannot drift apart.
    """

    demoted: tuple[PromotionState, ...]
    unattributed: tuple[str, ...]

    @property
    def clean(self) -> bool:
        """True only when nothing was demoted **and** nothing was left unattributable."""
        return not self.demoted and not self.unattributed


def demote_on_integrity_incidents(
    session: Session,
    *,
    now: datetime,
    actor: str = "integrity-sweep",
) -> IntegritySweep:
    """Drop every strategy with an unresolved non-informational incident (#16).

    `demote()` has always existed and is deliberately ungated; nothing called it when an
    incident landed, so the ladder could report a strategy as PAPER_QUALIFIED while its own
    reconciliation was failing. That is the failure mode the ladder exists to prevent, and a
    rule nobody invokes is not a control.

    Demotion does **not** erase forward evidence. `observe_forward_days` reads the ledger, which
    is unchanged, so resolving the incident and re-promoting restores the days that really
    happened. An incident casts doubt on evidence; it does not make the sessions un-occur, and
    deleting them would be its own kind of fabrication.

    **Known limitation, recorded rather than silently decided:** days that fall *inside* an
    incident window still count once the incident is resolved. Arguing they should not is
    reasonable -- evidence gathered while the ledger was in doubt is weaker evidence -- but
    excluding them needs incident time ranges this schema does not carry, and guessing the
    overlap would be worse than counting honestly and saying so.
    """
    repository = StorageRepository(session)
    ladder = PromotionLedger(session)
    suspect: set[str] = set()
    unattributed: list[str] = []
    for strategy_id, incident_id, severity in repository.unresolved_incidents_with_strategy():
        if severity.upper() not in NON_INFORMATIONAL:
            continue
        if strategy_id is None:
            unattributed.append(incident_id)
        else:
            suspect.add(strategy_id)

    demoted: list[PromotionState] = []
    for record in repository.list_promotions():
        if record.strategy_id not in suspect:
            continue
        if _rank(Stage(record.stage)) <= _rank(INTEGRITY_FLOOR):
            # Already at or below the floor. Re-demoting would write an event recording a move
            # that did not happen, and the history is meant to hold moves.
            continue
        demoted.append(
            ladder.demote(
                record.strategy_id,
                INTEGRITY_FLOOR,
                actor=actor,
                reason="unresolved integrity incident",
                now=now,
            )
        )
    return IntegritySweep(demoted=tuple(demoted), unattributed=tuple(unattributed))


class PromotionLedger:
    """The durable ladder: load where a strategy is, apply the rules, record that it moved.

    `promote()` and `demote()` are pure and stay that way -- they take a state and return one.
    Until this existed nothing stored the answer, so a ladder position lived only as long as the
    process that computed it and a restart reset every strategy to whatever a caller constructed
    next (#16).

    The rules are not duplicated here. This loads, delegates, and persists; a second copy of the
    transition table would eventually disagree with the first, and the one in `ALLOWED_PROMOTIONS`
    is the one that has tests.
    """

    def __init__(self, session: Session) -> None:
        self._session = session
        self._repository = StorageRepository(session)

    def current(self, strategy_id: str) -> PromotionState | None:
        """Where this strategy has got to, or `None` if it has never entered the ladder."""
        record = self._repository.get_promotion(strategy_id)
        if record is None:
            return None
        return PromotionState(
            strategy_id=record.strategy_id,
            stage=Stage(record.stage),
            strategy_version=record.strategy_version,
            configuration_hash=record.configuration_hash,
            reason=record.reason,
            actor=record.actor,
            updated_at=record.updated_at,
        )

    def enter(self, state: PromotionState) -> PromotionState:
        """Record a strategy entering the ladder. Refuses to overwrite an existing position.

        Re-entering would silently discard everything the strategy had earned, and would do it
        through the same call that legitimately starts one -- so it is refused rather than
        treated as an upsert.
        """
        if self._repository.get_promotion(state.strategy_id) is not None:
            raise PromotionRefused(
                state.strategy_id,
                state.stage,
                "already on the ladder; use promote or demote rather than re-entering",
            )
        self._save(state, direction="PROMOTION")
        return state

    def promote(
        self,
        strategy_id: str,
        target: Stage,
        *,
        account_id: str | None = None,
        hypothesis_id: str | None = None,
        hypothesis_version: int = 1,
        objective_path: str | Path | None = None,
        **kwargs: object,
    ) -> PromotionState:
        """Move a strategy up and persist it, or refuse and persist nothing.

        A refusal writes no event. A promotion that did not happen is not a move, and recording
        the attempt would put refused transitions in the same history as earned ones.

        Forward observations and forward economics are both **read here, not accepted here**.
        Paper qualification is the one gate whose evidence cannot be regenerated if it turns out
        to be wrong, so the ladder goes to the ledger rather than believing a caller about how
        many days a strategy traded or how it did. Passing either is refused rather than
        ignored: silently discarding an argument a caller believed was doing something is how a
        control becomes decorative.

        What a caller *does* supply is where to look -- the account holding the money and the
        registration that froze the claim. Naming a source cannot flatter the answer; supplying
        one can. The benchmark, the drawdown ceiling and the threshold for "meaningfully better"
        come from the frozen economic objective on disk, because those are the three knobs that
        would make a candidate pass if a caller held them.

        `objective_path` exists for tests, which need a mandate that is not the operator's. A
        test walks `src/` and fails if anything in production passes it, the same arrangement
        `issue_measured_readiness` uses: the boundary is that exactly one path reaches it, and
        that is checked rather than assumed.
        """
        state = self._require(strategy_id)
        # Only once the move is legal. An illegal transition should say so, rather than
        # complaining about the inputs of a stage it was never going to reach.
        if target is Stage.LIVE_REVIEW_ELIGIBLE and target in ALLOWED_PROMOTIONS[state.stage]:
            if "objective" in kwargs:
                raise PromotionRefused(
                    strategy_id,
                    target,
                    "the economic objective is read from disk, not supplied; a caller holding "
                    "it holds the definition of success",
                )
            try:
                kwargs["objective"] = (
                    load_economic_objective()
                    if objective_path is None
                    else load_economic_objective(objective_path)
                )
            except MandateError as error:
                raise PromotionRefused(strategy_id, target, str(error)) from error
        if target is Stage.PAPER_QUALIFIED and target in ALLOWED_PROMOTIONS[state.stage]:
            for supplied in ("observations", "evidence"):
                if supplied in kwargs:
                    raise PromotionRefused(
                        strategy_id,
                        target,
                        f"forward {supplied} are read from the durable ledger, not supplied",
                    )
            if account_id is None:
                raise PromotionRefused(
                    strategy_id,
                    target,
                    "paper qualification needs an account to read equity from; absolute P&L "
                    "cannot tell +4% against +8% from an edge, and neither can no P&L",
                )
            try:
                objective = (
                    load_economic_objective()
                    if objective_path is None
                    else load_economic_objective(objective_path)
                )
            except MandateError as error:
                raise PromotionRefused(strategy_id, target, str(error)) from error
            kwargs["observations"] = observe_forward_days(
                self._session,
                strategy_id=strategy_id,
                strategy_version=state.strategy_version,
                configuration_hash=state.configuration_hash,
            )
            assessed_at = kwargs.get("now")
            if not isinstance(assessed_at, datetime):
                raise PromotionRefused(
                    strategy_id, target, "promotion needs `now`; the assessment is stamped with it"
                )
            kwargs["evidence"] = assess_forward_evidence(
                self._session,
                strategy_id=strategy_id,
                strategy_version=state.strategy_version,
                configuration_hash=state.configuration_hash,
                account_id=account_id,
                objective=objective,
                assessed_at=assessed_at,
                hypothesis_id=hypothesis_id,
                hypothesis_version=hypothesis_version,
            )
        moved = promote(state, target, **kwargs)  # type: ignore[arg-type]
        self._save(moved, direction="PROMOTION")
        return moved

    def demote(self, strategy_id: str, target: Stage, **kwargs: object) -> PromotionState:
        """Drop a strategy back and persist it."""
        moved = demote(self._require(strategy_id), target, **kwargs)  # type: ignore[arg-type]
        self._save(moved, direction="DEMOTION")
        return moved

    def history(self, strategy_id: str) -> list[PromotionEvent]:
        """Every move this strategy made, oldest first."""
        return self._repository.list_promotion_events(strategy_id)

    def _require(self, strategy_id: str) -> PromotionState:
        state = self.current(strategy_id)
        if state is None:
            raise PromotionRefused(
                strategy_id, Stage.CANDIDATE, "is not on the ladder, so it cannot move on it"
            )
        return state

    def _save(self, state: PromotionState, *, direction: str) -> None:
        self._repository.save_promotion(
            PromotionRecord(
                strategy_id=state.strategy_id,
                stage=state.stage.value,
                strategy_version=state.strategy_version,
                configuration_hash=state.configuration_hash,
                reason=state.reason,
                actor=state.actor,
                updated_at=state.updated_at,
            ),
            direction=direction,
        )
