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

**The window is a list of sessions, not a count of them (#84).** The first version of this module
admitted a symbol when it held at least `lookback_sessions` observations and then let each symbol
choose its own most recent that many dates. Two symbols could satisfy that with *different*
sessions -- one missing a recent bar and carrying an extra older one -- and `_pearson` would zip
the vectors positionally, correlating Tuesday against Wednesday for the whole window. The counts
matched, so nothing raised; the coefficient came back to six decimal places and fed a hard veto.

The comment that used to sit above the return calculation said "every surviving symbol shares the
same session count, so every coefficient rests on the same sample". Same count is not the same
sample, and writing the invariant down did not establish it. So the snapshot now *carries* the
exact ordered `session_window` it was computed over, membership is `required <= set(series)`
against those precise dates, and every vector is built by indexing that tuple in order. The
window is in the identity, which means two snapshots over different sessions can no longer
collide, and a misaligned snapshot is no longer constructible rather than merely untested.

**No pairwise-complete missing-data rule, deliberately.** It computes each coefficient on whatever
sample that pair happens to share, so one cluster can rest on 120 sessions and another on 12 while
both report three decimal places. A symbol without the full window is excluded and named.

**"No coefficient" means one thing.** A symbol can fail to be measured for two unrelated reasons,
and the first version let them look identical: an incomplete window put a symbol in
`excluded_symbols`, while a constant price series simply produced no pair and vanished from
`symbols` with nothing recording why. `excluded` now maps every dropped symbol to its
`ExclusionReason`, and a validator requires the surviving symbols to form a *complete* coefficient
graph -- so a missing pair is a construction error rather than an ambiguity a reader has to
resolve.
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

    #: A symbol without an observation for **every session in the window** -- those exact dates,
    #: not that many dates -- is excluded and named. See the module docstring for the defect this
    #: wording used to describe without enforcing.
    REQUIRE_COMPLETE = "REQUIRE_COMPLETE"


class LinkageMethod(StrEnum):
    """How pairwise coefficients become clusters."""

    #: Every pair inside a cluster must clear the threshold. See the module docstring for the
    #: measurement that ruled out the alternative.
    COMPLETE = "COMPLETE"


class ExclusionReason(StrEnum):
    """Why a symbol offered to the builder carries no coefficient.

    Two different facts. A symbol the feed has not covered for the whole window may be perfectly
    well behaved; a symbol whose price never moved has been observed completely and still cannot
    be correlated with anything. Both refuse at the veto, and an operator reading a refusal is
    owed the difference.
    """

    #: The symbol lacks an observation for at least one session in the window.
    INCOMPLETE_WINDOW = "INCOMPLETE_WINDOW"
    #: The symbol's return series has zero spread, so correlation is undefined rather than zero.
    NON_VARYING = "NON_VARYING"


class DependenceSnapshot(BaseModel):
    """Pairwise dependence over one exact session window, frozen and content-addressed.

    Everything that changes a coefficient is a field, and every field is in `identity`. Sol's
    requirement on #58 and #59: a veto's basis has to be nameable later, and two studies across a
    method, a window or a session set are two studies.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method_version: int = Field(default=1, ge=1)
    linkage: LinkageMethod = LinkageMethod.COMPLETE
    return_definition: ReturnDefinition = ReturnDefinition.CLOSE_TO_CLOSE_LOG
    missing_data_rule: MissingDataRule = MissingDataRule.REQUIRE_COMPLETE
    #: Where the price series came from, so a provider change is a new snapshot.
    source: str = Field(min_length=1)
    #: The exact ordered sessions every coefficient was computed over (#84). Not a count and not
    #: a range: the dates themselves, so "the same sample" is a property of the object rather
    #: than a claim about it.
    session_window: tuple[date, ...]
    #: Coefficients keyed `"A|B"` with the pair sorted, so one entry exists per pair.
    coefficients: Mapping[str, Decimal]
    #: Symbols offered to the builder and dropped, each with why. Named, because a silently
    #: smaller universe is a silently weaker claim.
    excluded: Mapping[str, ExclusionReason] = {}

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
        if len(self.session_window) < 2:
            raise ValueError("a correlation window needs at least two sessions")
        if list(self.session_window) != sorted(set(self.session_window)):
            raise ValueError(
                "session_window must be strictly ascending and free of duplicates; an unordered "
                "or repeated window cannot describe the sample a coefficient was computed over"
            )
        for key in self.coefficients:
            left, _, right = key.partition("|")
            if not left or not right or left >= right:
                raise ValueError(
                    f"{key!r} is not a sorted symbol pair; an unsorted or duplicated key would "
                    "let one pair carry two coefficients"
                )
        measured = self.symbols
        expected_pairs = len(measured) * (len(measured) - 1) // 2
        if len(self.coefficients) != expected_pairs:
            # Every symbol that survives the builder varies and holds the whole window, so every
            # pair of survivors is computable. An absent pair would therefore be a third, unnamed
            # kind of "not measured" -- the ambiguity `excluded` exists to remove.
            raise ValueError(
                f"{len(measured)} measured symbols imply {expected_pairs} pairs but "
                f"{len(self.coefficients)} are present; a missing pair is a third meaning of "
                '"unmeasured" that no reason field records'
            )
        overlap = measured & set(self.excluded)
        if overlap:
            raise ValueError(
                f"{sorted(overlap)} appear both measured and excluded, so whether they count "
                "toward a cluster depends on which field the reader consults"
            )
        return self

    @property
    def symbols(self) -> frozenset[str]:
        """Symbols that were actually measured. Everything else is in `excluded` with a reason."""
        found: set[str] = set()
        for key in self.coefficients:
            left, _, right = key.partition("|")
            found.update({left, right})
        return frozenset(found)

    @property
    def as_of_session(self) -> date:
        """The window's last session. Derived, so it cannot disagree with the window."""
        return self.session_window[-1]

    @property
    def lookback_sessions(self) -> int:
        return len(self.session_window)

    @property
    def observations_used(self) -> int:
        """Returns, which is one fewer than sessions. Derived for the same reason."""
        return len(self.session_window) - 1

    @property
    def identity(self) -> str:
        payload = {
            "method_version": self.method_version,
            "linkage": self.linkage.value,
            "return_definition": self.return_definition.value,
            "missing_data_rule": self.missing_data_rule.value,
            "source": self.source,
            # The whole window, not its endpoints. Two snapshots ending on the same session over
            # different sessions are different evidence and must not share an identity (#84).
            "session_window": [day.isoformat() for day in self.session_window],
            "coefficients": {key: str(value) for key, value in sorted(self.coefficients.items())},
            "excluded": {
                symbol: reason.value for symbol, reason in sorted(self.excluded.items())
            },
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
    session_window: Sequence[date],
    source: str,
) -> DependenceSnapshot | None:
    """Compute a snapshot over one exact session window, or `None` when nothing survives it.

    `session_window` is the authoritative point-in-time session sequence -- read from the
    exchange calendar by the caller, not inferred from whichever dates the price series happen
    to contain. Deriving it from the data would let a feed outage silently redefine the window
    it is supposed to be measured against, which is the shape of defect #84 reported.

    `None` rather than a partial answer when fewer than two symbols hold the whole window: a
    correlation over eleven sessions and one over a hundred are not the same evidence, and a
    caller that cannot tell them apart will treat them the same. The veto refuses on `None`.
    """
    window = tuple(session_window)
    if len(window) < 2:
        raise ValueError("a correlation window needs at least two sessions")

    required = set(window)
    excluded: dict[str, ExclusionReason] = {}
    returns: dict[str, list[float]] = {}
    for symbol, series in closes.items():
        if not required <= set(series):
            excluded[symbol] = ExclusionReason.INCOMPLETE_WINDOW
            continue
        # Indexed by the window in order, so index N is the same session for every symbol.
        prices = [float(series[day]) for day in window]
        returns[symbol] = [
            math.log(prices[index] / prices[index - 1]) for index in range(1, len(prices))
        ]

    # Spread is computed once per symbol and reused by every pair, so "this symbol cannot be
    # correlated" is decided in exactly one place. Deciding it inside the pairwise loop instead
    # would make a degenerate symbol vanish from some pairs and not others.
    measured: dict[str, tuple[list[float], float, float]] = {}
    for symbol, values in returns.items():
        mean = statistics.fmean(values)
        spread = math.sqrt(sum((value - mean) ** 2 for value in values))
        if spread == 0:
            # Zero would say "independent"; the truth is "unanswerable", and they differ at the
            # veto -- one permits an order and the other refuses it.
            excluded[symbol] = ExclusionReason.NON_VARYING
        else:
            measured[symbol] = (values, mean, spread)

    if len(measured) < 2:
        return None

    coefficients: dict[str, Decimal] = {}
    ordered = sorted(measured)
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            coefficients["|".join((left, right))] = _pearson(measured[left], measured[right])

    return DependenceSnapshot(
        source=source,
        session_window=window,
        coefficients=coefficients,
        excluded=excluded,
    )


def _pearson(
    left: tuple[list[float], float, float], right: tuple[list[float], float, float]
) -> Decimal:
    """Correlation from pre-computed means and spreads.

    Total by construction: a symbol whose spread is zero never reaches here, because the builder
    excluded it as `NON_VARYING` using this same quantity.
    """
    values_a, mean_a, spread_a = left
    values_b, mean_b, spread_b = right
    covariance = sum(
        (x - mean_a) * (y - mean_b) for x, y in zip(values_a, values_b, strict=True)
    )
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
        (abs(position.market_value) for position in positions if position.symbol in cluster),
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
    "ExclusionReason",
    "LinkageMethod",
    "MissingDataRule",
    "ReturnDefinition",
    "build_dependence_snapshot",
    "build_dependence_state",
]
