"""Has this strategy actually beaten its benchmark, and by enough to be worth calling an edge?

#51's capacity ladder asks at what account size an edge erodes. It needs an edge to erode, and
`gross_excess_bps` was a parameter nothing supplied -- so the ladder reported `UNASSESSED`
forever, correctly, and would have gone on doing so after the data arrived because nothing
computed the number.

That is the pattern this branch has now found nine times: a control correct in isolation waiting
on an input no code produces. The measurement exists here so that when forward evidence
accumulates the ladder answers, rather than waiting for somebody to notice it never could.

**It refuses far more often than it answers, and that is the design.** Three separate refusals,
each a different way a capacity verdict could rest on nothing:

1. *Too few sessions.* A return measured over a handful of days is noise with a sign.
2. *An excess indistinguishable from zero.* If the strategy has not measurably beaten its
   benchmark, there is no edge for capacity to erode -- and a ladder fed a statistically
   meaningless number would produce a confident-looking AUM figure resting on nothing.
3. *Missing data on either side.* An excess needs both legs; one alone is a return, not an edge.

The third refusal is the one worth being loudest about. A raw strategy return would flatter the
system every time the market rose, and "we made 8%" is not evidence of skill in a year the
benchmark made 12%.

**Equity is assumed not to move for reasons other than trading.** A deposit into the account
would read as a return. This holds for the paper account today, where nothing deposits, and is
recorded as an assumption rather than checked -- a check would need a cash-flow record this
project does not keep.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

#: Sessions of paired daily returns required before an excess may be reported at all.
#:
#: A `PROJECT_DEFAULT` (#72). Chosen as the anomaly detector's baseline window rather than a
#: number invented here, so "long enough to characterise this" means the same thing in both
#: places. It is a floor on *arithmetic sanity*, not a claim of statistical adequacy -- the
#: significance test below is what decides whether the result means anything, and it routinely
#: refuses windows far longer than this.
MINIMUM_SESSIONS = 60

#: Sessions per year, for annualising. The project's calendar constant.
SESSIONS_PER_YEAR = 252

#: |t| a mean daily excess must clear before it is reported as an edge.
#:
#: A `PROJECT_DEFAULT`. 2.0 is roughly a 5% two-sided test at these sample sizes and is the
#: conventional bar. Deliberately not tuned: the tiebreak in CLAUDE.md prefers the value that
#: refuses more, and a threshold chosen to let an existing result through would be exactly the
#: manufacture of significance this module exists to prevent.
MINIMUM_ABS_T = Decimal("2.0")


class ExcessUnavailable(StrEnum):
    """Why no excess could be reported. Each is a fact about evidence, never about plumbing."""

    TOO_FEW_SESSIONS = "TOO_FEW_SESSIONS"
    #: The strategy did not measurably beat its benchmark, so there is no edge to erode.
    NOT_DISTINGUISHABLE_FROM_ZERO = "NOT_DISTINGUISHABLE_FROM_ZERO"
    NO_STRATEGY_RETURNS = "NO_STRATEGY_RETURNS"
    NO_BENCHMARK_RETURNS = "NO_BENCHMARK_RETURNS"
    #: Every paired day moved identically, so the spread has no variance to test against.
    NO_VARIANCE = "NO_VARIANCE"


class ForwardExcess(BaseModel):
    """A measured benchmark-relative excess, or a stated reason there is none."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    benchmark_symbol: str
    #: Paired sessions actually used. Both legs had to be present on each.
    sessions: int
    unavailable: ExcessUnavailable | None = None
    detail: str
    #: Annualised, in basis points. Populated only when the excess cleared every refusal.
    bps: Decimal | None = None
    #: The t-statistic on the mean daily excess. Carried even when it refused, because "we
    #: measured and it was not significant" and "we could not measure" are different findings and
    #: a reader deciding what to do next needs to tell them apart.
    t_statistic: Decimal | None = None
    first_session: date | None = None
    last_session: date | None = None

    @property
    def measured(self) -> bool:
        return self.bps is not None


def _daily_returns(series: Sequence[tuple[date, Decimal]]) -> dict[date, Decimal]:
    """Session-over-session returns, keyed by the later session.

    Keyed by the later day so a strategy return and a benchmark return for the same date describe
    the same interval. Keying by the earlier one would shift the two legs against each other by a
    session, which is a look-ahead in one direction and a lag in the other.
    """
    ordered = sorted(series)
    returns: dict[date, Decimal] = {}
    for (_, previous), (day, current) in zip(ordered, ordered[1:], strict=False):
        if previous > 0:
            returns[day] = (current - previous) / previous
    return returns


