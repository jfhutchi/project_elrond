"""Attack a hypothesis before it is frozen, mechanically first (#7).

The design brief is this project's own error rate. Three analysis errors in two cycles, every
one of which flattered the result, and **none was caught by the code looking wrong** -- each was
caught by the number being implausible. Errors of that shape are invisible to a reviewer reading
code, and equally invisible to an LLM asked "is there look-ahead here?". So everything
mechanically decidable is decided mechanically, and judgment is what is left over.

The checks here are not hypothetical. Each one, applied at the time, would have caught something
that actually happened:

* `hidden_beta` -- cycle 15: the rotation is beta 0.71 with alpha t=0.05, and SPY held at
  0.71x reproduces it with no trading at all.
* `cost_ladder` -- refuted #8: the strongest raw edge measured here nets -28.8% at 5bps.
* `regime_split` -- cycle 11: vol targeting as alpha won three eras of four and lost the most
  recent one.
* `cross_asset_generality` -- the same result wins on 6 of 12 assets, which is a coin flip.
* `shifted_feature_probe` -- any feature reading its own future: shift it a bar and the result
  has to move.
* survivorship -- cycle 10 needed 241 delisted names to stay interpretable.

**Consensus is not evidence, and the code cannot express it as evidence.** `consensus()` returns
the *most severe* verdict rather than the majority one, so two agreeing critics can never
outvote one objection. That is a structural guarantee rather than a policy note.

**Confidence is advisory; reasons are mandatory.** A `Critique` without reasons cannot be
constructed. A confidence number without them would be a vote, and votes are what this module
exists to prevent.

Scope: the LLM critic is an interface here, not an implementation -- #9 owns model runtime.
`DeterministicCritic` runs the mechanical checks and says plainly, in its own reasons, which
judgment dimensions it did not assess. Pretending to have assessed them would be worse than
leaving them open.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Judgment dimensions no deterministic check can settle. Named so a critique that skipped them
#: says so rather than implying coverage it does not have.
JUDGMENT_DIMENSIONS: tuple[str, ...] = (
    "economic mechanism plausibility",
    "confounders and alternative explanations",
    "capacity and execution realism",
    "data-quality concerns not resolvable mechanically",
    "whether the falsification criteria actually test the claim",
)


class CriticVerdict(StrEnum):
    PROCEED = "PROCEED"
    #: Fixable. Blocks until the objection is addressed by a new hypothesis version.
    REVISE = "REVISE"
    REJECT = "REJECT"


#: Ordered by severity. `consensus` takes the maximum, never the majority.
_SEVERITY = {CriticVerdict.PROCEED: 0, CriticVerdict.REVISE: 1, CriticVerdict.REJECT: 2}


class Severity(StrEnum):
    BLOCKING = "BLOCKING"
    ADVISORY = "ADVISORY"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Objection(FrozenModel):
    """One concrete thing wrong, with the number that shows it."""

    check: Text
    severity: Severity
    finding: Text
    #: The measurement behind the objection. An objection without one is an opinion.
    evidence: dict[str, str] = Field(min_length=1)


class Critique(FrozenModel):
    """One critic's structured review of one exact hypothesis version."""

    hypothesis_id: Text
    hypothesis_version: int = Field(ge=1)
    critic: Text
    verdict: CriticVerdict
    #: Mandatory. A verdict without reasons is a vote.
    reasons: tuple[Text, ...] = Field(min_length=1)
    objections: tuple[Objection, ...] = ()
    #: Tests the critic proposes to falsify the claim, which is the useful half of a REVISE.
    falsification_tests: tuple[Text, ...] = ()
    #: Advisory only, and never used to weigh one critique against another.
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    #: Dimensions this critic did not assess. Recorded so absence is not read as approval.
    unassessed: tuple[Text, ...] = ()
    produced_at: datetime

    @model_validator(mode="after")
    def validate_critique(self) -> Self:
        blocking = [item for item in self.objections if item.severity is Severity.BLOCKING]
        if blocking and self.verdict is CriticVerdict.PROCEED:
            raise ValueError(
                "a critique carrying a blocking objection cannot return PROCEED: "
                + "; ".join(item.check for item in blocking)
            )
        if self.verdict is not CriticVerdict.PROCEED and not self.objections:
            raise ValueError(f"a {self.verdict.value} verdict must name what is wrong")
        return self

    @property
    def blocking(self) -> bool:
        """Whether this verdict stops the hypothesis from being frozen or run."""
        return self.verdict is not CriticVerdict.PROCEED


