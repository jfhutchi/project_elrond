"""A frozen point-in-time picture of what moves together (#59 slice 2, feeding #58).

`EXPOSURE_CONCENTRATION` asks what instruments *are*, read off their mandates. This asks what they
have recently *done*, which is a different and weaker claim needing a different kind of evidence:
a coefficient computed from a window, over a return definition, under a missing-data rule, by a
method. All four change the number, so all four are part of the object's identity. A correlation
matrix reconstructed later is not the one a veto had at decision time.

**Complete linkage, and the measurement is why.** The obvious reading of "cluster symbols above a
correlation threshold" is connected components of the qualifying graph -- single linkage. On this
universe that chains catastrophically. Measured over 120 sessions of daily log returns, a 0.80
threshold under single linkage produces one cluster of thirteen symbols containing `TLT`, `IEF`
and `TIP` alongside `SPY`, `QQQ` and `XLK`, because:

    corr(TLT, SPY) = +0.445      nothing links these directly
    corr(HYG, SPY) = +0.817      but this does, one way
    corr(LQD, TLT) = +0.900      and this does, the other

Single linkage needs one qualifying edge per join, so Treasuries reach equities through credit.
A limit built on that reports a bond position as adding to equity concentration, and at 0.70 it
treats two thirds of the universe as one bet. That is not conservatism, it is a wrong answer that
happens to refuse -- the hardest kind to notice.

Requiring **every** pair in a cluster to clear the threshold makes a cluster mean what the veto
needs it to mean. The same data then yields `{IEF, LQD, TLT}` and `{EEM, EFA, SPY}`: duration, and
global equity beta.

**No pairwise-complete missing-data rule, deliberately.** It computes each coefficient on whatever
sample that pair happens to share, so one cluster can rest on 120 sessions and another on 12 while
both report three decimal places. A symbol without full history is excluded and named.
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
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.domain import Position


class ReturnDefinition(StrEnum):
    """How a price series becomes a return series. Changes the coefficient, so it is recorded."""

    #: Log of the close-to-close ratio. Additive across time, which is what a correlation over a
    #: window wants.
    CLOSE_TO_CLOSE_LOG = "CLOSE_TO_CLOSE_LOG"


class MissingDataRule(StrEnum):
    """What to do about a symbol that lacks the full window.

    `PAIRWISE_COMPLETE` is deliberately absent. It is the convenient choice and it makes every
    coefficient rest on a different sample, which a single matrix then presents as comparable.
    """

    #: A symbol without an observation for every session in the window is excluded and named.
    REQUIRE_COMPLETE = "REQUIRE_COMPLETE"


class LinkageMethod(StrEnum):
    """How pairwise coefficients become clusters."""

    #: Every pair inside a cluster must clear the threshold. See the module docstring for the
    #: measurement that ruled out the alternative.
    COMPLETE = "COMPLETE"


class DependenceSnapshot(BaseModel):
    """Pairwise dependence as of one session, frozen and content-addressed.

    Everything that changes a coefficient is a field, and every field is in `identity`. Sol's
    requirement on #58 and #59: a veto's basis has to be nameable later, and two studies across a
    method or window change are two studies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method_version: int = Field(default=1, ge=1)
    linkage: LinkageMethod = LinkageMethod.COMPLETE
    return_definition: ReturnDefinition = ReturnDefinition.CLOSE_TO_CLOSE_LOG
    missing_data_rule: MissingDataRule = MissingDataRule.REQUIRE_COMPLETE
    #: Where the price series came from, so a provider change is a new snapshot.
    source: str = Field(min_length=1)
    #: The session this was computed as of. Not a wall-clock time: the window is sessions.
    as_of_session: date
    #: Sessions of history requested, and returns actually used. The second is the first minus
    #: one, and it is recorded rather than inferred so a reader never has to remember which.
    lookback_sessions: int = Field(ge=2)
    observations_used: int = Field(ge=1)
    #: Coefficients keyed `"A|B"` with the pair sorted, so one entry exists per pair.
    coefficients: Mapping[str, Decimal]
    #: Symbols dropped for incomplete history. Named, because a silently smaller universe is a
    #: silently weaker claim.
    excluded_symbols: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        for key in self.coefficients:
            left, _, right = key.partition("|")
            if not left or not right or left >= right:
                raise ValueError(
                    f"{key!r} is not a sorted symbol pair; an unsorted or duplicated key would "
                    "let one pair carry two coefficients"
                )
        return self

    @property
    def symbols(self) -> frozenset[str]:
        found: set[str] = set()
        for key in self.coefficients:
            left, _, right = key.partition("|")
            found.update({left, right})
        return frozenset(found)

    @property
    def identity(self) -> str:
        payload = {
            "method_version": self.method_version,
            "linkage": self.linkage.value,
            "return_definition": self.return_definition.value,
            "missing_data_rule": self.missing_data_rule.value,
            "source": self.source,
            "as_of_session": self.as_of_session.isoformat(),
            "lookback_sessions": self.lookback_sessions,
            "observations_used": self.observations_used,
            "coefficients": {key: str(value) for key, value in sorted(self.coefficients.items())},
            "excluded_symbols": sorted(self.excluded_symbols),
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"dependence-v{self.method_version}-{digest[:16]}"

    def coefficient(self, left: str, right: str) -> Decimal | None:
        """The pair's coefficient, or `None` when it was not computed.

        A symbol compared with itself is 1 by definition. Anything else absent means one of the
        two was excluded, and the caller must treat that as unmeasured rather than uncorrelated.
        """
        if left == right:
            return Decimal("1")
        key = "|".join(sorted((left, right)))
        return self.coefficients.get(key)

    def clusters(self, symbols: Sequence[str], *, threshold: Decimal) -> list[list[str]]:
        """Group symbols so that **every** pair inside a group clears `threshold`.

        Assignment order is sorted and the first accepting cluster wins, so the result is
        deterministic. That ordering is part of the method rather than an accident of input
        order, which is why `linkage` is in the identity.
        """
        clusters: list[list[str]] = []
        for symbol in sorted(set(symbols)):
            for cluster in clusters:
                if all(
                    (self.coefficient(symbol, member) or Decimal("-1")) >= threshold
                    for member in cluster
                ):
                    cluster.append(symbol)
                    break
            else:
                clusters.append([symbol])
        return clusters


def build_dependence_snapshot(
    *,
    closes: Mapping[str, Mapping[date, Decimal]],
    as_of_session: date,
    lookback_sessions: int,
    source: str,
) -> DependenceSnapshot | None:
    """Compute a snapshot from stored closes, or `None` when the window cannot be filled.

    `None` rather than a partial answer: a correlation over eleven sessions and one over a
    hundred are not the same evidence, and a caller that cannot tell them apart will treat them
    the same. The veto refuses on `None`.
    """
    if lookback_sessions < 2:
        raise ValueError("a correlation window needs at least two sessions")

    usable = {
        symbol: series
        for symbol, series in closes.items()
        if len([day for day in series if day <= as_of_session]) >= lookback_sessions
    }
    excluded = tuple(sorted(set(closes) - set(usable)))
    if len(usable) < 2:
        return None

    window: dict[str, list[Decimal]] = {}
    for symbol, series in usable.items():
        days = sorted(day for day in series if day <= as_of_session)[-lookback_sessions:]
        window[symbol] = [series[day] for day in days]

    # Every surviving symbol shares the same session count, so every coefficient rests on the
    # same sample -- which is the whole point of REQUIRE_COMPLETE.
    returns = {
        symbol: [
            Decimal(str(math.log(float(prices[index]) / float(prices[index - 1]))))
            for index in range(1, len(prices))
        ]
        for symbol, prices in window.items()
    }
    observations = len(next(iter(returns.values())))
    if observations < 1:
        return None

    coefficients: dict[str, Decimal] = {}
    ordered = sorted(returns)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            value = _pearson(returns[left], returns[right])
            if value is not None:
                coefficients["|".join((left, right))] = value

    return DependenceSnapshot(
        source=source,
        as_of_session=as_of_session,
        lookback_sessions=lookback_sessions,
        observations_used=observations,
        coefficients=coefficients,
        excluded_symbols=excluded,
    )


def _pearson(left: Sequence[Decimal], right: Sequence[Decimal]) -> Decimal | None:
    """Correlation, or `None` when a series does not vary.

    A constant series has no correlation with anything, and reporting zero would say "these are
    independent" when the truth is "this cannot be answered".
    """
    a = [float(value) for value in left]
    b = [float(value) for value in right]
    mean_a, mean_b = statistics.fmean(a), statistics.fmean(b)
    spread_a = math.sqrt(sum((value - mean_a) ** 2 for value in a))
    spread_b = math.sqrt(sum((value - mean_b) ** 2 for value in b))
    if spread_a == 0 or spread_b == 0:
        return None
    covariance = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b, strict=True))
    return Decimal(str(round(covariance / (spread_a * spread_b), 6)))