def measure_forward_excess(
    *,
    strategy_id: str,
    benchmark_symbol: str,
    equity: Sequence[tuple[date, Decimal]],
    benchmark_closes: Mapping[date, Decimal],
    minimum_sessions: int = MINIMUM_SESSIONS,
    minimum_abs_t: Decimal = MINIMUM_ABS_T,
) -> ForwardExcess:
    """Measure the strategy's excess over its benchmark, or say why there is none to report.

    Takes series rather than a repository, for the reason every deterministic component here
    does: a function that reads the store it is judging cannot be tested about what it would say
    on a different one.

    Paired daily differences rather than a difference of total returns. The pairing is what makes
    the significance test meaningful -- it removes the market move common to both legs, so the
    variance being tested is the variance of the *spread* rather than the variance of the market,
    which is far larger and would hide any realistic edge.
    """
    strategy_returns = _daily_returns(equity)
    benchmark_returns = _daily_returns(sorted(benchmark_closes.items()))

    def refuse(reason: ExcessUnavailable, detail: str) -> ForwardExcess:
        """No sessions were paired, so the count is zero rather than unknown."""
        return ForwardExcess(
            strategy_id=strategy_id,
            benchmark_symbol=benchmark_symbol,
            sessions=0,
            unavailable=reason,
            detail=detail,
        )

    if not strategy_returns:
        return refuse(
            ExcessUnavailable.NO_STRATEGY_RETURNS,
            "no equity history, so the strategy has no return to compare",
        )
    if not benchmark_returns:
        return refuse(
            ExcessUnavailable.NO_BENCHMARK_RETURNS,
            f"no stored {benchmark_symbol} history; a raw strategy return would credit this "
            "strategy for market direction it had no view on",
        )

    paired = sorted(set(strategy_returns) & set(benchmark_returns))
    if len(paired) < minimum_sessions:
        return ForwardExcess(
            strategy_id=strategy_id,
            benchmark_symbol=benchmark_symbol,
            sessions=len(paired),
            unavailable=ExcessUnavailable.TOO_FEW_SESSIONS,
            detail=(
                f"{len(paired)} paired session(s) against a floor of {minimum_sessions}; a "
                "return measured over this little is noise with a sign"
            ),
            first_session=paired[0] if paired else None,
            last_session=paired[-1] if paired else None,
        )

    spread = [float(strategy_returns[day] - benchmark_returns[day]) for day in paired]
    mean = statistics.fmean(spread)
    deviation = statistics.stdev(spread) if len(spread) > 1 else 0.0
    if deviation == 0.0:
        return ForwardExcess(
            strategy_id=strategy_id,
            benchmark_symbol=benchmark_symbol,
            sessions=len(paired),
            unavailable=ExcessUnavailable.NO_VARIANCE,
            detail=(
                "every paired session moved identically, so the spread has no variance and "
                "nothing can be tested against it"
            ),
            first_session=paired[0],
            last_session=paired[-1],
        )

    t = Decimal(str(mean / (deviation / len(spread) ** 0.5))).quantize(Decimal("0.0001"))
    annualised = Decimal(str(mean * SESSIONS_PER_YEAR * 10000)).quantize(Decimal("0.01"))

    if abs(t) < minimum_abs_t:
        return ForwardExcess(
            strategy_id=strategy_id,
            benchmark_symbol=benchmark_symbol,
            sessions=len(paired),
            unavailable=ExcessUnavailable.NOT_DISTINGUISHABLE_FROM_ZERO,
            detail=(
                f"mean daily excess is {annualised}bps annualised at t={t}, which does not "
                f"clear {minimum_abs_t}; there is no edge for capacity to erode, and feeding "
                "this number to the ladder would produce a confident figure resting on noise"
            ),
            t_statistic=t,
            first_session=paired[0],
            last_session=paired[-1],
        )

    return ForwardExcess(
        strategy_id=strategy_id,
        benchmark_symbol=benchmark_symbol,
        sessions=len(paired),
        detail=(
            f"{annualised}bps annualised over {len(paired)} paired sessions at t={t}, measured "
            "against daily benchmark-relative differences"
        ),
        bps=annualised,
        t_statistic=t,
        first_session=paired[0],
        last_session=paired[-1],
    )


__all__ = [
    "MINIMUM_ABS_T",
    "MINIMUM_SESSIONS",
    "SESSIONS_PER_YEAR",
    "ExcessUnavailable",
    "ForwardExcess",
    "measure_forward_excess",
]
