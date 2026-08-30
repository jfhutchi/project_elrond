"""Does the detector buy better questions than a naive scan? (#56, the falsification gate)

#56's premise is economic before it is statistical: most symbols are informationally
uninteresting most of the time, so expensive news and model research should follow information
density rather than the calendar. That premise is falsifiable, and this measures it *before* any
enrichment is built -- because a subsystem that has already grown a news pipeline is much harder
to abandon than one that has not.

The success criterion is **better questions per unit search cost**, not profitable trades. An
anomaly score is a routing signal; it is never a direction, a confidence, or a claim of edge.

**Effective sample size, not event count.** Measured on the real bars, 53% of detected events
fall on the 49 sessions where five or more of 23 symbols fired at once -- 2025-04-07 fired 22 of
23. Those are one macro fact observed twenty-two times, and counting them as twenty-two
independent successes inflates every rate here, hardest exactly when the detector looks most
impressive.

The first attempt keyed on `(session, dependence group)` and **did not work**, which is worth
recording because it looked like it did. Complete linkage at 0.90 yields 20 clusters over 23
symbols -- almost all singletons -- so a 22-symbol macro day still counted as roughly 20
observations, and the reported inflation was a reassuring 1.08x. The pairwise threshold that
correctly refuses to call Treasuries and equities one bet is far too strict to describe what
happens when the whole market moves at once.

So the discount is measured from the outcomes rather than assumed from a threshold. Exact
duplicates within one `(session, group)` collapse first, then a Kish design effect is applied
with the **session** as the cluster:

    DEFF = 1 + (mean cluster size - 1) * ICC
    ESS  = units / DEFF

`ICC` is the intraclass correlation of the outcome within sessions, estimated from the data. It
answers the right question directly: when symbols firing on one day really do share a driver the
discount is large, and when they moved for their own reasons it is small. Nothing here depends
on choosing a second correlation threshold.

**The comparison must be against something that could actually be run.** A naive baseline that
enriches every symbol every session is the honest alternative: it is what an engine without a
detector would have to do. Selection rate against that is the search-cost saving, and it is the
one quantity here that needs no proxy and no assumption.

**What this module cannot measure, and says so.** Catalyst prevalence lift needs a catalyst
corpus, and this project has none yet: the universe is broad-market ETFs, which do not file
material 8-Ks the way single issuers do, and no news feed is integrated. `CATALYST_CORPUS_ABSENT`
is reported rather than substituted for, because a lift number computed against a proxy nobody
validated would be exactly the kind of flattering artefact this gate exists to catch.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class InformationProxy(StrEnum):
    """What stands in for "this question was worth asking".

    None of these is a catalyst. They are observable properties of the sessions the detector
    selected, and each is direction-free on purpose: an anomaly is a routing signal, and a proxy
    that rewarded direction would quietly turn this gate into a trading backtest.
    """

    #: Absolute return of the session **after** the event, against the same symbol's own
    #: trailing dispersion. Answers "did something continue to happen here", which is what an
    #: enrichment budget is really buying.
    NEXT_SESSION_ABSOLUTE_MOVE = "NEXT_SESSION_ABSOLUTE_MOVE"
    #: Catalyst prevalence. Unavailable: no news or filing corpus exists for this universe.
    CATALYST_PREVALENCE = "CATALYST_PREVALENCE"


class ProxyAvailability(StrEnum):
    MEASURED = "MEASURED"
    #: The corpus this proxy needs does not exist yet. Reported, never substituted.
    CATALYST_CORPUS_ABSENT = "CATALYST_CORPUS_ABSENT"
    #: Measurable in principle, but the sample was too small to say anything.
    UNDERPOWERED = "UNDERPOWERED"


#: Below this many effective observations, a rate is reported as `UNDERPOWERED` rather than as a
#: number. Chosen as a project default: thirty is the conventional floor at which a mean of a
#: bounded quantity starts to behave, and the tiebreak in CLAUDE.md prefers the choice that
#: refuses more. Versioned, so a later change cannot silently relabel an earlier verdict.
MINIMUM_EFFECTIVE_OBSERVATIONS = 30


class RoutingSample(BaseModel):
    """One symbol-session the detector could have selected, with its observable outcome."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: Text
    session_date: date
    selected: bool
    #: The dependence cluster this symbol was in on this session, or `None` when unmeasured.
    #: Two selected symbols sharing a session and a group are one effective observation.
    group_key: str | None
    #: Next-session absolute move over the symbol's trailing dispersion. `None` when the
    #: following session does not exist yet, which is unavailable rather than zero.
    next_absolute_move_sigma: Decimal | None


