"""What kind of market this was, frozen at the moment it was measured (#59).

`DependenceSnapshot` answers *what moved together*. This answers *what the market was doing* --
a small, deterministic, point-in-time description that later analysis can condition on without
reconstructing it from today's data.

**Narrow on purpose.** #59 asks for a versioned baseline rather than a preferred taxonomy, and a
regime vocabulary is the easiest place in this project to manufacture significance: enough tags
and every result finds a regime it works in. So there are three axes, each computed from the
benchmark's own history, each with an explicit unavailable state, and no composite label. A
composite would be a fourth free parameter dressed as a summary.

**A regime is context, never authority.** Nothing here may permit an order, relax a veto, or
raise a ceiling. It exists so a refusal or an outcome can later be grouped by the conditions it
occurred under, and grouping is the whole of its power.

**Method changes mint a new snapshot; they never relabel an old one.** The thresholds below are
project defaults under #72 and they are in the identity, so a snapshot computed under one
definition can never be pooled with one computed under another. Reconstructing an old decision's
regime from today's definition would answer what the market *would* be called now, which is a
different question from what it was called then -- the same rule #84 established for dependence
and #85 for veto evidence.

**Unavailable is a state, never a neutral value.** A benchmark with too little history is not a
calm market. `UNAVAILABLE` propagates and is recorded, because a study that silently treated
missing context as ordinary context would find regime effects in its own gaps.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TrendState(StrEnum):
    """Where the benchmark sits against its own trailing average."""

    ABOVE = "ABOVE"
    BELOW = "BELOW"
    #: Not enough history to place it. Distinct from being near the average.
    UNAVAILABLE = "UNAVAILABLE"


class VolatilityState(StrEnum):
    """Realised volatility against its own longer-run distribution.

    Terciles of the trailing distribution rather than absolute thresholds: an absolute bps figure
    would encode one era's volatility as a permanent definition, and every regime study across a
    decade would then be measuring the calendar.
    """

    CALM = "CALM"
    NORMAL = "NORMAL"
    STRESSED = "STRESSED"
    UNAVAILABLE = "UNAVAILABLE"


class BreadthState(StrEnum):
    """How much of the universe is participating in the benchmark's direction."""

    #: Most of the universe moving with the benchmark.
    BROAD = "BROAD"
    #: A minority carrying it. The interesting case, and the one a single index hides.
    NARROW = "NARROW"
    UNAVAILABLE = "UNAVAILABLE"


#: Sessions of history each axis needs. Project defaults under #72, chosen to match structures
#: already in this repository rather than picked fresh: 200 is the trend window the strategy's
#: own configs use, 20 is the realised-volatility window the risk layer already treats as the
#: minimum meaningful sample, and 250 is a year of sessions for the volatility distribution.
TREND_SESSIONS = 200
VOLATILITY_SESSIONS = 20
VOLATILITY_DISTRIBUTION_SESSIONS = 250

#: Fraction of the universe that must move with the benchmark for breadth to read `BROAD`.
#: A simple majority, deliberately: a tuned threshold here would be a free parameter that every
#: later regime-conditioned result inherits.
BROAD_PARTICIPATION = Decimal("0.5")


