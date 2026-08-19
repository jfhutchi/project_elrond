"""What the operator needs to see, and the one comparison that must never be blurred (#15).

This is the data behind the view, not the view. `scripts/dashboard.py` already renders the
operations panels -- daemon health, kill switch, reconciliation, paper account, strategy
identity -- and remains the single truth source for them. This adds the research panels beside
it rather than a second place to look.

**The dangerous confusion available here is treating research activity as progress toward
deployment.** Seventeen completed cycles and a nearly-empty qualification window can be made to
look like momentum by any view that shows throughput without its denominator. So
`EvidencePanel` holds forward days and research volume as separate fields with separate units,
`combined()` does not exist, and the panel reports the denominator whether or not anyone asked.

**Statistical budget is shown like spend, because it is spend that never refunds.** Cumulative
trials and the luck bar they imply are the resource most likely to be silently exhausted by an
autonomous loop, and unlike tokens it does not come back. At 1,000 trials the bar is t=3.72,
which is above SPY's own Sharpe on this window -- past that point the project cannot demonstrate
anything, and the number should be as visible as a token count.

**A metric carries where it came from.** `Provenance.MODEL_NARRATIVE` can never be displayed as
`OBSERVED`; the constructor refuses. "No metric shown as paper evidence can be sourced only from
an LLM narrative" is a validator rather than a review habit.

**The default emphasis is what changed and what is blocked, not what ran.** A research loop
generates a large volume of uninteresting activity by design. `attention()` returns blockers
first and drops routine activity entirely, because everything else is scrollback.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.director import ACTIVE, ResearchTask, TaskState
from quantbot.research.promotion import MINIMUM_FORWARD_DAYS, MINIMUM_FORWARD_TRADES

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EvidenceState(StrEnum):
    """What kind of thing a number is. Displayed, never inferred from context."""

    #: Genuinely happened on the paper account.
    OBSERVED = "OBSERVED"
    BACKTEST = "BACKTEST"
    EXPLORATORY = "EXPLORATORY"
    LITERATURE = "LITERATURE"
    #: Measured, and the sample could not resolve it. Distinct from refuted, everywhere.
    UNDERPOWERED = "UNDERPOWERED"
    #: Derived from other numbers rather than measured.
    INFERRED = "INFERRED"
    #: The window has not happened yet. The honest label for most of this project.
    NOT_YET_OBSERVED = "NOT_YET_OBSERVED"


class Provenance(StrEnum):
    DURABLE_LEDGER = "DURABLE_LEDGER"
    EXPERIMENT_MANIFEST = "EXPERIMENT_MANIFEST"
    EXTERNAL_SOURCE = "EXTERNAL_SOURCE"
    #: Produced by a model. May be shown, never as evidence.
    MODEL_NARRATIVE = "MODEL_NARRATIVE"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Metric(FrozenModel):
    """One displayed number, labelled with what it is and where it came from."""

    label: Text
    value: Text
    state: EvidenceState
    provenance: Provenance
    #: Where to look to check it. A summary without one is a claim.
    evidence_link: str | None = None

    @model_validator(mode="after")
    def validate_metric(self) -> Self:
        if self.provenance is Provenance.MODEL_NARRATIVE and self.state in (
            EvidenceState.OBSERVED,
            EvidenceState.BACKTEST,
        ):
            raise ValueError(
                f"'{self.label}' is sourced from a model narrative and cannot be displayed as "
                f"{self.state.value}; a generated number is not a measurement"
            )
        if self.state is EvidenceState.OBSERVED and self.provenance is not (
            Provenance.DURABLE_LEDGER
        ):
            raise ValueError(
                f"'{self.label}' is displayed as OBSERVED but does not come from the durable "
                "ledger; forward evidence has exactly one source"
            )
        return self


class EvidencePanel(FrozenModel):
    """Forward evidence and research volume, side by side and never added together.

    The two fields have different units and different meanings. There is deliberately no
    property here combining them, and `qualification_fraction` always reports the denominator:
    a view that shows cycle counts without it makes a nearly-empty window look like momentum.
    """

    forward_days: int = Field(ge=0)
    forward_trades: int = Field(ge=0)
    research_cycles: int = Field(ge=0)
    hypotheses_registered: int = Field(ge=0)
    hypotheses_refuted: int = Field(ge=0)
    hypotheses_underpowered: int = Field(ge=0)
    hypotheses_survived: int = Field(ge=0)

    @property
    def days_required(self) -> int:
        return MINIMUM_FORWARD_DAYS

    @property
    def trades_required(self) -> int:
        return MINIMUM_FORWARD_TRADES

    @property
    def qualification_fraction(self) -> str:
        """Always with its denominator. `1 of 30 days, 0 of 30 trades`, not `1 day`."""
        return (
            f"{self.forward_days} of {self.days_required} days, "
            f"{self.forward_trades} of {self.trades_required} trades"
        )

    @property
    def qualified(self) -> bool:
        return (
            self.forward_days >= self.days_required
            and self.forward_trades >= self.trades_required
        )

    def metrics(self) -> tuple[Metric, ...]:
        """Forward evidence first, and research volume labelled as what it is."""
        return (
            Metric(
                label="forward paper evidence",
                value=self.qualification_fraction,
                state=EvidenceState.OBSERVED
                if self.forward_days
                else EvidenceState.NOT_YET_OBSERVED,
                provenance=Provenance.DURABLE_LEDGER,
                evidence_link="qualification_days",
            ),
            Metric(
                label="research cycles completed",
                value=str(self.research_cycles),
                state=EvidenceState.BACKTEST,
                provenance=Provenance.EXPERIMENT_MANIFEST,
                evidence_link="reports/research",
            ),
            Metric(
                label="hypotheses underpowered",
                value=str(self.hypotheses_underpowered),
                state=EvidenceState.UNDERPOWERED,
                provenance=Provenance.EXPERIMENT_MANIFEST,
                evidence_link="power_assessments",
            ),
        )


def luck_bar_at(trials: int) -> Decimal:
    """The bar a result must clear at a given cumulative trial count."""
    if trials < 2:
        return Decimal("1.96")
    return Decimal(repr(math.sqrt(2 * math.log(trials)))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )


class BudgetPanel(FrozenModel):
    """Statistical budget shown like spend, because it is spend that never refunds."""

    budget_key: Text
    trials_spent: int = Field(ge=0)
    trials_cap: int = Field(ge=1)
    #: Renewable spend, shown beside it so the difference is visible rather than implied.
    tokens_spent: int = Field(default=0, ge=0)

    @property
    def luck_bar(self) -> Decimal:
        return luck_bar_at(self.trials_spent)

    @property
    def trials_remaining(self) -> int:
        return max(0, self.trials_cap - self.trials_spent)

    @property
    def exhausted(self) -> bool:
        return self.trials_remaining == 0

    @property
    def bar_at_cap(self) -> Decimal:
        """What the bar becomes if the whole budget is spent. The number worth seeing early."""
        return luck_bar_at(self.trials_cap)

    def metrics(self) -> tuple[Metric, ...]:
        return (
            Metric(
                label=f"trials spent ({self.budget_key})",
                value=f"{self.trials_spent} of {self.trials_cap}",
                state=EvidenceState.INFERRED,
                provenance=Provenance.DURABLE_LEDGER,
                evidence_link="budget_spend",
            ),
            Metric(
                label="luck bar now",
                value=str(self.luck_bar),
                state=EvidenceState.INFERRED,
                provenance=Provenance.DURABLE_LEDGER,
            ),
            Metric(
                label="luck bar if the budget is fully spent",
                value=str(self.bar_at_cap),
                state=EvidenceState.INFERRED,
                provenance=Provenance.DURABLE_LEDGER,
            ),
        )


class Alert(FrozenModel):
    """Something that needs the operator. Not something that merely happened."""

    #: BLOCKED, HALTED, STUCK, FAILED, EXHAUSTED, SURVIVED.
    kind: Text
    detail: Text
    #: Lower sorts first.
    urgency: int = Field(ge=0)


def attention(
    tasks: Sequence[ResearchTask],
    budgets: Sequence[BudgetPanel],
    *,
    stuck: Sequence[ResearchTask] = (),
    kill_switch_engaged: bool = False,
    reconciliation_failed: bool = False,
) -> tuple[Alert, ...]:
    """What changed and what is blocked. Routine activity is dropped, not ranked lower.

    A research loop generates a large volume of uninteresting activity by design. The signal is
    the halt, the failed reconciliation, the stuck task, the survivor. Everything else is
    scrollback, and including it is how the signal gets lost.
    """
    alerts: list[Alert] = []
    if kill_switch_engaged:
        alerts.append(Alert(kind="HALTED", detail="the kill switch is engaged", urgency=0))
    if reconciliation_failed:
        alerts.append(
            Alert(kind="FAILED", detail="the last reconciliation did not succeed", urgency=0)
        )
    for budget in budgets:
        if budget.exhausted:
            alerts.append(
                Alert(
                    kind="EXHAUSTED",
                    detail=(
                        f"{budget.budget_key} has no statistical budget left; the bar is "
                        f"{budget.luck_bar} and no amount of compute buys it back"
                    ),
                    urgency=1,
                )
            )
    alerts.extend(
        Alert(kind="BLOCKED", detail=f"{item.task_id}: {item.question}", urgency=2)
        for item in tasks
        if item.state is TaskState.BLOCKED
    )
    alerts.extend(
        Alert(kind="STUCK", detail=f"{item.task_id} has not moved", urgency=3)
        for item in stuck
    )
    alerts.extend(
        Alert(kind="SURVIVED", detail=f"{item.task_id}: {item.question}", urgency=4)
        for item in tasks
        if item.state is TaskState.SURVIVED
    )
    return tuple(sorted(alerts, key=lambda alert: (alert.urgency, alert.detail)))


def pipeline_counts(tasks: Sequence[ResearchTask]) -> dict[str, int]:
    """Lifecycle census, with the active states always present so zero is visible as zero."""
    counts = {state.value: 0 for state in TaskState}
    for item in tasks:
        counts[item.state.value] += 1
    return counts


def active_count(tasks: Sequence[ResearchTask]) -> int:
    return sum(1 for item in tasks if item.state in ACTIVE)


__all__ = [
    "Alert",
    "BudgetPanel",
    "EvidencePanel",
    "EvidenceState",
    "Metric",
    "Provenance",
    "active_count",
    "attention",
    "luck_bar_at",
    "pipeline_counts",
]