class ProxyResult(BaseModel):
    """One proxy's verdict, with its own availability rather than a shared one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proxy: InformationProxy
    availability: ProxyAvailability
    selected_mean: Decimal | None = None
    baseline_mean: Decimal | None = None
    #: Selected over baseline. `None` whenever either side is unavailable or underpowered.
    lift: Decimal | None = None
    detail: Text

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.availability is not ProxyAvailability.MEASURED and self.lift is not None:
            raise ValueError(
                f"{self.proxy.value} reports a lift while marked {self.availability.value}; an "
                "unmeasured proxy must not carry a number a reader could quote"
            )
        return self


class RoutingValueReport(BaseModel):
    """Whether routing earned its place. Frozen and content-addressed like every other verdict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1)
    detector_identity: Text
    #: Every symbol-session considered. The denominator of the search-cost saving.
    scanned_symbol_sessions: int = Field(ge=0)
    selected_symbol_sessions: int = Field(ge=0)
    #: Units after collapsing dependence groups, before the session design effect.
    dependence_units: int = Field(ge=0)
    #: Intraclass correlation of the outcome within sessions, estimated from the data.
    intraclass_correlation: Decimal = Field(ge=0, le=1)
    #: Kish design effect with the session as cluster.
    design_effect: Decimal = Field(ge=1)
    #: Units discounted by the design effect. The honest sample size.
    effective_observations: int = Field(ge=0)
    proxies: tuple[ProxyResult, ...]

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        if self.selected_symbol_sessions > self.scanned_symbol_sessions:
            raise ValueError("more symbol-sessions were selected than were scanned")
        if self.effective_observations > self.selected_symbol_sessions:
            raise ValueError(
                "effective observations exceed selected events; dependence grouping can only "
                "reduce a sample, never enlarge it"
            )
        if self.dependence_units > self.selected_symbol_sessions:
            raise ValueError("collapsing groups cannot produce more units than there were events")
        if self.effective_observations > self.dependence_units:
            raise ValueError(
                "the design effect enlarged the sample; a correlation discount can only shrink it"
            )
        return self

    @property
    def selection_rate(self) -> Decimal:
        """Fraction of the naive scan the detector would actually enrich."""
        if self.scanned_symbol_sessions == 0:
            return Decimal("0")
        return Decimal(self.selected_symbol_sessions) / Decimal(self.scanned_symbol_sessions)

    @property
    def searches_avoided(self) -> int:
        """Enrichment calls a naive scan would make that this detector does not."""
        return self.scanned_symbol_sessions - self.selected_symbol_sessions

    @property
    def sample_inflation(self) -> Decimal | None:
        """How much counting events rather than groups would have overstated the sample."""
        if self.effective_observations == 0:
            return None
        return Decimal(self.selected_symbol_sessions) / Decimal(self.effective_observations)

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"routing-value-v{self.version}-{digest[:16]}"


def dependence_units(
    samples: Sequence[RoutingSample],
) -> list[tuple[date, Decimal | None]]:
    """Collapse selected samples to one unit per `(session, dependence group)`.

    A symbol the snapshot could not place counts on its own, keyed by symbol, because
    "unmeasured" must not silently merge unrelated instruments into one observation. The outcome
    kept for a collapsed unit is the mean of its members, so a cluster contributes once.
    """
    grouped: dict[tuple[date, str], list[Decimal]] = {}
    for sample in samples:
        if not sample.selected:
            continue
        key = sample.group_key if sample.group_key is not None else f"ungrouped:{sample.symbol}"
        grouped.setdefault((sample.session_date, key), [])
        if sample.next_absolute_move_sigma is not None:
            grouped[(sample.session_date, key)].append(sample.next_absolute_move_sigma)
    return [
        (session, _mean(values) if values else None)
        for (session, _), values in sorted(grouped.items(), key=lambda pair: pair[0])
    ]


def intraclass_correlation(units: Sequence[tuple[date, Decimal | None]]) -> Decimal:
    """One-way random-effects ICC of the outcome, clustered by session.

    Estimated rather than assumed. Clamped to [0, 1]: a negative estimate means the between-
    session variance came out below the within-session variance, which is evidence of no
    clustering rather than of anti-clustering, and letting it go negative would *inflate* the
    effective sample -- the one direction this must never move.
    """
    by_session: dict[date, list[float]] = {}
    for session, outcome in units:
        if outcome is not None:
            by_session.setdefault(session, []).append(float(outcome))
    clusters = [values for values in by_session.values() if values]
    total = sum(len(values) for values in clusters)
    if len(clusters) < 2 or total <= len(clusters):
        return Decimal("0")

    grand = statistics.fmean(value for values in clusters for value in values)
    between = sum(
        len(values) * (statistics.fmean(values) - grand) ** 2 for values in clusters
    ) / (len(clusters) - 1)
    within = sum(
        (value - statistics.fmean(values)) ** 2 for values in clusters for value in values
    ) / (total - len(clusters))
    mean_size = total / len(clusters)
    denominator = between + (mean_size - 1) * within
    if denominator <= 0:
        return Decimal("0")
    icc = (between - within) / denominator
    return Decimal(str(round(max(0.0, min(1.0, icc)), 6)))