def consensus(critiques: Sequence[Critique]) -> CriticVerdict:
    """The most severe verdict, never the majority one.

    Model agreement is not evidence, so agreement cannot be made to outweigh an objection. Two
    critics returning PROCEED against one REJECT yields REJECT, and there is no configuration
    in which it does not.
    """
    if not critiques:
        raise ValueError("consensus over no critiques is not a verdict")
    return max((critique.verdict for critique in critiques), key=lambda item: _SEVERITY[item])


def disagreements(critiques: Sequence[Critique]) -> tuple[str, ...]:
    """Concrete unresolved disagreement, preserved rather than averaged away."""
    verdicts = {critique.verdict for critique in critiques}
    if len(verdicts) <= 1:
        return ()
    return tuple(
        f"{critique.critic}: {critique.verdict.value} -- {critique.reasons[0]}"
        for critique in critiques
    )


# --- Deterministic checks ------------------------------------------------------------------


class BetaReport(FrozenModel):
    """What is left of a return series once the benchmark is taken out of it."""

    beta: float
    alpha_per_period: float
    alpha_t: float
    r_squared: float
    observations: int


def hidden_beta(strategy: Sequence[float], benchmark: Sequence[float]) -> BetaReport:
    """Regress a strategy on its benchmark and report alpha with its t, never raw return.

    Cycle 15's result in one function: the rotation is beta 0.71 with alpha t=0.05, and SPY held
    at 0.71x reproduces it to within 0.05 CAGR points with no trading at all. A raw return
    comparison hides that entirely.
    """
    if len(strategy) != len(benchmark):
        raise ValueError("strategy and benchmark must be the same length")
    count = len(strategy)
    if count < 3:
        raise ValueError("a regression needs at least three observations")

    mean_x = statistics.fmean(benchmark)
    mean_y = statistics.fmean(strategy)
    variance = sum((x - mean_x) ** 2 for x in benchmark)
    if variance == 0:
        raise ValueError("benchmark has no variance to regress against")
    covariance = sum(
        (x - mean_x) * (y - mean_y) for x, y in zip(benchmark, strategy, strict=True)
    )
    beta = covariance / variance
    alpha = mean_y - beta * mean_x

    residuals = [
        y - (alpha + beta * x) for x, y in zip(benchmark, strategy, strict=True)
    ]
    residual_sum = sum(value**2 for value in residuals)
    degrees = count - 2
    standard_error = math.sqrt(residual_sum / degrees) * math.sqrt(1 / count + mean_x**2 / variance)
    total = sum((y - mean_y) ** 2 for y in strategy)
    return BetaReport(
        beta=beta,
        alpha_per_period=alpha,
        alpha_t=alpha / standard_error if standard_error > 0 else 0.0,
        r_squared=1 - residual_sum / total if total > 0 else 1.0,
        observations=count,
    )


class CostPoint(FrozenModel):
    cost_bps: float
    net_return: float


class CostLadder(FrozenModel):
    """How much cost the edge can pay before it stops existing."""

    points: tuple[CostPoint, ...]
    #: Cost level at which net return crosses zero. `None` when it never does.
    break_even_bps: float | None

    def survives(self, cost_bps: float) -> bool:
        """Whether the edge is still positive once `cost_bps` per leg is charged."""
        return self.break_even_bps is not None and self.break_even_bps > cost_bps

    def net_at(self, cost_bps: float) -> float | None:
        for point in self.points:
            if point.cost_bps == cost_bps:
                return point.net_return
        return None


def cost_ladder(
    gross_return: float,
    trade_legs: float,
    ladder: Sequence[float] = (0.5, 1.1, 5.0, 25.0),
) -> CostLadder:
    """Sweep the cost ladder rather than asserting one cost level.

    Both arguments cover the *same period*: `gross_return` as a fraction and `trade_legs` as the
    number of one-way legs traded over it. Mixing a total return with an annual turnover is a
    unit error, and unit errors are how this project produced a result showing margin calls
    creating wealth -- so the units are named in the signature rather than left to a comment.

    Refuted #8 is the case the shape matters for: 1-day reversal produced +427.9% gross and
    -28.8% at 5bps. A single cost number hides where the edge dies; the ladder shows it.
    """
    if trade_legs < 0:
        raise ValueError("trade legs cannot be negative")
    points = tuple(
        CostPoint(cost_bps=cost, net_return=gross_return - trade_legs * cost / 10_000)
        for cost in sorted(ladder)
    )
    break_even = (
        gross_return * 10_000 / trade_legs if trade_legs > 0 and gross_return > 0 else None
    )
    return CostLadder(points=points, break_even_bps=break_even)


