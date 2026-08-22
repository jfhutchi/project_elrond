"""Delegate research without delegating authority over evidence (#11).

An external miner is a fast way to spend a dataset. RD-Agent, Qlib, or anything like them can
evaluate thousands of candidates against a fixed window and hand back the best one, and the best
of a thousand skill-free candidates looks excellent. So the contract here is built around one
number.

**A worker that will not say how many candidates it evaluated is `EXPLORATORY_ONLY`.** Not
discouraged, not flagged in a report -- mechanically unable to produce confirmatory evidence,
because its result cannot be deflated against a search it will not disclose. Disclosing 900 and
returning 3 costs 900 trials, and #14 charges for all of them.

**Imported results are untrusted artifacts.** A worker returns numbers and files; it does not
return conclusions, cannot deploy a strategy, and has no route to an order. Nothing in this
module imports the broker, the execution path, or the runtime, and the #3 test walks the graph
to prove the whole research package does not.

**Unsupported semantics are detected, not approximated.** A worker that cannot express the
statistical test a registration requires says so, rather than running something adjacent and
reporting it as the same thing. Quiet approximation is how a faithful compilation becomes a
wrong experiment.

The most useful thing a miner can do here is not find a factor. It is `empirical_luck_threshold`:
run the same search procedure over shuffled or synthetic null data and measure what it produces
when there is nothing to find. That gives a bar earned by the actual procedure rather than
assumed from `sqrt(2 ln N)`, which is the honest way to judge a search nobody fully specified.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.registry import DataRole

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class WorkerTrust(StrEnum):
    """What a worker's output may be used for."""

    #: Discloses its total search cardinality, so its results can be deflated honestly.
    CONFIRMATORY_ELIGIBLE = "CONFIRMATORY_ELIGIBLE"
    #: Cannot or will not disclose it. Its results may inform a question, never answer one.
    EXPLORATORY_ONLY = "EXPLORATORY_ONLY"


class WorkerCapability(StrEnum):
    FACTOR_MINING = "FACTOR_MINING"
    MODEL_SEARCH = "MODEL_SEARCH"
    BACKTEST = "BACKTEST"
    #: Can run its own search over shuffled or synthetic null data. The most valuable one.
    NULL_CALIBRATION = "NULL_CALIBRATION"
    #: Reads source text and proposes confounders and falsification tests (#37). Named as a
    #: capability of its own because it is not mining: it evaluates no candidate against
    #: outcomes, and a worker that claims it must not be handed a factor-mining mandate.
    SEMANTIC_ANALYSIS = "SEMANTIC_ANALYSIS"


class UnsupportedSemantics(RuntimeError):
    """Raised when a worker cannot express what the registration actually requires.

    An error rather than an approximation: running something adjacent and reporting it as the
    same thing is how a faithful compilation becomes a wrong experiment.
    """


class WorkerFailure(RuntimeError):
    """A worker run that did not complete. Recorded, and never corrupting caller state."""


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WorkerSpec(FrozenModel):
    """One external engine, and what it is trusted with."""

    name: Text
    version: Text
    capabilities: frozenset[WorkerCapability]
    configuration_hash: Text
    #: Whether this worker reports total candidates evaluated for data-dependent search.
    discloses_search_cardinality: bool

    @property
    def trust(self) -> WorkerTrust:
        """Undisclosed search means exploratory only. There is no configuration to change it."""
        return (
            WorkerTrust.CONFIRMATORY_ELIGIBLE
            if self.discloses_search_cardinality
            else WorkerTrust.EXPLORATORY_ONLY
        )

    def require(self, capability: WorkerCapability) -> None:
        if capability not in self.capabilities:
            raise UnsupportedSemantics(
                f"{self.name}@{self.version} cannot do {capability.value}; it must say so "
                "rather than run something adjacent"
            )


class WorkerInput(FrozenModel):
    """What a worker is given, and what it is allowed to see."""

    hypothesis_id: str | None = None
    hypothesis_version: int | None = Field(default=None, ge=1)
    mandate: Text
    datasets: tuple[Text, ...] = Field(min_length=1)
    #: A worker may only ever be handed discovery or validation data.
    data_roles: frozenset[DataRole]
    #: Ceiling on candidates the worker may evaluate, from #14's admission.
    trial_budget: int = Field(ge=1)
    parameters: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_input(self) -> Self:
        forbidden = self.data_roles - {DataRole.DISCOVERY, DataRole.VALIDATION}
        if forbidden:
            raise ValueError(
                "an external worker may not be handed "
                + ", ".join(sorted(role.value for role in forbidden))
                + "; a miner over protected data spends it"
            )
        if not self.data_roles:
            raise ValueError("a worker input must name the data roles it may read")
        return self