def design_effect(units: Sequence[tuple[date, Decimal | None]]) -> Decimal:
    """Kish design effect with the session as the cluster. Never below 1.

    A sample confined to a **single** session is the one case the ICC cannot speak to: with one
    cluster there is no between-cluster variance to estimate, and the estimator returns zero --
    which would mean no discount at all for the most obviously clustered sample possible. So it
    is handled directly: everything happened on one day, so the effective sample is one. That is
    the fail-closed reading, and it is the direction a sample-size discount must always err in.
    """
    if not units:
        return Decimal("1")
    sessions = {session for session, _ in units}
    if len(sessions) == 1:
        return Decimal(len(units))
    mean_size = len(units) / len(sessions)
    inflated = 1 + (mean_size - 1) * float(intraclass_correlation(units))
    return Decimal(str(round(max(1.0, inflated), 6)))


def effective_observations(samples: Sequence[RoutingSample]) -> int:
    """Units after collapsing dependence groups, discounted by the session design effect."""
    units = dependence_units(samples)
    if not units:
        return 0
    return max(1, int(len(units) / float(design_effect(units))))


def _mean(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(round(statistics.fmean(float(value) for value in values), 6)))


def evaluate_routing_value(
    *,
    detector_identity: str,
    samples: Sequence[RoutingSample],
    minimum_effective: int = MINIMUM_EFFECTIVE_OBSERVATIONS,
) -> RoutingValueReport:
    """Compare the detector's selection against enriching everything.

    The baseline is every scanned symbol-session, because that is what an engine with no
    detector would have to enrich. It needs no proxy and no assumption, which is why the
    search-cost saving is the one number here that is not conditional.

    The information proxy is compared on the same corpus: selected sessions against all scanned
    sessions. If the detector routes at random the two means coincide and the lift is 1, which is
    the negative result this gate must be able to return.
    """
    selected = [sample for sample in samples if sample.selected]
    units = dependence_units(samples)
    icc = intraclass_correlation(units)
    deff = design_effect(units)
    ess = effective_observations(samples)

    selected_moves = [
        sample.next_absolute_move_sigma
        for sample in selected
        if sample.next_absolute_move_sigma is not None
    ]
    baseline_moves = [
        sample.next_absolute_move_sigma
        for sample in samples
        if sample.next_absolute_move_sigma is not None
    ]

    if ess < minimum_effective:
        move_result = ProxyResult(
            proxy=InformationProxy.NEXT_SESSION_ABSOLUTE_MOVE,
            availability=ProxyAvailability.UNDERPOWERED,
            detail=(
                f"{ess} effective observations against a {minimum_effective} floor; a rate from "
                "this sample would invite tuning a threshold against noise"
            ),
        )
    else:
        selected_mean = _mean(selected_moves)
        baseline_mean = _mean(baseline_moves)
        lift = (
            None
            if selected_mean is None or baseline_mean is None or baseline_mean == 0
            else Decimal(str(round(selected_mean / baseline_mean, 4)))
        )
        move_result = ProxyResult(
            proxy=InformationProxy.NEXT_SESSION_ABSOLUTE_MOVE,
            availability=ProxyAvailability.MEASURED,
            selected_mean=selected_mean,
            baseline_mean=baseline_mean,
            lift=lift,
            detail=(
                "mean next-session absolute move in trailing sigmas, selected sessions against "
                "every scanned session; 1.0 is what random routing produces"
            ),
        )

    catalyst_result = ProxyResult(
        proxy=InformationProxy.CATALYST_PREVALENCE,
        availability=ProxyAvailability.CATALYST_CORPUS_ABSENT,
        detail=(
            "no news or filing corpus exists for this universe: it is broad-market ETFs, which "
            "do not file material 8-Ks the way single issuers do, and no news provider is "
            "integrated. Reported as absent rather than replaced by a proxy nobody validated"
        ),
    )

    return RoutingValueReport(
        detector_identity=detector_identity,
        scanned_symbol_sessions=len(samples),
        selected_symbol_sessions=len(selected),
        dependence_units=len(units),
        intraclass_correlation=icc,
        design_effect=deff,
        effective_observations=ess,
        proxies=(move_result, catalyst_result),
    )


def render(report: RoutingValueReport) -> Mapping[str, object]:
    """A flat summary for a report or an issue comment."""
    return {
        "identity": report.identity,
        "detector_identity": report.detector_identity,
        "scanned_symbol_sessions": report.scanned_symbol_sessions,
        "selected_symbol_sessions": report.selected_symbol_sessions,
        "selection_rate": str(report.selection_rate),
        "searches_avoided": report.searches_avoided,
        "dependence_units": report.dependence_units,
        "intraclass_correlation": str(report.intraclass_correlation),
        "design_effect": str(report.design_effect),
        "effective_observations": report.effective_observations,
        "sample_inflation": (
            None if report.sample_inflation is None else str(report.sample_inflation)
        ),
        "proxies": {
            item.proxy.value: {
                "availability": item.availability.value,
                "lift": None if item.lift is None else str(item.lift),
            }
            for item in report.proxies
        },
    }


__all__ = [
    "MINIMUM_EFFECTIVE_OBSERVATIONS",
    "dependence_units",
    "design_effect",
    "intraclass_correlation",
    "InformationProxy",
    "ProxyAvailability",
    "ProxyResult",
    "RoutingSample",
    "RoutingValueReport",
    "effective_observations",
    "evaluate_routing_value",
    "render",
]
