"""Portfolio-level hard vetoes: rules that reject rather than resize (#58).

Everything already in `risk/sizing.py` is a **cap**. A cap that binds reduces the quantity and
lets the order through smaller, which is right for per-trade risk and wrong for concentration:
downsizing a position that breaches a sector limit still adds sector exposure. So these are a
separate type from `SizingCapName`, deliberately, and the separation is the protection -- a
future edit cannot quietly turn a veto into a haircut without changing which enum it belongs to.

**Risk authority is asymmetric.** Nothing here consults a score, a forecast, a semantic agent or
a catalyst strength. A veto is a deterministic function of frozen policy values and measured
portfolio state, and there is no argument a candidate can make to it.

**Fail closed, and say which input was missing.** Every veto that cannot be evaluated rejects.
That is not caution for its own sake: an unmeasurable limit that allows is indistinguishable from
an absent one, and this project has already shipped that shape more than once. `unmeasured` names
the input rather than merely reporting a refusal, so an operator can fix the cause instead of
guessing.

**Versioned identity on every rejection, because #57 has to attribute one.** A rejection carrying
free text cannot tell a counterfactual study *which rule at which settings* refused a trade, so
the study cannot say whether that rule was worth having. Each veto reports its name, its policy
version and a hash of the exact parameters that were in force.

**What is deliberately not here.** Sector and correlation concentration are specified in #58 and
`EXPOSURE_CONCENTRATION` is implemented as of #59's first slice, and is **named for exposure
rather than sector on purpose**: every instrument in this universe is an ETF, and a sector-shaped
rule would rate SPY + QQQ + XLK + IWM + MDY as five uncorrelated bets when it is US equity beta
wearing five tickers. `risk/exposure.py` carries the classification and the argument.

`CORRELATION_CONCENTRATION` is still a name with no evaluator. It needs a frozen covariance --
lookback, source, as-of and method identity all pinned -- and a matrix reconstructed later is not
the object the veto had at decision time. It stays declared so the gap is visible in the enum
rather than inferred from silence, and `PortfolioVetoPolicy` refuses to be configured with it
enabled: an unimplemented veto that reads as enforced is worse than one that reads as missing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.domain import Position
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.risk.exposure import (
    ExposureClassification,
    ExposureState,
    Provenance,
    build_exposure_state,
)

BASIS_POINTS = Decimal("10000")

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class VetoName(StrEnum):
    """The hard vetoes, named so a rejection can be attributed to one (#57).

    `CORRELATION_CONCENTRATION` is declared and unimplemented. It is here rather than omitted so
    the gap is a visible member of the enum instead of something a reader has to notice is
    missing, and `PortfolioVetoPolicy` refuses to enable it.
    """

    NET_EXPOSURE = "NET_EXPOSURE"
    DAILY_LOSS = "DAILY_LOSS"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    LIQUIDITY = "LIQUIDITY"
    #: Concentration in a mandate-defined exposure group (#59). Named for exposure rather
    #: than sector because sector alone cannot express that SPY and XLK are the same bet.
    EXPOSURE_CONCENTRATION = "EXPOSURE_CONCENTRATION"
    #: Declared, unimplemented, blocked on a frozen covariance source (#59).
    CORRELATION_CONCENTRATION = "CORRELATION_CONCENTRATION"


#: Vetoes with no evaluator behind them. Enabling one is refused rather than ignored.
UNIMPLEMENTED: frozenset[VetoName] = frozenset({VetoName.CORRELATION_CONCENTRATION})


class VetoModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PortfolioVetoPolicy(VetoModel):
    """The frozen, versioned limits. A candidate cannot change these; only the operator can.

    `version` is bumped by whoever edits the values, and `identity` hashes them, so a
    counterfactual study can tell a rejection under one setting from a rejection under another.
    Two studies of "did the daily loss limit help" across a threshold change are two studies.
    """

    version: int = Field(default=1, ge=1)

    #: Net exposure ceiling, |long - short| as a fraction of equity in basis points. Gross is
    #: already capped in sizing; net is not, and a long-short book can sit inside a gross limit
    #: while carrying the directional risk the gross limit was meant to bound.
    max_net_exposure_bps: int = Field(default=10000, gt=0)

    #: Loss from the session's opening equity that halts new entries for the rest of the day.
    #: Keyed on the **Eastern** session, not UTC -- a 20:05 New York mark is the next UTC day,
    #: and a UTC-keyed limit would reset mid-session and halt on the previous day's losses. The
    #: same off-by-one already bit fill attribution and equity marks.
    max_daily_loss_bps: int = Field(default=500, gt=0)

    #: How old the newest market observation may be before entries stop. Stale data is not a
    #: connectivity problem at this layer; it is a risk one, because sizing against a price that
    #: no longer exists is worse than not trading.
    #:
    #: **The default is four days, not fifteen minutes, and the first version of this got it
    #: wrong.** This strategy trades daily bars once per session, so the newest observation is
    #: always the previous session's close -- roughly a day old at decision time, and longer
    #: across a weekend or a holiday. A 900-second ceiling encoded an intraday assumption into a
    #: daily-bar system and refused every entry. Four days clears a long weekend and still
    #: catches a feed that is genuinely behind.
    #:
    #: **Recorded limitation:** calendar seconds are a coarse proxy for "sessions behind". The
    #: precise rule needs the exchange calendar, which the risk layer does not currently take;
    #: an intraday strategy would need this retuned rather than inheriting a daily default.
    max_market_data_age_seconds: int = Field(default=345_600, gt=0)

    #: The order may not exceed this share of the symbol's average daily volume. A position that
    #: cannot be exited without moving the price is a position whose backtest never happened.
    max_participation_bps: int = Field(default=100, gt=0)
    #: Sessions of volume history required before the liquidity veto will pass anything. Below
    #: this the average is not an average, and an unmeasurable limit must not allow.
    minimum_volume_sessions: int = Field(default=20, gt=0)

    #: Ceiling on any one exposure group's committed value, in basis points of equity.
    #:
    #: **The default does not bind, and that is deliberate rather than an oversight.** This
    #: universe is mostly US equity: a roster of 10 from 23 can be near 100% `US_EQUITY` while
    #: being exactly what the strategy is designed to hold. A tight global threshold would refuse
    #: almost every roster, so the shipped value is a placeholder and the veto ships disabled.
    #:
    #: **Recorded limitation:** one threshold across all groups is a blunt instrument here. It is
    #: too loose to constrain the dominant group and would be too tight for the narrow ones
    #: (sectors, duration, credit) if set for the dominant one. Per-group ceilings are the likely
    #: next step; choosing them is an operator decision about risk appetite, not an agent's.
    max_group_exposure_bps: int = Field(default=10000, gt=0)

    #: Vetoes to enforce. Anything absent is not evaluated, which is a policy choice and is
    #: recorded in `identity` so a study can tell one policy from another.
    enabled: frozenset[VetoName] = frozenset(
        {VetoName.NET_EXPOSURE, VetoName.STALE_MARKET_DATA}
    )

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        unimplemented = sorted(name.value for name in self.enabled & UNIMPLEMENTED)
        if unimplemented:
            raise ValueError(
                f"{', '.join(unimplemented)} has no evaluator: it is blocked on a frozen "
                "point-in-time covariance source (#59), and a veto that reads as enforced while "
                "doing nothing is worse than one that reads as missing"
            )
        return self

    @property
    def identity(self) -> str:
        """Hash of the exact parameters in force, for #57's `rule_parameters_hash`."""
        payload = json.loads(self.model_dump_json())
        payload["enabled"] = sorted(payload["enabled"])
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"veto-policy-v{self.version}-{digest[:16]}"


class PortfolioVetoInputs(VetoModel):
    """Measured portfolio and market state. Every field is an observation, never an opinion.

    Nullable fields are the ones that can genuinely be unavailable, and every one of them causes
    the veto that needs it to reject. They are `None` rather than defaulted for the reason the
    kill switch refuses a defaulted readiness: a substituted value is indistinguishable from a
    measured one once it is inside the arithmetic.
    """

    symbol: str
    equity: FiniteDecimal = Field(gt=0)
    #: Signed market value across open positions: long positive, short negative.
    committed_net_exposure: FiniteDecimal
    #: Signed market value this order would add. `None` when no reference price could be
    #: measured, which **rejects**: substituting zero would let a real proposed entry contribute
    #: nothing to projected exposure while `NET_EXPOSURE` still reported itself as evaluated.
    #: That is the fail-open this module exists to refuse, and it shipped here first (#58).
    order_net_exposure: FiniteDecimal | None
    #: Shares this order would trade.
    order_quantity: NonNegativeDecimal

    #: The session being traded, in Eastern terms, and the equity it opened at.
    session_date: date
    session_opening_equity: FiniteDecimal | None = None

    #: When the newest market observation for this symbol was captured, and the clock to
    #: measure it against. Both passed in; nothing here reads `datetime.now()`.
    latest_market_data_at: datetime | None = None
    observed_at: datetime

    #: Average daily volume over `minimum_volume_sessions`, and how many sessions backed it.
    average_daily_volume: NonNegativeDecimal | None = None
    volume_sessions: int = Field(default=0, ge=0)

    #: Committed exposure per mandate-defined group, or `None` when no classification was
    #: supplied. `None` rejects: concentration measured against a table that is not there would
    #: report every group at zero, which is the most permissive possible answer.
    exposure: ExposureState | None = None

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("symbol must be uppercase without surrounding whitespace")
        for field in ("latest_market_data_at", "observed_at"):
            moment = getattr(self, field)
            if moment is not None and (moment.tzinfo is None or moment.utcoffset() is None):
                raise ValueError(f"{field} must be timezone-aware")
        return self


class VetoRejection(VetoModel):
    """One veto refusing, with everything #57 needs to attribute an outcome to it."""

    veto: VetoName
    policy_version: int
    rule_parameters_hash: str
    reason: str
    #: Set when the veto refused because it could not measure something, rather than because a
    #: limit was breached. Both reject; only one is fixable by changing a limit.
    unmeasured: str | None = None


class PortfolioVetoDecision(VetoModel):
    """Allow or reject, and never a size. A veto has no opinion about quantity."""

    symbol: str
    policy_version: int
    rule_parameters_hash: str
    evaluated: tuple[VetoName, ...]
    rejections: tuple[VetoRejection, ...]

    @property
    def allowed(self) -> bool:
        return not self.rejections

    @property
    def reasons(self) -> tuple[str, ...]:
        return tuple(f"{item.veto.value}: {item.reason}" for item in self.rejections)

    def explain(self) -> str:
        if self.allowed:
            return f"{self.symbol}: {len(self.evaluated)} veto(es) evaluated, none refused"
        return f"{self.symbol}: " + "; ".join(self.reasons)


def _reject(
    veto: VetoName, policy: PortfolioVetoPolicy, reason: str, unmeasured: str | None = None
) -> VetoRejection:
    return VetoRejection(
        veto=veto,
        policy_version=policy.version,
        rule_parameters_hash=policy.identity,
        reason=reason,
        unmeasured=unmeasured,
    )


def _net_exposure(
    inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy
) -> VetoRejection | None:
    if inputs.order_net_exposure is None:
        return _reject(
            VetoName.NET_EXPOSURE,
            policy,
            f"no reference price could be measured for {inputs.symbol}, so the exposure this "
            "order would add is unknown; a missing price is not a zero one",
            unmeasured="REFERENCE_PRICE_UNAVAILABLE",
        )
    projected = abs(inputs.committed_net_exposure + inputs.order_net_exposure)
    ceiling = inputs.equity * Decimal(policy.max_net_exposure_bps) / BASIS_POINTS
    if projected > ceiling:
        return _reject(
            VetoName.NET_EXPOSURE,
            policy,
            f"projected net exposure {projected} exceeds the {ceiling} ceiling "
            f"({policy.max_net_exposure_bps}bps of {inputs.equity})",
        )
    return None


def _daily_loss(inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy) -> VetoRejection | None:
    if inputs.session_opening_equity is None:
        return _reject(
            VetoName.DAILY_LOSS,
            policy,
            "the session's opening equity is unknown, so today's loss cannot be measured",
            unmeasured="session_opening_equity",
        )
    if inputs.session_opening_equity <= 0:
        return _reject(
            VetoName.DAILY_LOSS,
            policy,
            f"the session opened at {inputs.session_opening_equity}, which is not a base to "
            "measure a loss against",
            unmeasured="session_opening_equity",
        )
    loss = inputs.session_opening_equity - inputs.equity
    limit = inputs.session_opening_equity * Decimal(policy.max_daily_loss_bps) / BASIS_POINTS
    if loss > limit:
        return _reject(
            VetoName.DAILY_LOSS,
            policy,
            f"down {loss} on the {inputs.session_date.isoformat()} session against a {limit} "
            f"limit ({policy.max_daily_loss_bps}bps); entries stop until the next session",
        )
    return None


def _stale_market_data(
    inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy
) -> VetoRejection | None:
    if inputs.latest_market_data_at is None:
        return _reject(
            VetoName.STALE_MARKET_DATA,
            policy,
            f"no market observation is recorded for {inputs.symbol}, so its age is unknown",
            unmeasured="latest_market_data_at",
        )
    age = inputs.observed_at - inputs.latest_market_data_at
    ceiling = timedelta(seconds=policy.max_market_data_age_seconds)
    if age > ceiling:
        return _reject(
            VetoName.STALE_MARKET_DATA,
            policy,
            f"the newest {inputs.symbol} observation is {int(age.total_seconds())}s old against "
            f"a {policy.max_market_data_age_seconds}s ceiling; sizing against a price that no "
            "longer exists is worse than not trading",
        )
    if age < timedelta(0):
        # A future-stamped observation is not fresh data, it is a clock or ingestion defect, and
        # treating it as the freshest possible reading is the fail-open direction.
        return _reject(
            VetoName.STALE_MARKET_DATA,
            policy,
            f"the newest {inputs.symbol} observation is stamped after the decision clock, which "
            "is a data defect rather than fresh data",
            unmeasured="latest_market_data_at",
        )
    return None


def _liquidity(inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy) -> VetoRejection | None:
    if inputs.average_daily_volume is None:
        return _reject(
            VetoName.LIQUIDITY,
            policy,
            f"no volume history for {inputs.symbol}, so participation cannot be measured",
            unmeasured="average_daily_volume",
        )
    if inputs.volume_sessions < policy.minimum_volume_sessions:
        return _reject(
            VetoName.LIQUIDITY,
            policy,
            f"{inputs.volume_sessions} sessions of volume against the "
            f"{policy.minimum_volume_sessions} this average needs; too few to be an average",
            unmeasured="volume_sessions",
        )
    if inputs.average_daily_volume <= 0:
        return _reject(
            VetoName.LIQUIDITY,
            policy,
            f"{inputs.symbol} averages no volume, so any order is unbounded participation",
        )
    participation = inputs.order_quantity / inputs.average_daily_volume * BASIS_POINTS
    if participation > policy.max_participation_bps:
        return _reject(
            VetoName.LIQUIDITY,
            policy,
            f"{inputs.order_quantity} shares is {participation.quantize(Decimal('0.01'))}bps of "
            f"{inputs.symbol}'s average daily volume against a {policy.max_participation_bps}bps "
            "ceiling; a position that cannot be exited without moving the price is one whose "
            "backtest never happened",
        )
    return None


def _exposure_concentration(
    inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy
) -> VetoRejection | None:
    """Refuse an order that would push one mandate-defined exposure past its ceiling.

    Three ways this rejects without a limit being breached, all of them the same rule: an
    exposure that cannot be measured is not an exposure of zero.
    """
    if inputs.exposure is None:
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"no exposure classification was supplied for {inputs.symbol}, so committed "
            "concentration is unknown; an absent table would otherwise report every group at "
            "zero, which is the most permissive answer available",
            unmeasured="exposure_classification",
        )
    state = inputs.exposure
    if state.unclassified:
        # A held position nobody could classify makes every group total an understatement, and an
        # understated numerator is the permissive direction. This is also what refuses a decision
        # dated before the classification was verified, which is the anti-look-ahead rule.
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"{', '.join(state.unclassified)} could not be classified as of "
            f"{inputs.session_date.isoformat()}, so every group total understates what is "
            "actually committed",
            unmeasured="UNCLASSIFIED_SYMBOL",
        )
    if state.order_groups is None:
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"{inputs.symbol} carries no known exposure, so the concentration this order would "
            "add cannot be attributed to any group",
            unmeasured="UNCLASSIFIED_SYMBOL",
        )
    if inputs.order_net_exposure is None:
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"no reference price could be measured for {inputs.symbol}, so the value this order "
            "would add to its exposure groups is unknown",
            unmeasured="REFERENCE_PRICE_UNAVAILABLE",
        )

    # Absolute, not signed. Netting a long against a short across two instruments that share an
    # exposure would report the concentration away.
    added = abs(inputs.order_net_exposure)
    ceiling = inputs.equity * Decimal(policy.max_group_exposure_bps) / BASIS_POINTS
    breached = [
        (group, projected)
        for group in sorted(state.order_groups)
        if (projected := state.committed_by_group.get(group, Decimal("0")) + added) > ceiling
    ]
    if breached:
        # Each breach names how that exposure was assigned. A refusal driven by a prospectus and
        # one driven by a declared stress proxy are different claims, and #57 cannot pool them
        # into one dataset if the rejection does not say which it was.
        detail = "; ".join(
            f"{group.value} ({state.order_provenance.get(group, Provenance.MANDATE).value}) "
            f"would reach {value}"
            for group, value in breached
        )
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"{detail} against a {ceiling} ceiling "
            f"({policy.max_group_exposure_bps}bps of {inputs.equity})",
        )
    return None


