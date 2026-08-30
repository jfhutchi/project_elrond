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
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.domain import Position
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.risk.dependence import (
    DependenceSnapshot,
    DependenceState,
    build_dependence_state,
)
from quantbot.risk.evidence import EVIDENCE_FIELDS, DecisionEvidence, EvidenceKind
from quantbot.risk.exposure import (
    ExposureClassification,
    ExposureGroup,
    ExposureState,
    Provenance,
    build_exposure_state,
)
from quantbot.risk.freshness import (
    FreshnessExpectation,
    FreshnessOutcome,
    MarketFreshnessPolicy,
    assess_freshness,
    expected_freshness,
)
from quantbot.strategy import XNYSSession

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
    #: Concentration in a cluster of instruments that have recently *moved* together, as
    #: opposed to sharing a mandate. Backed by a frozen point-in-time dependence snapshot (#59).
    CORRELATION_CONCENTRATION = "CORRELATION_CONCENTRATION"


#: Vetoes with no evaluator behind them. Empty since #58's correlation rule landed, and kept
#: rather than deleted: the mechanism that refuses to enable an unimplemented veto is what stops
#: a name from reading as a control, and it should be here waiting the next time one is declared.
UNIMPLEMENTED: frozenset[VetoName] = frozenset()


class PolicyAuthority(StrEnum):
    """Who chose these numbers. Provenance, never permission (#72, #75).

    A threshold an agent selected is not operator-approved, and a policy that cannot say which it
    is invites exactly the misreading #72's non-goals forbid. The same rule as the economic
    mandate's `authority`: state it plainly rather than gate on it.
    """

    #: Selected by the system from an existing deployed value or a project constraint, then
    #: frozen and versioned. Sufficient to run paper trading; not an approval.
    PROJECT_DEFAULT = "PROJECT_DEFAULT"
    #: The operator set these deliberately.
    OPERATOR_SET = "OPERATOR_SET"


#: Ceiling for any exposure group with no specific entry, in basis points of equity.
#:
#: **Derived, not picked.** The strategy holds `roster_size: 10` positions at roughly equal
#: weight, so 4000bps is four concurrent holdings carrying one exposure. That permits the
#: rotation's normal behaviour -- including a duration book of TLT + IEF + TIP + LQD -- and
#: refuses a fifth, which is a pile-in rather than a rotation.
DEFAULT_GROUP_CEILING_BPS = 4000