class DependenceState(BaseModel):
    """The cluster a proposed order joins, and what is already committed to it.

    Assembled from measured positions rather than asserted, like `ExposureState`. A held symbol
    the snapshot could not measure is named: without it the cluster total is an understatement,
    and an understated numerator is the permissive direction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_identity: str
    threshold: Decimal
    #: The cluster the order's symbol belongs to, itself included. `None` when the snapshot has
    #: no coefficient for that symbol at all.
    order_cluster: tuple[str, ...] | None
    #: Absolute market value already held across that cluster.
    committed_in_cluster: Decimal
    #: Held or ordered symbols the snapshot could not measure.
    unmeasured: tuple[str, ...]


def build_dependence_state(
    *,
    snapshot: DependenceSnapshot,
    symbol: str,
    positions: Sequence[Position],
    threshold: Decimal,
) -> DependenceState:
    """Cluster the held book with the proposed symbol and total what shares its cluster.

    Absolute market value, for the reason exposure concentration uses it: netting a long against
    a short inside one cluster reports the concentration away.
    """
    held = {str(position.symbol) for position in positions}
    universe = sorted(held | {symbol})
    unmeasured = tuple(
        sorted(name for name in universe if name not in snapshot.symbols and name != symbol)
    )

    if symbol not in snapshot.symbols:
        return DependenceState(
            snapshot_identity=snapshot.identity,
            threshold=threshold,
            order_cluster=None,
            committed_in_cluster=Decimal("0"),
            unmeasured=tuple(sorted({*unmeasured, symbol})),
        )

    measured = [name for name in universe if name in snapshot.symbols]
    clusters = snapshot.clusters(measured, threshold=threshold)
    cluster = next(group for group in clusters if symbol in group)
    committed = sum(
        (
            abs(position.market_value)
            for position in positions
            if position.symbol in cluster
        ),
        Decimal("0"),
    )
    return DependenceState(
        snapshot_identity=snapshot.identity,
        threshold=threshold,
        order_cluster=tuple(cluster),
        committed_in_cluster=committed,
        unmeasured=unmeasured,
    )


__all__ = [
    "DependenceSnapshot",
    "DependenceState",
    "LinkageMethod",
    "MissingDataRule",
    "ReturnDefinition",
    "build_dependence_snapshot",
    "build_dependence_state",
]
