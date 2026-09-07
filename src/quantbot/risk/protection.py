"""Protective exit evaluation that does not depend on the loop that opened the position (#157).

**The invariant:** once a position exists, safety-critical exit evaluation must not depend on the
code path that decided to enter it.

That invariant is currently broken, and not hypothetically. Every protective exit in this system
is produced by `SignalAction.EXIT` inside the strategy evaluation, which `RunOnceCycle.run` calls
*after* three halts:

    recovery.recover   -- HALT on reconciliation failure
    kill_switch.get    -- HALT on engaged
    data_sync.sync     -- HALT on stale / disconnected / unhealthy
    strategy.evaluate  <-- every exit decision is produced here

Two consequences, and the second is worse than the first.

A stopped daemon evaluates nothing. On 2026-08-20 the operator's store recorded seven open
positions; the last run was 2026-08-19 and nothing has run since, so those positions went eleven
days with no stop, target or drawdown evaluation.

And a *running* daemon that trips any of those three halts also evaluates nothing -- so protection
stops precisely when local and broker state disagree, when the switch is engaged, or when the
data went stale mid-session. The system is least protective exactly when it is least healthy, and
the 2026-08-19 `RECONCILIATION_FAILED` halt is a real instance of that path being taken with
positions open.

**So this deliberately does not reuse those gates.** Reconciliation state and price staleness are
*inputs* here rather than preconditions: they change the outcome, and the outcome they produce is
`BLOCKED_STATE` with a named reason, never silence. A protection that declines to answer when it
cannot see clearly is a protection; one that skips evaluation is an absence.

**It decides; it does not submit.** Every outcome is a typed record. Wiring an exit path and the
logic that decides when to fire it in a single change is how a protective exit becomes an
unintended one, and every regression test this issue asks for is about the decision rather than
the order. `risk/gates.py` already guarantees the other half -- a veto applies to `ENTRY` only, so
nothing here can be blocked by the portfolio governor.

**Nothing judgemental participates.** No model, no confidence score, no research artifact is an
input. The rules are arithmetic on a price, an entry and a policy, which is what makes the
independence claim checkable rather than asserted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from quantbot.domain import Position

#: How stale a price may be before it stops being usable for a protective decision. A
#: `PROJECT_DEFAULT` (#72): the deployed strategy trades daily bars, so a price from the previous
#: session is the ordinary case and one from a week ago is not. Four days covers a long weekend
#: plus a holiday, matching `STALE_RUN_DAYS` -- deliberately the same number, because a reader
#: comparing the two should not have to wonder why they differ.
#:
#: Chosen toward refusing: a `BLOCKED_STATE` costs an operator a look, and treating a week-old
#: price as current costs the exit the rule exists to trigger.
MAXIMUM_PRICE_AGE_DAYS = 4


class ProtectionOutcome(StrEnum):
    """What a protective evaluation concluded. Four states, and the third is the point."""

    #: Evaluated against a usable price, and no rule requires an exit.
    NO_ACTION = "NO_ACTION"
    #: A protective rule fired. The position should be closed through the authoritative path.
    EXIT_REQUIRED = "EXIT_REQUIRED"
    #: The state needed to decide safely could not be established -- no price, a stale one, or
    #: local and broker records that disagree. **Not** a silent pass: it is the honest answer
    #: when a protection cannot see, and it is what distinguishes this from skipping evaluation.
    BLOCKED_STATE = "BLOCKED_STATE"
    #: Something was wrong with the inputs themselves rather than with the position -- a
    #: nonsensical entry price, a quantity that cannot be squared. Attributable, never assumed
    #: safe.
    INCIDENT = "INCIDENT"


class ProtectionRule(StrEnum):
    """Which deterministic rule produced the outcome."""

    HARD_STOP = "HARD_STOP"
    ACCOUNT_DRAWDOWN = "ACCOUNT_DRAWDOWN"
    #: No rule fired, or none could be evaluated.
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class ProtectionDecision:
    """One position, one evaluation, and everything needed to audit it later."""

    symbol: str
    outcome: ProtectionOutcome
    rule: ProtectionRule
    #: Why, in terms an operator can act on. Always populated for anything but `NO_ACTION`.
    reason: str = ""
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    #: The price the decision rested on, or `None` when there was not a usable one.
    price: Decimal | None = None
    #: Position-relative return from entry, in basis points: losses are negative for either side.
    #: `None` when unmeasurable.
    move_bps: Decimal | None = None

    @property
    def requires_exit(self) -> bool:
        return self.outcome is ProtectionOutcome.EXIT_REQUIRED

    @property
    def protected(self) -> bool:
        """Whether this position can be said to be protected right now.

        `BLOCKED_STATE` and `INCIDENT` are both **False**. A position nobody could evaluate is
        not a protected one, and the whole failure this module exists for was an absence that
        read as safety.
        """
        return self.outcome in (ProtectionOutcome.NO_ACTION, ProtectionOutcome.EXIT_REQUIRED)

    def explain(self) -> str:
        if self.outcome is ProtectionOutcome.NO_ACTION:
            return f"{self.symbol}: NO_ACTION"
        return f"{self.symbol}: {self.outcome.value} ({self.rule.value}) -- {self.reason}"


@dataclass(frozen=True, slots=True)
class ProtectionPolicy:
    """The deterministic rules. Arithmetic only -- nothing here consults a model.

    Defaults are `PROJECT_DEFAULT` (#72) and deliberately loose: this ships evaluating and not
    submitting, so a rule that fires often costs a row and a console line, while one that never
    fires would make the whole supervisor look like it was working.
    """

    #: Exit when a position loses this much relative to entry, in basis points. 2000 = 20%.
    hard_stop_bps: Decimal = Decimal("2000")
    #: Exit everything when account drawdown reaches this, in basis points. Matches the config's
    #: existing 20% halt rather than inventing a second number.
    account_drawdown_exit_bps: Decimal = Decimal("2000")
    maximum_price_age_days: int = MAXIMUM_PRICE_AGE_DAYS

    def __post_init__(self) -> None:
        for name in ("hard_stop_bps", "account_drawdown_exit_bps"):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be a positive finite Decimal")
        if type(self.maximum_price_age_days) is not int or self.maximum_price_age_days < 0:
            raise ValueError("maximum_price_age_days must be a nonnegative integer")


def evaluate_position(
    position: Position,
    *,
    policy: ProtectionPolicy,
    price: Decimal | None,
    priced_at: datetime | None,
    observed_at: datetime,
    reconciled: bool,
    account_drawdown_bps: Decimal | None = None,
) -> ProtectionDecision:
    """Decide whether one open position requires a protective exit.

    Takes the price and the reconciliation state as **inputs** rather than as preconditions. That
    is the whole difference from the cycle's gates: a stale price or a broker mismatch changes
    the answer to `BLOCKED_STATE`, and does not cause the evaluation to be skipped.
    """
    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not position.quantity.is_finite() or position.quantity == 0:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.INCIDENT,
            rule=ProtectionRule.NONE,
            reason="position quantity must be finite and nonzero to evaluate an open position",
            observed_at=observed_at,
        )
    if not position.average_entry_price.is_finite() or position.average_entry_price <= 0:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.INCIDENT,
            rule=ProtectionRule.NONE,
            reason=(
                f"average entry price is {position.average_entry_price}; a move cannot be "
                "measured against it and assuming the position is protected would be a guess"
            ),
            observed_at=observed_at,
        )

    if not reconciled:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.BLOCKED_STATE,
            rule=ProtectionRule.NONE,
            reason=(
                "local and broker position state disagree; exiting on a quantity nobody has "
                "confirmed could close a position that is not there or leave one that is"
            ),
            observed_at=observed_at,
        )

    if price is None or priced_at is None:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.BLOCKED_STATE,
            rule=ProtectionRule.NONE,
            reason="no price is available, so no protective rule can be evaluated",
            observed_at=observed_at,
        )

    if not price.is_finite() or price <= 0:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.INCIDENT,
            rule=ProtectionRule.NONE,
            reason="market price must be positive and finite",
            observed_at=observed_at,
        )
    if priced_at.tzinfo is None or priced_at.utcoffset() is None:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.BLOCKED_STATE,
            rule=ProtectionRule.NONE,
            reason="price timestamp must be timezone-aware to establish freshness",
            observed_at=observed_at,
        )
    age = observed_at.astimezone(UTC) - priced_at.astimezone(UTC)
    if age < timedelta(0):
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.BLOCKED_STATE,
            rule=ProtectionRule.NONE,
            reason="price timestamp is in the future; freshness cannot be established",
            observed_at=observed_at,
        )
    if age > timedelta(days=policy.maximum_price_age_days):
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.BLOCKED_STATE,
            rule=ProtectionRule.NONE,
            reason=(
                f"the last price is {age.days} days old (elapsed {age}); "
                f"the maximum age is {policy.maximum_price_age_days} days"
            ),
            observed_at=observed_at,
            price=price,
        )

    if account_drawdown_bps is not None and (
        not account_drawdown_bps.is_finite() or account_drawdown_bps < 0
    ):
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.INCIDENT,
            rule=ProtectionRule.NONE,
            reason="account drawdown must be finite and nonnegative",
            observed_at=observed_at,
        )

    move = (price - position.average_entry_price) / position.average_entry_price * Decimal(10_000)
    if position.quantity < 0:
        move = -move

    # Account drawdown before the per-position stop: it closes everything, so reporting a single
    # symbol's stop as the reason would name the smaller cause.
    if (
        account_drawdown_bps is not None
        and account_drawdown_bps >= policy.account_drawdown_exit_bps
    ):
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.EXIT_REQUIRED,
            rule=ProtectionRule.ACCOUNT_DRAWDOWN,
            reason=(
                f"account drawdown {account_drawdown_bps}bps has reached the "
                f"{policy.account_drawdown_exit_bps}bps exit threshold"
            ),
            observed_at=observed_at,
            price=price,
            move_bps=move,
        )

    if move <= -policy.hard_stop_bps:
        return ProtectionDecision(
            symbol=position.symbol,
            outcome=ProtectionOutcome.EXIT_REQUIRED,
            rule=ProtectionRule.HARD_STOP,
            reason=(
                f"{position.symbol} is {move}bps against entry, past the "
                f"{-policy.hard_stop_bps}bps hard stop"
            ),
            observed_at=observed_at,
            price=price,
            move_bps=move,
        )

    return ProtectionDecision(
        symbol=position.symbol,
        outcome=ProtectionOutcome.NO_ACTION,
        rule=ProtectionRule.NONE,
        observed_at=observed_at,
        price=price,
        move_bps=move,
    )


def evaluate_positions(
    positions: Sequence[Position],
    *,
    policy: ProtectionPolicy,
    prices: Mapping[str, tuple[Decimal, datetime]],
    observed_at: datetime,
    reconciled: bool,
    account_drawdown_bps: Decimal | None = None,
) -> tuple[ProtectionDecision, ...]:
    """Evaluate every open position. One decision each, always -- never a filtered list.

    A position that could not be evaluated produces a `BLOCKED_STATE` row rather than being
    omitted, because the count of decisions has to match the count of positions for "every open
    position is covered by a measurable protection state" to mean anything.
    """
    decisions = []
    for position in positions:
        priced = prices.get(position.symbol)
        decisions.append(
            evaluate_position(
                position,
                policy=policy,
                price=None if priced is None else priced[0],
                priced_at=None if priced is None else priced[1],
                observed_at=observed_at,
                reconciled=reconciled,
                account_drawdown_bps=account_drawdown_bps,
            )
        )
    return tuple(decisions)


def unprotected(decisions: Sequence[ProtectionDecision]) -> tuple[ProtectionDecision, ...]:
    """Positions nobody could evaluate. Empty means every position has a real answer."""
    return tuple(decision for decision in decisions if not decision.protected)


__all__ = [
    "MAXIMUM_PRICE_AGE_DAYS",
    "ProtectionDecision",
    "ProtectionOutcome",
    "ProtectionPolicy",
    "ProtectionRule",
    "evaluate_position",
    "evaluate_positions",
    "unprotected",
]