class RegimeSplit(FrozenModel):
    """The same claim, measured on non-overlapping eras."""

    eras: tuple[str, ...]
    statistics_by_era: dict[str, float]
    #: Eras where the statistic has the claimed sign.
    wins: int

    @property
    def consistent(self) -> bool:
        return self.wins == len(self.eras)

    @property
    def coin_flip(self) -> bool:
        return self.eras != () and abs(self.wins / len(self.eras) - 0.5) < 0.17


def regime_split(
    statistics_by_era: dict[str, float], *, expect_positive: bool = True
) -> RegimeSplit:
    """Non-overlapping eras, because an effect present in one regime is a regime, not an edge.

    Vol targeting as alpha won three of four eras and lost the most recent, which is visible
    immediately here and invisible in an aggregate.
    """
    if not statistics_by_era:
        raise ValueError("a regime split needs at least one era")
    eras = tuple(sorted(statistics_by_era))

    def has_claimed_sign(value: float) -> bool:
        return value > 0 if expect_positive else value < 0

    wins = sum(1 for era in eras if has_claimed_sign(statistics_by_era[era]))
    return RegimeSplit(eras=eras, statistics_by_era=dict(statistics_by_era), wins=wins)


class GeneralityReport(FrozenModel):
    """Whether a claimed mechanism holds where the mechanism says it should."""

    assets: int
    clearing: int
    threshold: float

    @property
    def share(self) -> float:
        return self.clearing / self.assets if self.assets else 0.0

    @property
    def indistinguishable_from_chance(self) -> bool:
        """At or below half the assets, a 'general' mechanism is a coin flip."""
        return self.share <= 0.5


def cross_asset_generality(
    statistic_by_asset: dict[str, float], *, threshold: float
) -> GeneralityReport:
    """A mechanism claim implies the mechanism works elsewhere. Vol targeting won 6 of 12."""
    if not statistic_by_asset:
        raise ValueError("generality needs at least one asset")
    return GeneralityReport(
        assets=len(statistic_by_asset),
        clearing=sum(1 for value in statistic_by_asset.values() if value >= threshold),
        threshold=threshold,
    )


def shifted_feature_probe(
    baseline: Sequence[float], shifted: Sequence[float], *, tolerance: float = 1e-12
) -> bool:
    """Shift every feature one bar and re-run. An unchanged result means it read the future.

    Returns True when the probe *passes*, meaning the result moved as it should. This is the
    cheapest look-ahead detector available and needs no understanding of the feature at all.
    """
    if len(baseline) != len(shifted):
        raise ValueError("baseline and shifted runs must be the same length")
    if not baseline:
        raise ValueError("a shifted-feature probe needs at least one observation")
    return any(
        abs(before - after) > tolerance for before, after in zip(baseline, shifted, strict=True)
    )


