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

from collections.abc import Iterator, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy.orm import Session

from quantbot.research.builder import ExperimentOutcome, OutcomeVerdict
from quantbot.research.manifest import VALID_FOR, ExecutionPath
from quantbot.research.registry import DataRole
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


def promote(
    state: PromotionState,
    target: Stage,
    *,
    actor: str,
    reason: str,
    now: datetime,
    outcome: ExperimentOutcome | None = None,
    observations: LedgerObservations | None = None,
    integrity_clear: bool = True,
) -> PromotionState:
    """Move a strategy up, refusing every stage it has not earned.

    There is no `target` this function accepts that results in live trading, because no such
    stage exists to name.
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
    "ForwardObservation",
    "LedgerObservations",
    "PromotionRefused",
    "PromotionState",
    "Stage",
    "count_forward_days",
    "demote",
    "forward_progress",
    "material_change",
    "observe_forward_days",
    "promote",
    "survivor_objections",
]


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

    def promote(self, strategy_id: str, target: Stage, **kwargs: object) -> PromotionState:
        """Move a strategy up and persist it, or refuse and persist nothing.

        A refusal writes no event. A promotion that did not happen is not a move, and recording
        the attempt would put refused transitions in the same history as earned ones.

        Forward observations are **read here, not accepted here**. Paper qualification is the one
        gate whose evidence cannot be regenerated if it turns out to be wrong, so the ladder goes
        to the ledger itself rather than believing a caller about how many days a strategy has
        traded. Passing them is refused rather than ignored: silently discarding an argument a
        caller believed was doing something is how a control becomes decorative.
        """
        state = self._require(strategy_id)
        if target is Stage.PAPER_QUALIFIED:
            if "observations" in kwargs:
                raise PromotionRefused(
                    strategy_id,
                    target,
                    "forward observations are read from the durable ledger, not supplied",
                )
            kwargs["observations"] = observe_forward_days(
                self._session,
                strategy_id=strategy_id,
                strategy_version=state.strategy_version,
                configuration_hash=state.configuration_hash,
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
