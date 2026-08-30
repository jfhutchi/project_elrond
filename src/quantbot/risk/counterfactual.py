"""What a refusal cost, measured only after the horizon it declared (#57 phase 2).

Phase 1 made refusals durable: which rule refused, under which parameters, on which evidence.
This is the other half -- what would have happened -- and it is the half where a research system
most easily fools itself, so almost everything here is a restriction.

**The execution policy is frozen before any outcome exists.** Entry reference, horizon, cost
model, benchmark convention: all fixed in a content-addressed object, and the outcome names it.
Choosing them after seeing returns is how a hypothetical fill becomes whatever the analyst
needed. The policy is therefore an input to evaluation, never a parameter of it.

**Outcomes are append-only and never touch the refusal.** The original `RejectedCandidate` is
what was known at decision time and stays that way forever. Hindsight arrives as a new row that
references it. Rewriting the refusal would destroy the only record of what the system actually
knew when it decided.

**Evaluation happens only after the declared horizon has elapsed.** Before that the answer is
`UNAVAILABLE` -- not yet, rather than never. A missing, stale or ambiguous future price makes it
`UNEVALUABLE`, which is terminal and distinct. Neither ever becomes a guessed fill or a zero
return, because a zero is a measurement and these are absences of one.

**A hypothetical outcome can never become an order.** Nothing here carries a quantity, a client
order id or a side an execution path would recognise, and the module is structurally unable to
import one -- the same boundary `shadow.py` holds, checked the same way.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.risk.shadow import CandidateDirection, CounterfactualState

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EntryReference(StrEnum):
    """Which price a refused candidate is assumed to have been filled at."""

    #: The close of the session the decision was made in. The only reference this project can
    #: honestly support: it stores daily bars, so an intraday fill would be an invention.
    DECISION_SESSION_CLOSE = "DECISION_SESSION_CLOSE"


class BenchmarkConvention(StrEnum):
    """What the hypothetical return is measured against."""

    #: Return minus the benchmark's return over the same sessions. A raw return would credit a
    #: refusal for market direction it had no view on.
    BENCHMARK_RELATIVE = "BENCHMARK_RELATIVE"


class UnevaluableReason(StrEnum):
    """Why an elapsed horizon still produced no outcome. Terminal, and never a zero."""

    #: No price exists at the horizon session for this symbol.
    HORIZON_PRICE_MISSING = "HORIZON_PRICE_MISSING"
    #: The benchmark's price is missing, so a relative return cannot be formed.
    BENCHMARK_PRICE_MISSING = "BENCHMARK_PRICE_MISSING"
    #: A price exists but is not usable -- non-positive, or from the wrong session.
    PRICE_AMBIGUOUS = "PRICE_AMBIGUOUS"


class HypotheticalExecutionPolicy(BaseModel):
    """How a refused candidate is assumed to have executed. Frozen before any outcome exists.

    Every field changes the number, so every field is in `identity`. Two outcomes computed under
    different policies are two datasets, and #57's whole purpose -- asking whether a gate added
    value -- is defeated the moment they are pooled.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(default=1, ge=1)
    entry_reference: EntryReference = EntryReference.DECISION_SESSION_CLOSE
    benchmark_convention: BenchmarkConvention = BenchmarkConvention.BENCHMARK_RELATIVE
    benchmark_symbol: Text = "SPY"
    #: Sessions from the decision to the measurement. Declared in advance; an outcome may not be
    #: evaluated before it elapses.
    horizon_sessions: int = Field(default=20, ge=1)
    #: Round-trip cost charged against the hypothetical, in basis points. A counterfactual that
    #: ignored costs would credit every refusal with a frictionless fill and systematically
    #: overstate what the gate gave up.
    round_trip_cost_bps: Decimal = Field(default=Decimal("10"), ge=0)

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"counterfactual-policy-v{self.version}-{digest[:16]}"