#: Groups whose ceiling is the net-exposure limit, because constraining them below it would
#: refuse the strategy's whole purpose rather than a concentration.
#:
#: `US_EQUITY` is the one that matters: measured against the deployed roster it stands at
#: ~5972bps of equity, and every broad and sector holding in the universe carries it. A limit
#: tight enough to bind here would refuse nearly every order the strategy wants to place, which
#: is a control that stops the research rather than one that bounds a risk.
STRUCTURAL_GROUPS: frozenset[ExposureGroup] = frozenset({ExposureGroup.US_EQUITY})


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
    max_net_exposure_bps: int = Field(
        title="Total exposure ceiling",
        description=(
            "How much of the account may be invested at once, rather than sitting in cash. "
            "Basis points, where 10000 is 100% -- so 10000 means the whole account may be "
            "in positions. Lowering it keeps more in cash: fewer opportunities taken, and a "
            "smaller loss on any day the whole market falls."
        ),
        default=10000, gt=0)

    #: Loss from the session's opening equity that halts new entries for the rest of the day.
    #: Keyed on the **Eastern** session, not UTC -- a 20:05 New York mark is the next UTC day,
    #: and a UTC-keyed limit would reset mid-session and halt on the previous day's losses. The
    #: same off-by-one already bit fill attribution and equity marks.
    max_daily_loss_bps: int = Field(
        title="Daily loss limit",
        description=(
            "A stop for the whole account, not one position. If the account is down this "
            "much from where it opened this morning, no new positions are opened for the "
            "rest of the day; existing ones are left alone. 500 is 5%. It exists to stop a "
            "bad day compounding into a worse one through trades made while things are "
            "already going wrong."
        ),
        default=500, gt=0)

    #: How many expected intervals an **intraday** feed may miss while the market is open
    #: before it counts as stopped. Ignored for daily bars, whose freshness is a question about
    #: sessions rather than seconds (#74).
    #:
    #: Three, because one missed interval is a hiccup and a feed that has produced nothing for
    #: three consecutive expected updates during an open session has stopped rather than slowed.
    #:
    #: **This replaced a wall clock, and the history is why.** The veto first shipped a
    #: 900-second ceiling, which encoded an intraday assumption into a daily-bar system and
    #: refused every entry. That was corrected to four days, which cleared a long weekend and
    #: was recorded in the code as a coarse proxy for "sessions behind". Elapsed time was the
    #: wrong measurement both times: a Friday close is current all weekend, a Tuesday close is
    #: stale on Wednesday, and no single number distinguishes them. The calendar does.
    freshness: MarketFreshnessPolicy = MarketFreshnessPolicy()

    #: The order may not exceed this share of the symbol's average daily volume. A position that
    #: cannot be exited without moving the price is a position whose backtest never happened.
    max_participation_bps: int = Field(
        title="Share of traded volume",
        description=(
            "How large an order may be compared with how much of that symbol normally "
            "trades in a day. 100 is 1%. This matters because buying a large share of a "
            "day's volume pushes the price up as you buy -- so you pay more than the price "
            "you saw, and the profit the backtest measured never existed. Lower is more "
            "cautious and means smaller positions."
        ),
        default=100, gt=0)
    #: Sessions of volume history required before the liquidity veto will pass anything. Below
    #: this the average is not an average, and an unmeasurable limit must not allow.
    minimum_volume_sessions: int = Field(
        title="Sessions of volume history",
        description=(
            "How many days of trading history a symbol needs before an order in it is "
            "allowed at all. Without enough history there is no way to tell whether today's "
            "volume is normal or unusual, so the order is refused rather than sized against "
            "a guess."
        ),
        default=20, gt=0)

    #: Ceiling on any one exposure group's committed value, in basis points of equity.
    #:
    #: The fallback for any group without its own ceiling below. This universe is mostly US
    #: equity -- a roster of 10 from 23 can be near 100% `US_EQUITY` while being exactly what the
    #: strategy is designed to hold -- so one global threshold cannot serve every group: it is
    #: too loose for the dominant one and refuses every order if set for the narrow ones.
    #:
    #: **That is why `group_exposure_ceilings_bps` exists, and this is the fallback rather than
    #: the rule.** An earlier version of this comment said the value was a placeholder, that the
    #: veto shipped disabled, and that per-group ceilings were an operator decision. All three
    #: stopped being true: #75 ships every implemented veto enabled, the per-group ceilings are
    #: below, and #72 settled that a threshold like this is a versioned project default an agent
    #: chooses and labels rather than a preference it waits for. The prose is corrected here
    #: because a future reader would otherwise take it as a live instruction to stop (#72).
    max_group_exposure_bps: int = Field(
        title="Per-group exposure ceiling",
        description=(
            "A ceiling on how much may sit in one *kind* of thing -- US shares, government "
            "bonds, corporate credit -- rather than in one symbol. Holding ten different "
            "funds that all rise and fall with the US stock market is one bet wearing ten "
            "names, and this is what stops the account taking it. 4000 is 40% of the "
            "account in any single kind."
        ),
        default=DEFAULT_GROUP_CEILING_BPS, gt=0)

    #: Ceilings for named groups, overriding `max_group_exposure_bps`. A single global threshold
    #: cannot serve this universe: it is too loose to constrain the narrow groups if set for the
    #: dominant one, and refuses every order if set for the narrow ones.
    group_exposure_ceilings_bps: Mapping[ExposureGroup, int] = Field(
        default_factory=lambda: {group: 10000 for group in STRUCTURAL_GROUPS}
    )

    #: Who chose these numbers. Excluded from `identity` for the reason the economic mandate
    #: excludes its lifecycle fields: the identity answers "which thresholds were in force", and
    #: an operator adopting the same numbers does not change which numbers they are.
    authority: PolicyAuthority = PolicyAuthority.PROJECT_DEFAULT

    #: Pairwise correlation at or above which two instruments count as one bet, and the
    #: ceiling on what a single such cluster may reach in basis points of equity.
    #:
    #: **0.90 is measured, not picked.** Over 120 sessions of daily log returns on this universe,
    #: complete-linkage clustering at 0.90 yields `{IEF, LQD, TLT}` -- duration, and nothing else
    #: larger. Below 0.85 the clusters begin absorbing most of the book, and the veto stops
    #: distinguishing a concentrated roster from an ordinary one.
    #:
    #: 4000bps matches the exposure ceiling for the same reason: with `roster_size: 10` at
    #: roughly equal weight it binds at four concurrent holdings inside one cluster.
    correlation_threshold: Decimal = Field(
        title="Correlation cluster threshold",
        description=(
            "Correlation measures whether two investments tend to move at the same time: "
            "near 1.0 they rise and fall almost together, near 0 they move independently. "
            "Two holdings that move together are not really two holdings -- if one falls "
            "the other very likely falls too, so the account is far less spread out than "
            "the number of positions suggests. This setting is the line above which two "
            "symbols are treated as the same position for the exposure limits. At 0.90 the "
            "groups it finds are tight and genuinely related; below about 0.85 they start "
            "swallowing most of the portfolio, and the control stops telling a concentrated "
            "account apart from a normal one."
        ),
        default=Decimal("0.90"), gt=0, le=1)
    max_correlated_cluster_bps: int = Field(
        title="Per-cluster exposure ceiling",
        description=(
            "The ceiling on how much may sit in one group of holdings that move together, "
            "found using the correlation setting above. 4000 is 40% of the account. It is "
            "the same idea as the per-kind ceiling, but the groups are discovered from how "
            "prices actually behaved rather than from what the funds are labelled as."
        ),
        default=4000, gt=0)

    #: Sessions of history the dependence snapshot is measured over.
    #:
    #: **It lives here, and only here, because two consumers need it to agree (#84).** The data
    #: sync must fetch at least this many sessions or the veto is permanently unmeasurable, and
    #: the snapshot builder must measure over exactly this many or the threshold above was
    #: chosen against a different window. Those were separate constructor arguments "kept in
    #: step" by a comment, which is a convention rather than an invariant -- and #82 is about to
    #: put a configuration surface in front of both.
    #:
    #: Being a policy field also means changing it changes `identity`, so a refusal measured
    #: over 120 sessions can never be pooled with one measured over 60.
    correlation_lookback_sessions: int = Field(
        title="Correlation lookback",
        description=(
            "How many days of price history are used to work out which holdings move "
            "together. Shorter means the measure reacts faster to a change in behaviour, "
            "but is noisier and more easily fooled by a few unusual days. It also sets how "
            "much history must be downloaded before any decision can be made at all, so "
            "raising it makes the system need more data before it will trade."
        ),
        default=120, ge=2)

    #: Vetoes to enforce. Anything absent is not evaluated, which is a policy choice and is
    #: recorded in `identity` so a study can tell one policy from another.
    #:
    #: **Every implemented veto is on by default (#75).** They previously shipped disabled
    #: because threshold selection was described as operator policy, which left tested controls
    #: doing nothing while waiting for a preference nobody had been asked for. A paper runtime
    #: should start with a complete versioned policy.
    #:
    #: This is not free, and the cost belongs in the open: `LIQUIDITY` refuses entries until
    #: twenty sessions of volume exist per symbol, and `DAILY_LOSS` refuses while the session's
    #: opening equity is unmeasured. Both are fail-closed by design. The trade is deliberate --
    #: a false refusal costs a paper entry, a false permit costs the thing the control exists to
    #: prevent -- and #57 records every refusal so the choice can be revisited on evidence.
    enabled: frozenset[VetoName] = frozenset(
        {
            VetoName.NET_EXPOSURE,
            VetoName.STALE_MARKET_DATA,
            VetoName.DAILY_LOSS,
            VetoName.LIQUIDITY,
            VetoName.EXPOSURE_CONCENTRATION,
            VetoName.CORRELATION_CONCENTRATION,
        }
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
        # Provenance, not a threshold. Two policies with identical numbers behave identically and
        # must group as one dataset in a counterfactual study; who chose them is reported by
        # `authority` instead.
        payload.pop("authority", None)
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

    #: What the exchange calendar says the feed owes at `observed_at` (#74). `None` rejects:
    #: without it there is no way to tell a quiet holiday from a dead feed, and guessing in
    #: either direction is worse than refusing.
    freshness: FreshnessExpectation | None = None

    #: Average daily volume over `minimum_volume_sessions`, and how many sessions backed it.
    average_daily_volume: NonNegativeDecimal | None = None
    volume_sessions: int = Field(default=0, ge=0)

    #: The correlated cluster this order would join, or `None` when no dependence snapshot was
    #: supplied. `None` rejects: without a snapshot every cluster reads as empty, which is the
    #: most permissive possible answer.
    dependence: DependenceState | None = None

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

    #: Freshness evidence, when `STALE_MARKET_DATA` refused (#74). Structured rather than only in
    #: the reason string, because #57 studies rules across versions and cannot group on prose.
    freshness_policy_identity: str | None = None
    expected_session: date | None = None
    observed_session: date | None = None
    market_state: str | None = None

    #: The dependence snapshot a correlation refusal rested on (#58/#59), so a counterfactual
    #: study can tell a refusal under one matrix from one under another.
    dependence_snapshot_identity: str | None = None

    def decision_evidence(self) -> DecisionEvidence:
        """The contemporaneous context this refusal rested on, ready to persist (#85).

        Assembled here rather than at the persistence boundary, so the module that produces the
        evidence also decides its shape. A composer that read these fields itself would be a
        second place to update, and the one that got forgotten would fail silently: the
        rejection would still store, just without its basis.

        The kind is derived from which fields are populated rather than from the veto name. A
        veto that refused *because it could not measure* its input has no context to record even
        though its name suggests otherwise, and `NONE` is the honest answer there.
        """
        for kind in (EvidenceKind.MARKET_FRESHNESS, EvidenceKind.DEPENDENCE):
            values = {field: getattr(self, field) for field in EVIDENCE_FIELDS[kind]}
            if any(value is not None for value in values.values()):
                return DecisionEvidence(
                    kind=kind,
                    payload={
                        # An empty string is "this kind was recorded and that field was not
                        # measurable", which is different from the key being absent -- absent
                        # would mean a writer that did not know the field existed.
                        field: "" if value is None else str(value)
                        for field, value in values.items()
                    },
                )
        return DecisionEvidence(kind=EvidenceKind.NONE, payload={})


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
    veto: VetoName,
    policy: PortfolioVetoPolicy,
    reason: str,
    unmeasured: str | None = None,
    **evidence: object,
) -> VetoRejection:
    return VetoRejection(
        veto=veto,
        policy_version=policy.version,
        rule_parameters_hash=policy.identity,
        reason=reason,
        unmeasured=unmeasured,
        **evidence,
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
    """Judge the feed against the exchange calendar rather than against a clock (#74).

    The rejection carries the expected session, the observed session and the outcome, so a reader
    can tell a missing session from a dead intraday feed from an unmeasurable one without
    re-deriving any of it.
    """
    assessment = assess_freshness(
        expectation=inputs.freshness,
        latest_observation_at=inputs.latest_market_data_at,
        observed_at=inputs.observed_at,
        symbol=inputs.symbol,
        tolerated_intervals=policy.freshness.tolerated_intervals,
    )
    if assessment.fresh:
        return None
    return _reject(
        VetoName.STALE_MARKET_DATA,
        policy,
        f"{assessment.outcome.value}: {assessment.detail}",
        # Only the unmeasurable outcome is a missing input. A missing session and a stopped feed
        # are measurements, and reporting them as unmeasured would send a reader looking for
        # absent plumbing instead of absent data.
        unmeasured=(
            "market_data_freshness"
            if assessment.outcome is FreshnessOutcome.FRESHNESS_METADATA_UNAVAILABLE
            else None
        ),
        freshness_policy_identity=assessment.policy_identity,
        expected_session=assessment.expected_session,
        observed_session=assessment.observed_session,
        market_state=assessment.market_state.value,
    )


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
    breached: list[tuple[ExposureGroup, Decimal, Decimal]] = []
    for group in sorted(state.order_groups):
        limit_bps = policy.group_exposure_ceilings_bps.get(group, policy.max_group_exposure_bps)
        ceiling = inputs.equity * Decimal(limit_bps) / BASIS_POINTS
        projected = state.committed_by_group.get(group, Decimal("0")) + added
        if projected > ceiling:
            breached.append((group, projected, ceiling))
    if breached:
        # Each breach names how that exposure was assigned. A refusal driven by a prospectus and
        # one driven by a declared stress proxy are different claims, and #57 cannot pool them
        # into one dataset if the rejection does not say which it was.
        detail = "; ".join(
            f"{group.value} ({state.order_provenance.get(group, Provenance.MANDATE).value}) "
            f"would reach {value} against a {limit} ceiling"
            for group, value, limit in breached
        )
        return _reject(
            VetoName.EXPOSURE_CONCENTRATION,
            policy,
            f"{detail}, on equity of {inputs.equity}",
        )
    return None


def _correlation_concentration(
    inputs: PortfolioVetoInputs, policy: PortfolioVetoPolicy
) -> VetoRejection | None:
    """Refuse an order that would concentrate the book in one cluster of co-moving instruments.

    This is the empirical sibling of `EXPOSURE_CONCENTRATION`. That one asks what an instrument
    *is* by mandate; this asks what it has recently *done*. They disagree usefully: `HYG` carries
    a credit mandate and correlates with `SPY` at 0.82, and only one of the two rules can see it.
    """
    if inputs.dependence is None:
        return _reject(
            VetoName.CORRELATION_CONCENTRATION,
            policy,
            f"no dependence snapshot was supplied for {inputs.symbol}, so the cluster this order "
            "would join is unknown; an absent snapshot would otherwise report every cluster as "
            "empty",
            unmeasured="dependence_snapshot",
        )
    state = inputs.dependence
    if state.unmeasured:
        # A held position outside the snapshot understates its cluster's total, and an
        # understated numerator is the permissive direction.
        return _reject(
            VetoName.CORRELATION_CONCENTRATION,
            policy,
            f"{', '.join(state.unmeasured)} has no measured dependence as of the snapshot, so "
            "any cluster total that includes it understates what is actually committed",
            unmeasured="UNMEASURED_SYMBOL",
        )
    if state.order_cluster is None:
        return _reject(
            VetoName.CORRELATION_CONCENTRATION,
            policy,
            f"{inputs.symbol} has no measured dependence, so the cluster this order would join "
            "cannot be identified",
            unmeasured="UNMEASURED_SYMBOL",
        )
    if inputs.order_net_exposure is None:
        return _reject(
            VetoName.CORRELATION_CONCENTRATION,
            policy,
            f"no reference price could be measured for {inputs.symbol}, so the value this order "
            "would add to its cluster is unknown",
            unmeasured="REFERENCE_PRICE_UNAVAILABLE",
        )

    projected = state.committed_in_cluster + abs(inputs.order_net_exposure)
    ceiling = inputs.equity * Decimal(policy.max_correlated_cluster_bps) / BASIS_POINTS
    if projected > ceiling:
        return _reject(
            VetoName.CORRELATION_CONCENTRATION,
            policy,
            f"{', '.join(state.order_cluster)} move together at or above {state.threshold} and "
            f"would reach {projected} against a {ceiling} ceiling "
            f"({policy.max_correlated_cluster_bps}bps of {inputs.equity})",
            dependence_snapshot_identity=state.snapshot_identity,
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
    VetoName.CORRELATION_CONCENTRATION: _correlation_concentration,
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
    sessions: Sequence[XNYSSession] = (),
    freshness_policy: MarketFreshnessPolicy | None = None,
    dependence: DependenceSnapshot | None = None,
    correlation_threshold: Decimal = Decimal("0.90"),
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
        # `None` when no calendar was supplied, and the veto then refuses. An empty calendar
        # must not read as an open market with nothing owed.
        freshness=expected_freshness(
            sessions=sessions,
            observed_at=observed_at,
            policy=freshness_policy or MarketFreshnessPolicy(),
        ),
        # `None` when no classification was supplied, and the veto that needs it then rejects.
        # Defaulting to an empty table here would report every group at zero.
        # `None` when no snapshot was supplied, and the veto then rejects. An absent matrix
        # would otherwise report every cluster as empty.
        dependence=(
            None
            if dependence is None
            else build_dependence_state(
                snapshot=dependence,
                symbol=symbol,
                positions=positions,
                threshold=correlation_threshold,
            )
        ),
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