class WorkerResult(FrozenModel):
    """What came back: numbers and artifacts, never a conclusion."""

    worker: WorkerSpec
    request: WorkerInput
    started_at: datetime
    finished_at: datetime
    #: Total candidates evaluated, including everything discarded. `None` when the worker will
    #: not say, which forces the result to exploratory whatever it contains.
    search_cardinality: int | None = None
    #: Mapped into Elrond's units by the adapter, not the worker's own metric names.
    metrics: dict[str, str] = Field(default_factory=dict)
    #: Content hashes of files the worker produced. Untrusted until something checks them.
    artifacts: dict[str, str] = Field(default_factory=dict)
    #: Anything the worker could not express. A non-empty list forces exploratory.
    unsupported: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if self.finished_at < self.started_at:
            raise ValueError("a worker run cannot finish before it starts")
        if self.search_cardinality is not None and self.search_cardinality < 1:
            raise ValueError("a disclosed search evaluated at least one candidate")
        if (
            self.search_cardinality is not None
            and self.search_cardinality > self.request.trial_budget
        ):
            raise ValueError(
                f"{self.worker.name} evaluated {self.search_cardinality} candidates against a "
                f"budget of {self.request.trial_budget}"
            )
        return self

    @property
    def trust(self) -> WorkerTrust:
        """Confirmatory only when the worker disclosed its search and expressed everything."""
        if self.worker.trust is WorkerTrust.EXPLORATORY_ONLY:
            return WorkerTrust.EXPLORATORY_ONLY
        if self.search_cardinality is None or self.unsupported:
            return WorkerTrust.EXPLORATORY_ONLY
        return WorkerTrust.CONFIRMATORY_ELIGIBLE

    @property
    def trials_spent(self) -> int:
        """What #14 is charged. An undisclosed search costs the whole budget it was given.

        Charging the budget rather than zero is deliberate: a worker that will not say what it
        spent must not be cheaper than one that will.
        """
        return (
            self.request.trial_budget
            if self.search_cardinality is None
            else self.search_cardinality
        )

    @property
    def citable_as_confirmatory(self) -> bool:
        return self.trust is WorkerTrust.CONFIRMATORY_ELIGIBLE


class ResearchWorker(Protocol):
    """The contract a second implementation satisfies without touching orchestration."""

    @property
    def spec(self) -> WorkerSpec: ...

    def run(self, request: WorkerInput, *, now: datetime) -> WorkerResult: ...


def empirical_luck_threshold(
    null_statistics: Sequence[float], *, quantile: float = 0.95
) -> Decimal:
    """What this search procedure produces on data with nothing in it.

    Run the same miner over shuffled or synthetic null data and take a high quantile of what it
    finds. That is a bar the procedure earned rather than one assumed from `sqrt(2 ln N)`, and
    it is the only honest way to judge a search whose true cardinality nobody fully specified.
    """
    if len(null_statistics) < 20:
        raise ValueError(
            "an empirical null needs at least 20 draws to say anything about its own tail"
        )
    if not 0.5 < quantile < 1.0:
        raise ValueError("quantile must be an upper tail")
    ordered = sorted(abs(value) for value in null_statistics)
    index = min(len(ordered) - 1, math.ceil(quantile * len(ordered)) - 1)
    return Decimal(repr(ordered[index])).quantize(Decimal("0.000001"), rounding=ROUND_HALF_EVEN)


def null_calibration_summary(null_statistics: Sequence[float]) -> Mapping[str, Decimal]:
    """The shape of what a search finds in noise, for the record rather than for a verdict."""
    if len(null_statistics) < 20:
        raise ValueError("an empirical null needs at least 20 draws")
    absolute = [abs(value) for value in null_statistics]
    return {
        "draws": Decimal(len(absolute)),
        "median": Decimal(repr(statistics.median(absolute))).quantize(Decimal("0.000001")),
        "p95": empirical_luck_threshold(null_statistics, quantile=0.95),
        "p99": empirical_luck_threshold(null_statistics, quantile=0.99),
        "max": Decimal(repr(max(absolute))).quantize(Decimal("0.000001")),
    }


def import_result(result: WorkerResult) -> dict[str, str]:
    """Map a worker's output into Elrond's units, carrying its trust with it.

    The trust label travels with the numbers. A caller that drops it is the failure this
    function exists to make awkward.
    """
    return {
        "worker": f"{result.worker.name}@{result.worker.version}",
        "configuration_hash": result.worker.configuration_hash,
        "trust": result.trust.value,
        "search_cardinality": (
            "undisclosed" if result.search_cardinality is None else str(result.search_cardinality)
        ),
        "trials_charged": str(result.trials_spent),
        "data_roles": ",".join(sorted(role.value for role in result.request.data_roles)),
        "unsupported": ",".join(result.unsupported) if result.unsupported else "none",
        **{f"metric.{name}": value for name, value in sorted(result.metrics.items())},
        **{f"artifact.{name}": value for name, value in sorted(result.artifacts.items())},
    }


__all__ = [
    "ResearchWorker",
    "UnsupportedSemantics",
    "WorkerCapability",
    "WorkerFailure",
    "WorkerInput",
    "WorkerResult",
    "WorkerSpec",
    "WorkerTrust",
    "empirical_luck_threshold",
    "import_result",
    "null_calibration_summary",
]
