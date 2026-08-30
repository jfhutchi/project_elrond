"""Did a risk gate add economic value, and is the sample big enough to say? (#57 phase 2)

The question #57 was opened for. A risk governor can look prudent while quietly destroying edge,
or look overly conservative while preventing the losses that would have ended the account, and
nothing in this project could tell those apart.

**Grouping is the whole method, and the wrong grouping is the whole failure.** Outcomes are
reported per `(gate, gate_version, rule_parameters_hash, policy_identity)`. Pooling across a
threshold change judges a rule on evidence gathered under a different rule; pooling across
execution policies compares numbers computed under different assumed fills. Both are silent, and
both make a gate look better or worse than it was.

**One candidate is one sample, however many rules refused it.** A candidate refused by four
vetoes is one event with four reasons. Counting it four times inflates the sample fourfold --
the manufacture of sample size that killed cycle 11's overnight result -- so the gate-level
count and the candidate-level count are different numbers and this reports both.

**Same-session refusals are not independent.** On a macro day the whole roster is refused for the
same underlying reason, and #56's routing gate measured what that does: the naive count overstates
by 1.67x on real data. The design-effect estimator built there is reused rather than
reimplemented, because two notions of effective sample size in one repository would eventually
disagree.

**Below the floor there is no point estimate.** `UNDERPOWERED` suppresses the economic number
entirely rather than reporting it with a caveat. A number with a warning beside it is still a
number somebody will tune a threshold against, and threshold tuning from noise is precisely what
this dataset makes easy.
"""

from __future__ import annotations

