"""Staggered rebalance scheduling, so the roster date stops being a coin flip.

Running the identical strategy on a different day of the month produced terminal wealth
between $246.38 and $307.61 per $100 over the research history — a $61.24 spread decided by
nothing but the calendar. The deployed system holds one arbitrary draw from that distribution.
Tranching does not raise the expected return; it removes the gamble. See
docs/tranching-spec.md for the measurements that settled the design.

This module is deliberately pure: scheduling and weights only, no roster construction, no I/O.
The scheduling rule is the part where an off-by-one silently changes which day a real order is
placed, so it is isolated here where it can be tested exhaustively.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

#: Alpaca rejects orders below this notional, which is what bounds the tranche count at small
#: account sizes: a tranche slice of a position must still be orderable.
MINIMUM_ORDER_NOTIONAL = Decimal("1")


def tranche_schedule(session_months: Sequence[str], tranches: int) -> dict[int, int]:
    """Map each session index to the tranche that rebalances on it.

    ``session_months`` is one ``YYYY-MM`` string per session, in order. Sessions that are not a
    rebalance date for any tranche are absent from the result.

    With ``tranches=1`` this returns exactly the final session of each month, which is the
    behaviour every deployed version has. That equivalence is the reason the generalisation is
    safe: enabling tranching is a real change, but leaving it off changes nothing.
    """
    if tranches < 1:
        raise ValueError("tranches must be at least 1")
    schedule: dict[int, int] = {}
    start = 0
    for index in range(len(session_months) + 1):
        ended = index == len(session_months) or (
            index > 0 and session_months[index] != session_months[start]
        )
        if not ended:
            continue
        count = index - start
        if count:
            for tranche in range(tranches):
                # The last tranche always lands on the month's final session, so a single
                # tranche reproduces month-end rebalancing exactly.
                offset = round((tranche + 1) * count / tranches) - 1
                schedule[start + max(0, min(count - 1, offset))] = tranche
        start = index
    return schedule


def tranche_weights(
    holdings: Sequence[Sequence[str]], full_weight: Decimal
) -> dict[str, Decimal]:
    """Blend per-tranche rosters into fractional target weights.

    ``holdings[k]`` is the symbol list tranche *k* currently holds. A symbol held by 3 of 5
    tranches targets three fifths of a full position. Membership stops being binary, which is
    the whole mechanism — it is what averages away the choice of rebalance date.
    """
    if not holdings:
        return {}
    tranches = Decimal(len(holdings))
    counts: dict[str, int] = {}
    for roster in holdings:
        for symbol in set(roster):  # a tranche holding a name twice still counts once
            counts[symbol] = counts.get(symbol, 0) + 1
    return {
        symbol: full_weight * Decimal(count) / tranches for symbol, count in sorted(counts.items())
    }


def max_supportable_tranches(
    equity: Decimal, full_weight: Decimal, *, ceiling: int = 21
) -> int:
    """Largest tranche count whose slice of a full position still clears the broker minimum.

    This is a capital constraint, not a strategy one. At $100 with a 10% position cap a fifth
    of a position is $2.00 and fine, while a twenty-first is $0.48 and would be rejected. The
    limit relaxes as the account grows, so deriving it from equity means the system widens its
    own tranching automatically instead of needing a config change.
    """
    if equity <= 0 or full_weight <= 0:
        return 1
    position_value = equity * full_weight
    if position_value <= 0:
        return 1
    supportable = int(position_value / MINIMUM_ORDER_NOTIONAL)
    return max(1, min(ceiling, supportable))