#: Name to evaluator. A veto with no entry here cannot be enabled, which is checked by
#: `PortfolioVetoPolicy` rather than discovered when it silently allows everything.
_EVALUATORS = {
    VetoName.NET_EXPOSURE: _net_exposure,
    VetoName.DAILY_LOSS: _daily_loss,
    VetoName.STALE_MARKET_DATA: _stale_market_data,
    VetoName.LIQUIDITY: _liquidity,
    VetoName.EXPOSURE_CONCENTRATION: _exposure_concentration,
}


def evaluate_portfolio_vetoes(
    inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy
) -> PortfolioVetoDecision:
    """Every enabled veto, all of them, and the full list of what refused.

    **Not short-circuited.** Stopping at the first refusal would make the second reason invisible,
    and #57 evaluates rejection *reasons* -- a study of whether the liquidity veto adds value
    cannot run if liquidity never got to speak because net exposure refused first. `primary` is
    the first in enum order; the rest travel with it.
    """
    rejections: list[VetoRejection] = []
    evaluated: list[VetoName] = []
    for name in VetoName:
        if name not in policy.enabled:
            continue
        evaluator = _EVALUATORS[name]
        evaluated.append(name)
        rejection = evaluator(inputs, policy)
        if rejection is not None:
            rejections.append(rejection)
    return PortfolioVetoDecision(
        symbol=inputs.symbol,
        policy_version=policy.version,
        rule_parameters_hash=policy.identity,
        evaluated=tuple(evaluated),
        rejections=tuple(rejections),
    )