import statistics
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.anomaly.routing import (
    MINIMUM_EFFECTIVE_OBSERVATIONS,
    RoutingSample,
    dependence_units,
    design_effect,
    intraclass_correlation,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GateVerdict(StrEnum):
    """What the evidence says about one gate, under one policy."""

    #: Refused candidates would, on average, have lost money after costs. The gate paid.
    REFUSALS_AVOIDED_LOSSES = "REFUSALS_AVOIDED_LOSSES"
    #: Refused candidates would have made money. The gate cost something, which is a finding
    #: rather than a fault -- a control that never costs anything is not binding.
    REFUSALS_FORWENT_GAINS = "REFUSALS_FORWENT_GAINS"
    #: Too few effective observations to say. No point estimate is reported.
    UNDERPOWERED = "UNDERPOWERED"
    #: Nothing has been evaluated for this gate yet.
    NO_EVALUATED_OUTCOMES = "NO_EVALUATED_OUTCOMES"


class GateOutcomeSample(BaseModel):
    """One evaluated refusal, reduced to what a gate-level report needs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    observation_id: Text
    gate_id: Text
    gate_version: int = Field(ge=1)
    rule_parameters_hash: Text
    policy_identity: Text
    session_date: date
    #: The dependence cluster the symbol was in, when a snapshot could place it.
    group_key: str | None = None
    net_excess_bps: Decimal


class GateValueReport(BaseModel):
    """One gate's economics under one rule version and one execution policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate_id: Text
    gate_version: int = Field(ge=1)
    rule_parameters_hash: Text
    policy_identity: Text
    #: Distinct candidates, not reasons. A candidate refused by four gates appears once in each
    #: gate's report and never four times in one.
    candidates: int = Field(ge=0)
    effective_observations: int = Field(ge=0)
    intraclass_correlation: Decimal = Field(ge=0, le=1)
    design_effect: Decimal = Field(ge=1)
    verdict: GateVerdict
    #: Mean net excess the refused candidates would have earned. `None` whenever the verdict is
    #: not a measured one -- suppressed rather than caveated.
    mean_net_excess_bps: Decimal | None = None
    detail: Text

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        measured = {
            GateVerdict.REFUSALS_AVOIDED_LOSSES,
            GateVerdict.REFUSALS_FORWENT_GAINS,
        }
        if self.verdict in measured and self.mean_net_excess_bps is None:
            raise ValueError("a measured verdict must carry the number it rests on")
        if self.verdict not in measured and self.mean_net_excess_bps is not None:
            raise ValueError(
                f"{self.verdict.value} carries a point estimate; a number with a warning beside "
                "it is still a number somebody will tune a threshold against"
            )
        if self.effective_observations > self.candidates:
            raise ValueError("a dependence discount can only shrink a sample, never enlarge it")
        return self


def _as_routing_samples(samples: Sequence[GateOutcomeSample]) -> list[RoutingSample]:
    """Reuse #56's estimator rather than writing a second notion of effective sample size.

    Two implementations of the same statistic in one repository eventually disagree, and the one
    that disagrees in the flattering direction is the one that survives review.
    """
    return [
        RoutingSample(
            symbol=sample.observation_id,
            session_date=sample.session_date,
            selected=True,
            group_key=sample.group_key,
            next_absolute_move_sigma=sample.net_excess_bps,
        )
        for sample in samples
    ]


def report_gate_value(
    samples: Sequence[GateOutcomeSample],
    *,
    gate_id: str,
    gate_version: int,
    rule_parameters_hash: str,
    policy_identity: str,
    minimum_effective: int = MINIMUM_EFFECTIVE_OBSERVATIONS,
) -> GateValueReport:
    """Judge one gate under one rule version and one execution policy.

    The caller supplies an already-grouped set. That is deliberate: making the grouping an
    argument rather than something this function infers means a caller cannot accidentally hand
    it a mixture, and a mixture is the failure that matters here.
    """
    matching = [
        sample
        for sample in samples
        if (
            sample.gate_id == gate_id
            and sample.gate_version == gate_version
            and sample.rule_parameters_hash == rule_parameters_hash
            and sample.policy_identity == policy_identity
        )
    ]
    # One candidate is one sample however many times it appears.
    unique: dict[str, GateOutcomeSample] = {}
    for sample in matching:
        unique.setdefault(sample.observation_id, sample)
    ordered = list(unique.values())

    if not ordered:
        return GateValueReport(
            gate_id=gate_id,
            gate_version=gate_version,
            rule_parameters_hash=rule_parameters_hash,
            policy_identity=policy_identity,
            candidates=0,
            effective_observations=0,
            intraclass_correlation=Decimal("0"),
            design_effect=Decimal("1"),
            verdict=GateVerdict.NO_EVALUATED_OUTCOMES,
            detail=(
                f"no evaluated counterfactual exists for {gate_id} v{gate_version} under "
                f"{policy_identity}; the horizon may not have elapsed"
            ),
        )

    routing = _as_routing_samples(ordered)
    units = dependence_units(routing)
    icc = intraclass_correlation(units)
    deff = design_effect(units)
    effective = max(1, int(len(units) / float(deff)))

    if effective < minimum_effective:
        return GateValueReport(
            gate_id=gate_id,
            gate_version=gate_version,
            rule_parameters_hash=rule_parameters_hash,
            policy_identity=policy_identity,
            candidates=len(ordered),
            effective_observations=effective,
            intraclass_correlation=icc,
            design_effect=deff,
            verdict=GateVerdict.UNDERPOWERED,
            detail=(
                f"{len(ordered)} candidates reduce to {effective} effective observations against "
                f"a {minimum_effective} floor (ICC {icc}, design effect {deff}); no point "
                "estimate is reported, because a number with a caveat beside it is still a "
                "number somebody will tune a threshold against"
            ),
        )

    mean = Decimal(
        str(round(statistics.fmean(float(sample.net_excess_bps) for sample in ordered), 2))
    )
    verdict = (
        GateVerdict.REFUSALS_FORWENT_GAINS if mean > 0 else GateVerdict.REFUSALS_AVOIDED_LOSSES
    )
    return GateValueReport(
        gate_id=gate_id,
        gate_version=gate_version,
        rule_parameters_hash=rule_parameters_hash,
        policy_identity=policy_identity,
        candidates=len(ordered),
        effective_observations=effective,
        intraclass_correlation=icc,
        design_effect=deff,
        verdict=verdict,
        mean_net_excess_bps=mean,
        detail=(
            f"{len(ordered)} refused candidates would have averaged {mean}bps net of costs, "
            f"benchmark-relative, over {effective} effective observations"
        ),
    )


def group_keys(samples: Sequence[GateOutcomeSample]) -> tuple[tuple[str, int, str, str], ...]:
    """Every distinct `(gate, version, parameters, policy)` present, sorted.

    Exposed so a caller iterates the groups that exist rather than assuming a set, which is how
    an unnoticed second rule version ends up pooled into the first.
    """
    return tuple(
        sorted(
            {
                (
                    sample.gate_id,
                    sample.gate_version,
                    sample.rule_parameters_hash,
                    sample.policy_identity,
                )
                for sample in samples
            }
        )
    )


def report_all(
    samples: Sequence[GateOutcomeSample],
    *,
    minimum_effective: int = MINIMUM_EFFECTIVE_OBSERVATIONS,
) -> Mapping[tuple[str, int, str, str], GateValueReport]:
    """One report per distinct group. Never a single pooled answer."""
    return {
        key: report_gate_value(
            samples,
            gate_id=key[0],
            gate_version=key[1],
            rule_parameters_hash=key[2],
            policy_identity=key[3],
            minimum_effective=minimum_effective,
        )
        for key in group_keys(samples)
    }


__all__ = [
    "GateOutcomeSample",
    "GateValueReport",
    "GateVerdict",
    "group_keys",
    "report_all",
    "report_gate_value",
]