class CounterfactualOutcome(BaseModel):
    """One measured answer to "what did this refusal cost", or a recorded reason there is none.

    Append-only: it references the observation rather than amending it, so the refusal keeps
    saying what was known when it was made.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: Text
    policy_identity: Text
    state: CounterfactualState
    #: The session the horizon landed on. Present whenever one was reached, including when the
    #: price there turned out to be unusable.
    horizon_session: date | None = None
    evaluated_at: datetime | None = None
    #: Benchmark-relative return in basis points, after costs. Only ever set on `EVALUATED`.
    net_excess_bps: Decimal | None = None
    gross_return_bps: Decimal | None = None
    benchmark_return_bps: Decimal | None = None
    unevaluable_reason: UnevaluableReason | None = None
    detail: Text

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        measured = (self.net_excess_bps, self.gross_return_bps, self.benchmark_return_bps)
        if self.state is CounterfactualState.EVALUATED:
            if any(value is None for value in measured):
                raise ValueError(
                    "an evaluated outcome must carry its gross, benchmark and net figures; a "
                    "partial one invites a reader to infer the missing piece"
                )
            if self.horizon_session is None or self.evaluated_at is None:
                raise ValueError(
                    "an evaluated outcome must say which session it landed on, and when"
                )
            if self.unevaluable_reason is not None:
                raise ValueError("an evaluated outcome cannot also carry a reason it could not be")
        else:
            if any(value is not None for value in measured):
                raise ValueError(
                    f"{self.state.value} carries a return; an unevaluated outcome must not hold "
                    "a number a reader could quote as a measurement"
                )
        if self.state is CounterfactualState.UNEVALUABLE and self.unevaluable_reason is None:
            raise ValueError(
                "a terminal UNEVALUABLE must say why; without one it is indistinguishable from "
                "an outcome nobody has got to yet"
            )
        if self.state is CounterfactualState.UNAVAILABLE and self.unevaluable_reason is not None:
            raise ValueError(
                "UNAVAILABLE means not yet, so it cannot carry a terminal reason it never will be"
            )
        return self

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"counterfactual-outcome-{digest[:16]}"

    def as_row(self) -> Mapping[str, object]:
        return {
            "outcome_id": self.identity,
            "observation_id": self.observation_id,
            "policy_identity": self.policy_identity,
            "state": self.state.value,
            "horizon_session": (
                None if self.horizon_session is None else self.horizon_session.isoformat()
            ),
            "evaluated_at": (
                None if self.evaluated_at is None else self.evaluated_at.isoformat()
            ),
            "net_excess_bps": None if self.net_excess_bps is None else str(self.net_excess_bps),
            "gross_return_bps": (
                None if self.gross_return_bps is None else str(self.gross_return_bps)
            ),
            "benchmark_return_bps": (
                None if self.benchmark_return_bps is None else str(self.benchmark_return_bps)
            ),
            "unevaluable_reason": (
                None if self.unevaluable_reason is None else self.unevaluable_reason.value
            ),
            "detail": self.detail,
        }


def horizon_session_for(
    *, decision_session: date, sessions: Sequence[date], horizon_sessions: int
) -> date | None:
    """The session the horizon lands on, or `None` when the calendar has not reached it.

    Counted in **sessions** rather than days, for the reason #74 established for freshness: a
    calendar week and a trading week are different lengths, and a horizon measured in days drifts
    across holidays.
    """
    ordered = sorted(sessions)
    if decision_session not in ordered:
        return None
    index = ordered.index(decision_session)
    target = index + horizon_sessions
    return ordered[target] if target < len(ordered) else None


def evaluate_counterfactual(
    *,
    observation_id: str,
    direction: CandidateDirection,
    reference_price: Decimal,
    decision_session: date,
    policy: HypotheticalExecutionPolicy,
    sessions: Sequence[date],
    symbol_closes: Mapping[date, Decimal],
    benchmark_closes: Mapping[date, Decimal],
    evaluated_at: datetime,
) -> CounterfactualOutcome:
    """Measure what a refusal cost, but only once its declared horizon has elapsed.

    Returns `UNAVAILABLE` while the horizon is still ahead -- not yet, rather than never -- and
    `UNEVALUABLE` with a named reason when the horizon arrived and the prices did not. Neither
    is ever a zero: a zero return is a measurement, and these are the absence of one.
    """
    horizon = horizon_session_for(
        decision_session=decision_session,
        sessions=sessions,
        horizon_sessions=policy.horizon_sessions,
    )
    if horizon is None:
        return CounterfactualOutcome(
            observation_id=observation_id,
            policy_identity=policy.identity,
            state=CounterfactualState.UNAVAILABLE,
            detail=(
                f"the {policy.horizon_sessions}-session horizon from "
                f"{decision_session.isoformat()} has not elapsed; this is not yet rather than "
                "never, and no outcome may be computed before it"
            ),
        )

    def unevaluable(reason: UnevaluableReason, detail: str) -> CounterfactualOutcome:
        return CounterfactualOutcome(
            observation_id=observation_id,
            policy_identity=policy.identity,
            state=CounterfactualState.UNEVALUABLE,
            horizon_session=horizon,
            evaluated_at=evaluated_at,
            unevaluable_reason=reason,
            detail=detail,
        )

    exit_price = symbol_closes.get(horizon)
    if exit_price is None:
        return unevaluable(
            UnevaluableReason.HORIZON_PRICE_MISSING,
            f"no price exists for the {horizon.isoformat()} session, so the hypothetical has no "
            "exit; substituting the last known price would report a stale number as a result",
        )
    if exit_price <= 0 or reference_price <= 0:
        return unevaluable(
            UnevaluableReason.PRICE_AMBIGUOUS,
            f"a non-positive price at {horizon.isoformat()} cannot form a return",
        )
    entry_benchmark = benchmark_closes.get(decision_session)
    exit_benchmark = benchmark_closes.get(horizon)
    if entry_benchmark is None or exit_benchmark is None or entry_benchmark <= 0:
        return unevaluable(
            UnevaluableReason.BENCHMARK_PRICE_MISSING,
            f"{policy.benchmark_symbol} has no usable price across "
            f"{decision_session.isoformat()}..{horizon.isoformat()}, and a raw return would "
            "credit this refusal for market direction it had no view on",
        )

    basis = Decimal("10000")
    gross = (exit_price - reference_price) / reference_price * basis
    benchmark = (exit_benchmark - entry_benchmark) / entry_benchmark * basis
    if direction is CandidateDirection.SHORT:
        gross, benchmark = -gross, -benchmark
    net = gross - benchmark - policy.round_trip_cost_bps

    return CounterfactualOutcome(
        observation_id=observation_id,
        policy_identity=policy.identity,
        state=CounterfactualState.EVALUATED,
        horizon_session=horizon,
        evaluated_at=evaluated_at,
        gross_return_bps=gross.quantize(Decimal("0.01")),
        benchmark_return_bps=benchmark.quantize(Decimal("0.01")),
        net_excess_bps=net.quantize(Decimal("0.01")),
        detail=(
            f"{direction.value} {observation_id} over {policy.horizon_sessions} sessions to "
            f"{horizon.isoformat()}, benchmark-relative and net of "
            f"{policy.round_trip_cost_bps}bps round-trip cost"
        ),
    )


__all__ = [
    "BenchmarkConvention",
    "CounterfactualOutcome",
    "EntryReference",
    "HypotheticalExecutionPolicy",
    "UnevaluableReason",
    "evaluate_counterfactual",
    "horizon_session_for",
]