def build_veto_inputs(
    *,
    symbol: str,
    equity: Decimal,
    positions: Sequence[Position],
    order_quantity: Decimal,
    order_price: Decimal | None,
    equity_marks: Sequence[tuple[datetime, Decimal]],
    symbol_bars: Sequence[tuple[datetime, Decimal]],
    observed_at: datetime,
    volume_sessions: int,
    classification: ExposureClassification | None = None,
) -> PortfolioVetoInputs:
    """Assemble measured state from primitives the caller already holds.

    Pure, and takes rows rather than a session, for the same reason `build_portfolio_risk_state`
    does: a risk rule that fetched its own inputs would decide what it was allowed to see.

    `equity_marks` and `symbol_bars` are `(captured_at, value)` pairs. Both are reduced on the
    **Eastern** session, never the UTC date -- a mark written at 20:05 New York belongs to that
    day's session and not the next one, and keying on UTC would take the previous session's
    opening equity as today's.

    An input that cannot be derived comes back `None` rather than defaulted, and the veto that
    needs it then rejects. Substituting a plausible value here would put the failure somewhere
    nobody looks -- and a zero reference price is the worst version of it, because zero is not a
    conservative guess: it makes a real order contribute no exposure at all.
    """
    session = session_date_of(observed_at)
    opening: Decimal | None = None
    for captured_at, value in sorted(equity_marks, key=lambda item: item[0]):
        if session_date_of(captured_at) == session:
            # The first mark of the session is what it opened at. Later marks are the day's
            # progress, which is the thing being measured against it.
            opening = value
            break
    latest_bar: datetime | None = None
    # Volume per **session**, not per row. Counting rows would overstate the history whenever a
    # feed carries more than one bar for a day, and the veto's whole question is how many
    # sessions back the average reaches.
    by_session: dict[date, Decimal] = {}
    for captured_at, volume in sorted(symbol_bars, key=lambda item: item[0]):
        latest_bar = captured_at
        bar_session = session_date_of(captured_at)
        by_session[bar_session] = by_session.get(bar_session, Decimal("0")) + volume
    volumes = [by_session[day] for day in sorted(by_session)]
    average_volume: Decimal | None = None
    if volumes:
        window = volumes[-volume_sessions:] if volume_sessions > 0 else volumes
        average_volume = sum(window, Decimal("0")) / Decimal(len(window))
    net = sum((position.market_value for position in positions), Decimal("0"))
    return PortfolioVetoInputs(
        symbol=symbol,
        equity=equity,
        committed_net_exposure=net,
        # `None` propagates rather than collapsing to zero. A caller with no price has an
        # unmeasured input, and the veto that needs it says so.
        order_net_exposure=None if order_price is None else order_quantity * order_price,
        order_quantity=order_quantity,
        session_date=session,
        session_opening_equity=opening,
        latest_market_data_at=latest_bar,
        observed_at=observed_at,
        average_daily_volume=average_volume,
        volume_sessions=len(volumes),
        # `None` when no classification was supplied, and the veto that needs it then rejects.
        # Defaulting to an empty table here would report every group at zero.
        exposure=(
            None
            if classification is None
            else build_exposure_state(
                classification=classification,
                symbol=symbol,
                positions=positions,
                as_of=session,
            )
        ),
    )


def session_date_of(moment: datetime) -> date:
    """The Eastern session a moment belongs to.

    Exported because a daily limit keyed on the wrong date resets mid-session, and the callers
    that build `PortfolioVetoInputs` must not each re-derive this. The same off-by-one already
    cost this project correct fill attribution and correct equity marks.
    """
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("a session date needs a timezone-aware moment")
    return moment.astimezone(XNYS_TIMEZONE).date()


__all__ = [
    "UNIMPLEMENTED",
    "build_veto_inputs",
    "PortfolioVetoDecision",
    "PortfolioVetoInputs",
    "PortfolioVetoPolicy",
    "VetoName",
    "VetoRejection",
    "evaluate_portfolio_vetoes",
    "session_date_of",
]