class MarketRegimeSnapshot(BaseModel):
    """A frozen, content-addressed description of market conditions as of one session."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method_version: int = Field(default=1, ge=1)
    #: The session this describes. Not a wall-clock time: regimes are session-scoped.
    as_of_session: date
    #: The instrument the trend and volatility axes were computed from.
    benchmark_symbol: Text
    #: Where the price series came from, so a provider change is a new snapshot.
    source: Text
    trend: TrendState
    volatility: VolatilityState
    breadth: BreadthState
    #: Realised volatility over `VOLATILITY_SESSIONS`, annualised, when measurable.
    realised_volatility: Decimal | None = None
    #: Fraction of the universe moving with the benchmark, when measurable.
    participation: Decimal | None = None
    #: Which axes could not be computed, and why. Named rather than left to the enum alone, so a
    #: reader knows whether history was short or the input was absent.
    unavailable: tuple[str, ...] = ()
    #: Other frozen artifacts this was derived alongside, so a study can trace the whole context.
    source_snapshot_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        # A list, not a dict keyed on the enum members. All three enums are `StrEnum` with the
        # value "UNAVAILABLE", so they compare and hash equal across classes: a dict keyed on
        # them collapses to one entry and only the last axis is ever checked. Found by the test
        # below, which is the whole reason it constructs a trend-only gap.
        degraded = (
            self.trend is TrendState.UNAVAILABLE,
            self.volatility is VolatilityState.UNAVAILABLE,
            self.breadth is BreadthState.UNAVAILABLE,
        )
        if any(degraded) and not self.unavailable:
            raise ValueError(
                "an axis reports UNAVAILABLE without saying why; an unexplained gap is "
                "indistinguishable from an ordinary reading once it is in a dataset"
            )
        if self.volatility is VolatilityState.UNAVAILABLE and self.realised_volatility is not None:
            raise ValueError(
                "a volatility state of UNAVAILABLE cannot carry a measured volatility"
            )
        if self.breadth is BreadthState.UNAVAILABLE and self.participation is not None:
            raise ValueError("a breadth state of UNAVAILABLE cannot carry a measured share")
        if list(self.source_snapshot_ids) != sorted(set(self.source_snapshot_ids)):
            raise ValueError("source snapshot ids must be sorted and unique")
        return self

    @property
    def fully_measured(self) -> bool:
        """Whether every axis resolved. False means a study must treat this as degraded."""
        return not self.unavailable

    @property
    def identity(self) -> str:
        """Content-addressed over the reading **and** the method that produced it.

        `method_version` participates, so revising the definition mints a new snapshot rather
        than relabelling every observation already grouped under the old one.
        """
        payload = {
            "method_version": self.method_version,
            "as_of_session": self.as_of_session.isoformat(),
            "benchmark_symbol": self.benchmark_symbol,
            "source": self.source,
            "trend": self.trend.value,
            "volatility": self.volatility.value,
            "breadth": self.breadth.value,
            "realised_volatility": (
                None if self.realised_volatility is None else str(self.realised_volatility)
            ),
            "participation": None if self.participation is None else str(self.participation),
            "unavailable": list(self.unavailable),
            "source_snapshot_ids": list(self.source_snapshot_ids),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"market-regime-v{self.method_version}-{digest[:16]}"

    def as_row(self) -> Mapping[str, object]:
        return {
            "snapshot_id": self.identity,
            "as_of_session": self.as_of_session.isoformat(),
            "benchmark_symbol": self.benchmark_symbol,
            "source": self.source,
            "method_version": self.method_version,
            "trend": self.trend.value,
            "volatility": self.volatility.value,
            "breadth": self.breadth.value,
            "realised_volatility": (
                None if self.realised_volatility is None else str(self.realised_volatility)
            ),
            "participation": None if self.participation is None else str(self.participation),
            "unavailable": " | ".join(self.unavailable),
            "source_snapshot_ids": ",".join(self.source_snapshot_ids),
        }


def _log_returns(closes: Sequence[Decimal]) -> list[float]:
    return [
        math.log(float(closes[index]) / float(closes[index - 1]))
        for index in range(1, len(closes))
        if closes[index] > 0 and closes[index - 1] > 0
    ]


def build_market_regime_snapshot(
    *,
    as_of_session: date,
    benchmark_symbol: str,
    source: str,
    benchmark_closes: Sequence[Decimal],
    universe_closes: Mapping[str, Sequence[Decimal]] | None = None,
    source_snapshot_ids: Sequence[str] = (),
    method_version: int = 1,
) -> MarketRegimeSnapshot:
    """Describe the market as of one session, from series that end at that session.

    Every input is a series the caller has already sliced to the decision point. Nothing here
    fetches, so look-ahead cannot enter through this function -- the same construction the
    anomaly detector uses, and for the same reason.

    Returns a snapshot in every case. An axis that cannot be computed is `UNAVAILABLE` with a
    reason rather than an exception, because a study conditioning on regime needs to know that a
    decision happened in unmeasurable conditions, which is different from it not happening.
    """
    unavailable: list[str] = []

    # --- trend ---------------------------------------------------------------------------
    if len(benchmark_closes) < TREND_SESSIONS:
        trend = TrendState.UNAVAILABLE
        unavailable.append(
            f"trend: {len(benchmark_closes)} benchmark sessions available and "
            f"{TREND_SESSIONS} required"
        )
    else:
        window = benchmark_closes[-TREND_SESSIONS:]
        average = sum(window, Decimal("0")) / Decimal(len(window))
        trend = TrendState.ABOVE if window[-1] >= average else TrendState.BELOW

    # --- volatility ----------------------------------------------------------------------
    realised: Decimal | None = None
    if len(benchmark_closes) < VOLATILITY_DISTRIBUTION_SESSIONS:
        volatility = VolatilityState.UNAVAILABLE
        unavailable.append(
            f"volatility: {len(benchmark_closes)} benchmark sessions available and "
            f"{VOLATILITY_DISTRIBUTION_SESSIONS} required to place one against its own history"
        )
    else:
        returns = _log_returns(benchmark_closes[-VOLATILITY_DISTRIBUTION_SESSIONS:])
        rolling = [
            statistics.pstdev(returns[index - VOLATILITY_SESSIONS : index])
            for index in range(VOLATILITY_SESSIONS, len(returns) + 1)
        ]
        current = rolling[-1]
        ordered = sorted(rolling)
        low = ordered[len(ordered) // 3]
        high = ordered[2 * len(ordered) // 3]
        realised = Decimal(str(round(current * math.sqrt(252), 6)))
        if current <= low:
            volatility = VolatilityState.CALM
        elif current >= high:
            volatility = VolatilityState.STRESSED
        else:
            volatility = VolatilityState.NORMAL

    # --- breadth -------------------------------------------------------------------------
    participation: Decimal | None = None
    usable = {
        symbol: series
        for symbol, series in (universe_closes or {}).items()
        if len(series) >= 2 and series[-1] > 0 and series[-2] > 0
    }
    if len(benchmark_closes) < 2 or len(usable) < 2:
        breadth = BreadthState.UNAVAILABLE
        unavailable.append(
            f"breadth: {len(usable)} usable universe series and at least 2 required alongside "
            "a benchmark move"
        )
    else:
        benchmark_move = float(benchmark_closes[-1]) - float(benchmark_closes[-2])
        if benchmark_move == 0:
            breadth = BreadthState.UNAVAILABLE
            unavailable.append(
                "breadth: the benchmark did not move, so there is no direction to participate in"
            )
        else:
            agreeing = sum(
                1
                for series in usable.values()
                if (float(series[-1]) - float(series[-2])) * benchmark_move > 0
            )
            participation = (Decimal(agreeing) / Decimal(len(usable))).quantize(
                Decimal("0.0001")
            )
            breadth = (
                BreadthState.BROAD
                if participation >= BROAD_PARTICIPATION
                else BreadthState.NARROW
            )

    return MarketRegimeSnapshot(
        method_version=method_version,
        as_of_session=as_of_session,
        benchmark_symbol=benchmark_symbol,
        source=source,
        trend=trend,
        volatility=volatility,
        breadth=breadth,
        realised_volatility=realised,
        participation=participation,
        unavailable=tuple(unavailable),
        source_snapshot_ids=tuple(sorted(set(source_snapshot_ids))),
    )


__all__ = [
    "BROAD_PARTICIPATION",
    "TREND_SESSIONS",
    "VOLATILITY_DISTRIBUTION_SESSIONS",
    "VOLATILITY_SESSIONS",
    "BreadthState",
    "MarketRegimeSnapshot",
    "TrendState",
    "VolatilityState",
    "build_market_regime_snapshot",
]