def deterministic_objections(
    *,
    beta: BetaReport | None = None,
    ladder: CostLadder | None = None,
    split: RegimeSplit | None = None,
    generality: GeneralityReport | None = None,
    shifted_probe_passed: bool | None = None,
    survivorship_bias: float | None = None,
    alpha_threshold: float,
    #: Cost the edge must clear, per leg. Measured NBBO here is ~1.1bps; the deployed model
    #: assumed 5bps, which `REFUTED.md` records as miscalibrated in the punitive direction.
    cost_floor_bps: float = 1.1,
) -> tuple[Objection, ...]:
    """Turn the mechanical measurements into blocking objections, or none.

    Anything not supplied produces no objection, which is why `DeterministicCritic` records what
    it could not check. A check that was never run must not read as a check that passed.
    """
    objections: list[Objection] = []

    if beta is not None and abs(beta.alpha_t) < alpha_threshold:
        objections.append(
            Objection(
                check="hidden-beta",
                severity=Severity.BLOCKING,
                finding=(
                    f"alpha is t={beta.alpha_t:.2f} against a {alpha_threshold:.2f} bar; the "
                    f"series is beta {beta.beta:.2f} to its benchmark"
                ),
                evidence={
                    "alpha_t": f"{beta.alpha_t:.4f}",
                    "beta": f"{beta.beta:.4f}",
                    "r_squared": f"{beta.r_squared:.4f}",
                },
            )
        )

    if ladder is not None and not ladder.survives(cost_floor_bps):
        break_even = (
            "never positive"
            if ladder.break_even_bps is None
            else f"{ladder.break_even_bps:.2f}bps"
        )
        objections.append(
            Objection(
                check="cost-sensitivity",
                severity=Severity.BLOCKING,
                finding=(
                    f"the edge breaks even at {break_even}, at or below the "
                    f"{cost_floor_bps}bps it has to cross"
                ),
                evidence={
                    "break_even_bps": break_even,
                    "cost_floor_bps": f"{cost_floor_bps}",
                },
            )
        )

    if split is not None and not split.consistent:
        objections.append(
            Objection(
                check="regime-split",
                severity=Severity.BLOCKING if split.coin_flip else Severity.ADVISORY,
                finding=f"holds in {split.wins} of {len(split.eras)} non-overlapping eras",
                evidence={era: f"{split.statistics_by_era[era]:.4f}" for era in split.eras},
            )
        )

    if generality is not None and generality.indistinguishable_from_chance:
        objections.append(
            Objection(
                check="cross-asset-generality",
                severity=Severity.BLOCKING,
                finding=(
                    f"the mechanism clears its bar on {generality.clearing} of "
                    f"{generality.assets} assets, which is a coin flip"
                ),
                evidence={
                    "clearing": str(generality.clearing),
                    "assets": str(generality.assets),
                    "threshold": f"{generality.threshold:.2f}",
                },
            )
        )

    if shifted_probe_passed is False:
        objections.append(
            Objection(
                check="shifted-feature-probe",
                severity=Severity.BLOCKING,
                finding="shifting every feature one bar left the result unchanged",
                evidence={"result_moved": "false"},
            )
        )

    if survivorship_bias is not None and survivorship_bias == 0.0:
        objections.append(
            Objection(
                check="survivorship",
                severity=Severity.ADVISORY,
                finding=(
                    "no instrument in the universe ever delisted, which for a cross-sectional "
                    "equity study means the universe is live-only"
                ),
                evidence={"survivorship_bias": "0.0"},
            )
        )

    return tuple(objections)


class Critic(Protocol):
    """What a critic is, so an LLM one (#9) drops in without changing the gate."""

    @property
    def name(self) -> str: ...

    def review(self, hypothesis_id: str, version: int, *, now: datetime) -> Critique: ...


class DeterministicCritic:
    """Runs the mechanical checks and abstains, loudly, from everything else."""

    name = "deterministic"

    def __init__(self, objections: Sequence[Objection]) -> None:
        self._objections = tuple(objections)

    def review(self, hypothesis_id: str, version: int, *, now: datetime) -> Critique:
        blocking = [item for item in self._objections if item.severity is Severity.BLOCKING]
        verdict = CriticVerdict.REVISE if blocking else CriticVerdict.PROCEED
        reasons = (
            tuple(f"{item.check}: {item.finding}" for item in self._objections)
            if self._objections
            else ("no mechanical check produced an objection",)
        )
        return Critique(
            hypothesis_id=hypothesis_id,
            hypothesis_version=version,
            critic=self.name,
            verdict=verdict,
            reasons=reasons,
            objections=self._objections,
            # Named rather than left implicit: this critic assessed none of them, and a
            # registration that only ever saw this critic has an open judgment surface.
            unassessed=JUDGMENT_DIMENSIONS,
            produced_at=now,
        )


__all__ = [
    "JUDGMENT_DIMENSIONS",
    "BetaReport",
    "CostLadder",
    "CostPoint",
    "Critic",
    "CriticVerdict",
    "Critique",
    "DeterministicCritic",
    "GeneralityReport",
    "Objection",
    "RegimeSplit",
    "Severity",
    "consensus",
    "cost_ladder",
    "cross_asset_generality",
    "deterministic_objections",
    "disagreements",
    "hidden_beta",
    "regime_split",
    "shifted_feature_probe",
]
