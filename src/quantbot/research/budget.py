"""Bounds on autonomous research, with the scarce budget treated as the scarce one (#14).

Wall time, CPU, tokens and dollars all refill. Wait a day and there is more. **Untouched data
does not.** SIP history begins 2016-01-04, cycles 2-10 consumed every out-of-sample window, and
each further trial against that fixed window permanently raises the bar every future result has
to clear:

| cumulative trials | luck bar | minimum Sharpe detectable over 10.6y |
|---|---|---|
| 24 | 2.52 | 0.77 |
| 100 | 3.03 | 0.93 |
| 1,000 | 3.72 | 1.14 |

SPY buy-and-hold scores 0.90 on that window. Spend enough trials and this project can no longer
demonstrate anything at all, and **no amount of compute buys the budget back**. A GPU-hour
regenerates overnight; a consumed holdout never does. So `TRIALS` is modelled as a budget with
a hard cap, admission control, and an audited override -- the same machinery as tokens, and a
different attitude.

Statistical budget is keyed per family and per dataset window rather than as one global number,
because a single figure hides which evidence was actually spent and on what.

**The trading daemon is out of scope on purpose.** Research starvation must never be able to
interrupt the paper account, which is the only genuinely uncontaminated evidence this project
will ever get. Nothing in `quantbot.operations` or `quantbot.runtime` imports this module, and
`tests/unit/research/test_budget.py` asserts that mechanically rather than trusting it.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from quantbot.storage.database import encode_utc
from quantbot.storage.schema import budget_spend

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

GLOBAL = "global"


class Resource(StrEnum):
    """What is being spent. The first is not like the others."""

    #: Candidate evaluations against a dataset window. Non-renewable, and the reason this
    #: module exists rather than a token counter.
    TRIALS = "TRIALS"
    WALL_SECONDS = "WALL_SECONDS"
    TOKENS = "TOKENS"
    REQUESTS = "REQUESTS"
    DOLLARS = "DOLLARS"
    #: Debate, revision and retry rounds. The bound that stops a recursive agent loop.
    ROUNDS = "ROUNDS"


#: Resources that come back on their own. Everything absent from this set is permanent.
RENEWABLE = frozenset(
    {Resource.WALL_SECONDS, Resource.TOKENS, Resource.REQUESTS, Resource.DOLLARS, Resource.ROUNDS}
)


class BudgetExceeded(RuntimeError):
    """Raised when admitting a task would exceed a cap. Fails closed by construction."""

    def __init__(
        self,
        budget_key: str,
        resource: Resource,
        requested: Decimal,
        spent: Decimal,
        cap: Decimal,
    ) -> None:
        self.budget_key = budget_key
        self.resource = resource
        self.requested = requested
        self.spent = spent
        self.cap = cap
        permanence = "renewable" if resource in RENEWABLE else "NON-RENEWABLE"
        super().__init__(
            f"{budget_key}: {requested} more {resource.value} would take spend to "
            f"{spent + requested} against a cap of {cap} ({permanence})"
        )


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class BudgetOverride(FrozenModel):
    """An operator raising a cap, on the record.

    Raising a statistical cap is a permanent evidence decision. It is stored beside the spend
    it authorised rather than logged, so the audit survives the session that made it.
    """

    authorized_by: Text
    reason: Text


class Cap(FrozenModel):
    """One limit on one resource for one budget key."""

    budget_key: Text
    resource: Resource
    limit: Decimal = Field(ge=0, allow_inf_nan=False)
    #: Fraction of the cap at which the governor starts warning rather than refusing.
    warn_at: Decimal = Field(default=Decimal("0.8"), gt=0, le=1)

    @property
    def renewable(self) -> bool:
        return self.resource in RENEWABLE


class Admission(FrozenModel):
    """The answer to "may this task run", and what it costs if it does."""

    budget_key: Text
    resource: Resource
    requested: Decimal
    spent: Decimal
    cap: Decimal
    warning: bool

    @property
    def remaining(self) -> Decimal:
        return self.cap - self.spent

    @property
    def permanent(self) -> bool:
        return self.resource not in RENEWABLE


class BudgetGovernor:
    """Admit or refuse work against durable spend, inside one caller-owned transaction."""

    def __init__(self, session: Session, caps: Sequence[Cap]) -> None:
        self._session = session
        self._caps = {(cap.budget_key, cap.resource): cap for cap in caps}
        if len(self._caps) != len(caps):
            raise ValueError("a budget key and resource may carry only one cap")

    def caps(self) -> tuple[Cap, ...]:
        return tuple(self._caps.values())

    def spent(self, budget_key: str, resource: Resource) -> Decimal:
        total = self._session.execute(
            select(func.coalesce(func.sum(budget_spend.c.amount), "0")).where(
                budget_spend.c.budget_key == budget_key,
                budget_spend.c.category == resource.value,
            )
        ).scalar_one()
        return Decimal(str(total))

    def admit(
        self,
        budget_key: str,
        resource: Resource,
        requested: Decimal,
        *,
        override: BudgetOverride | None = None,
    ) -> Admission:
        """Estimate the cost before the work happens, and refuse it if it does not fit.

        Estimating *before* admission is the point: a worker that evaluates a thousand
        candidates and returns one has spent a thousand trials, and discovering that afterwards
        is discovering it too late.
        """
        cap = self._caps.get((budget_key, resource))
        if cap is None:
            raise BudgetExceeded(budget_key, resource, requested, Decimal(0), Decimal(0))
        if requested < 0:
            raise ValueError("a request cannot be negative")

        spent = self.spent(budget_key, resource)
        projected = spent + requested
        if projected > cap.limit and override is None:
            raise BudgetExceeded(budget_key, resource, requested, spent, cap.limit)
        return Admission(
            budget_key=budget_key,
            resource=resource,
            requested=requested,
            spent=spent,
            cap=cap.limit,
            warning=cap.limit > 0 and projected >= cap.limit * cap.warn_at,
        )

    def record(
        self,
        admission: Admission,
        *,
        task: str,
        now: datetime,
        override: BudgetOverride | None = None,
        note: str = "",
    ) -> None:
        """Commit the spend. Append-only, so a budget cannot be quietly rewound."""
        self._session.execute(
            budget_spend.insert().values(
                budget_key=admission.budget_key,
                category=admission.resource.value,
                amount=str(admission.requested),
                task=task,
                recorded_at=encode_utc(now),
                override_by=None if override is None else override.authorized_by,
                note=note if override is None else f"{override.reason} | {note}".strip(" |"),
            )
        )

    def spend(
        self,
        budget_key: str,
        resource: Resource,
        requested: Decimal,
        *,
        task: str,
        now: datetime,
        override: BudgetOverride | None = None,
        note: str = "",
    ) -> Admission:
        """Admit and record in one step, which is the normal path."""
        admission = self.admit(budget_key, resource, requested, override=override)
        self.record(admission, task=task, now=now, override=override, note=note)
        return admission

    def overrides(self) -> list[tuple[str, str, str]]:
        """Every cap an operator raised, attributable and permanent."""
        rows = self._session.execute(
            select(
                budget_spend.c.override_by,
                budget_spend.c.budget_key,
                budget_spend.c.category,
            )
            .where(budget_spend.c.override_by.is_not(None))
            .order_by(budget_spend.c.spend_id)
        )
        return [(str(row[0]), str(row[1]), str(row[2])) for row in rows]

    def worth_spending(
        self, budget_key: str, requested_trials: Decimal, *, expected_information_gain: Decimal
    ) -> bool:
        """Whether a family can afford this question at all.

        Not a compute decision. A family close to its trial cap should decline a low-value
        question even when compute is free, because the trials it would spend are the thing
        that cannot be bought back.
        """
        cap = self._caps.get((budget_key, Resource.TRIALS))
        if cap is None or cap.limit == 0:
            return False
        remaining = cap.limit - self.spent(budget_key, Resource.TRIALS)
        if requested_trials > remaining:
            return False
        share = requested_trials / cap.limit
        return expected_information_gain >= share


class LoopBound(FrozenModel):
    """A hard stop on debate, revision and retry rounds.

    A recursive agent workflow that can always try once more does not terminate. This is the
    counter that makes it, and it is deliberately not a soft warning.
    """

    task: Text
    max_rounds: int = Field(ge=1)
    rounds_taken: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_bound(self) -> Self:
        if self.rounds_taken > self.max_rounds:
            raise ValueError(
                f"{self.task} took {self.rounds_taken} rounds against a bound of {self.max_rounds}"
            )
        return self

    def advance(self) -> LoopBound:
        if self.rounds_taken >= self.max_rounds:
            raise BudgetExceeded(
                self.task,
                Resource.ROUNDS,
                Decimal(1),
                Decimal(self.rounds_taken),
                Decimal(self.max_rounds),
            )
        return self.model_copy(update={"rounds_taken": self.rounds_taken + 1})

    @property
    def exhausted(self) -> bool:
        return self.rounds_taken >= self.max_rounds


def family_key(family_id: str) -> str:
    return f"family:{family_id}"


def dataset_key(dataset: str) -> str:
    """Statistical budget is keyed per dataset, not globally.

    One global number hides which evidence was spent and on what -- the difference between a
    project that has used up US equities and one that has used up everything.
    """
    return f"dataset:{dataset}"


__all__ = [
    "GLOBAL",
    "RENEWABLE",
    "Admission",
    "BudgetExceeded",
    "BudgetGovernor",
    "BudgetOverride",
    "Cap",
    "LoopBound",
    "Resource",
    "dataset_key",
    "family_key",
]
