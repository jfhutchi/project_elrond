"""The project-level economic objective, frozen before anything is compared against it (#54).

Every experiment here already freezes its own estimand. This freezes the level above it -- which
strategy the project would rather deploy -- because that is where metric shopping actually
happens, and the repository's own record is the demonstration:

* SPY buy-and-hold wins on terminal wealth (15.36% CAGR, $454.70 per $100).
* SPY_SMA200 wins on Sharpe (0.91) and halves the drawdown, and loses 4.75 CAGR points.
* Vol targeting reduces drawdown on 16 of 18 assets and costs ~1.83 CAGR points a year.
* Leverage raises CAGR and breaches the drawdown rule.

Not one of those comparisons is decidable by statistics. Which is better is a question about
what the operator wants, and until it is answered in advance a research system can search not
only strategies but *definitions of success* -- and it will find one, because with four metrics
and four candidates something always wins on something.

**A file, not a table.** It is operator policy, so it lives where the operator reads and edits
it, in the same shape as the strategy configs beside it. Git makes a change visible as a diff
and the content hash makes it visible to the code, and both are needed for the actual
requirement: a change creates a new version prospectively and never retroactively relabels
evidence gathered under the old one.

**`PROVISIONAL` is load-bearing, and it is not merely bookkeeping.** An earlier version of this
module said every value in `config/economic-objective-v1.yaml` was transcribed from rules already
recorded in `STATUS.md` and `CLAUDE.md`. That was too strong, and #54's review said so: the
drawdown ceiling, the leverage cap and the benchmark are transcriptions, but the intended capital
range, the 100bps improvement threshold, the turnover cap, the three-year horizon and the choice
of `BENCHMARK_RELATIVE_GROWTH` over a drawdown-constrained growth objective are **proposals**. They
are defensible; they are still policy an agent picked.

So an unratified mandate freezes the objective and authorises nothing -- not live review, and
**not `PAPER_QUALIFIED` either**. `PAPER_QUALIFIED` is already an economic-success label, and an
agent that can write the hurdle and then announce its evidence cleared it has not been gated at
all; it has been asked politely.

**Ratification does not change the objective's identity.** `content_hash` covers the economic
content only -- the objective, the benchmark, the constraints, the thresholds -- and excludes
`status`, `ratified_by`, `ratified_at` and the authorship fields. That is the lifecycle #54 asked
to be made explicit rather than left accidental: ratifying is the operator accepting a fixed set
of policy values, so it must not silently turn one economic objective into a different one and
invalidate evidence gathered under it. Changing any *policy* value does change the identity, which
is the half that has to keep working.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Where the current objective lives. A default rather than a constant a caller must know,
#: because a gate that silently proceeds when nobody passed a mandate is the failure this
#: module exists to prevent.
DEFAULT_MANDATE_PATH = Path("config/economic-objective-v1.yaml")


class MandateStatus(StrEnum):
    """Who stands behind the numbers this objective freezes.

    The distinction that matters is **provenance, not permission** (#72). All three of the usable
    states freeze the objective identically; what differs is what a reader should conclude when
    they see evidence measured against one.
    """

    #: A draft. Freezes nothing anyone should rely on and backs no qualification claim.
    PROVISIONAL = "PROVISIONAL"
    #: Chosen by the system, frozen, versioned, and honest about its authorship. Sufficient for
    #: research, shadow and paper qualification -- the numbers are as fixed as a ratified
    #: objective's, and a hurdle set before the evidence is still a hurdle. It is **not** an
    #: approval, and anything measured against it says so.
    PROJECT_DEFAULT = "PROJECT_DEFAULT"
    #: A human accepted it. Never reachable from code, and never synthesised from
    #: `PROJECT_DEFAULT`: a status an agent can write is not a signature.
    RATIFIED = "RATIFIED"
    #: A later version supersedes it. Kept because candidates assessed under it name it.
    SUPERSEDED = "SUPERSEDED"


#: Statuses that may back a qualification claim. `PROVISIONAL` cannot, because a draft nobody
#: froze deliberately is not a bar; `SUPERSEDED` cannot, because the project has moved on.
USABLE_STATUSES: frozenset[MandateStatus] = frozenset(
    {MandateStatus.PROJECT_DEFAULT, MandateStatus.RATIFIED}
)


class Objective(StrEnum):
    """What is being maximised, subject to the constraints. Exactly one.

    Deliberately not a weighted combination. A weighted score needs coefficients, and
    coefficients nobody can defend are metric shopping with extra arithmetic -- the weights get
    chosen, consciously or not, once the candidates are on the table.
    """

    #: Terminal wealth. Ignores that a fractional index position reproduces most of it.
    TERMINAL_WEALTH = "TERMINAL_WEALTH"
    #: Growth over the benchmark's. What is left once market exposure is paid for.
    BENCHMARK_RELATIVE_GROWTH = "BENCHMARK_RELATIVE_GROWTH"
    #: Sharpe or equivalent. Beware: a strategy that barely trades scores well by not trading.
    RISK_ADJUSTED_RETURN = "RISK_ADJUSTED_RETURN"


class MandateError(ValueError):
    """Raised when the objective cannot be read, or reads as something it must not."""


#: Fields describing the mandate's *lifecycle and provenance* rather than its economic content.
#: Excluded from `content_hash` so that ratifying a set of policy values does not change which
#: set of policy values it is (#54). A candidate assessed under identity X stays assessed under
#: identity X when the operator signs it; the alternative silently invalidates evidence at the
#: exact moment the objective becomes usable.
LIFECYCLE_FIELDS = frozenset(
    {"status", "ratified_by", "ratified_at", "authored_by", "authored_at"}
)

#: Substrings that mark a ratifier as automated. Ratification is the one act in this file that
#: only a human may perform, and an agent naming itself as the ratifier is the honest version of
#: the failure -- so it is the version that gets refused. This does not stop an agent typing the
#: operator's name; nothing in software can. It stops ratification happening as a side effect of
#: an agent tidying a config, which is how it would actually occur.
#:
#: Substring matching over-matches -- "Solomon" and "Abbott" contain "sol" and "bot" -- and that
#: direction is deliberate. A false refusal costs a human one edit and says exactly why; a false
#: acceptance is an agent having set this project's definition of success. The error names the
#: substring it matched so the fix is obvious.
_AUTOMATED_RATIFIER = (
    "claude",
    "gpt",
    "opus",
    "sonnet",
    "haiku",
    "codex",
    "sol",
    "agent",
    "llm",
    "bot",
    "copilot",
    "assistant",
    "automation",
)


class EconomicObjective(BaseModel):
    """The frozen deployment mandate. Immutable, versioned, and hashed.

    `extra="forbid"` matters more here than in most models: a key nobody consumes reads like a
    constraint in review and constrains nothing, and this file is exactly the kind that grows
    aspirational fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    status: MandateStatus
    ratified_by: str | None = None
    ratified_at: date | None = None
    authored_by: Text
    authored_at: date

    objective: Objective
    benchmark_symbol: Text
    simplest_compliant_baseline: Text

    target_capital_usd: int = Field(gt=0)
    intended_capital_range_usd: tuple[int, int]
    permitted_asset_classes: tuple[Text, ...] = Field(min_length=1)

    max_drawdown_bps: int = Field(gt=0, le=10000)
    max_gross_exposure_bps: int = Field(gt=0)
    max_leverage: Decimal = Field(gt=0, allow_inf_nan=False)

    minimum_meaningful_improvement_bps: int = Field(gt=0)
    max_annual_turnover: int = Field(ge=0)
    assumed_round_trip_cost_bps: Decimal = Field(ge=0, allow_inf_nan=False)
    evaluation_horizon_years: int = Field(gt=0)
    tax_treatment: Text

    @model_validator(mode="after")
    def validate_mandate(self) -> Self:
        low, high = self.intended_capital_range_usd
        if low <= 0 or high < low:
            raise ValueError("intended capital range must be positive and ordered")
        if not low <= self.target_capital_usd <= high:
            raise ValueError("target capital must sit inside the intended capital range")
        ratification = (self.ratified_by, self.ratified_at)
        if self.status is MandateStatus.RATIFIED and any(item is None for item in ratification):
            raise ValueError("a ratified mandate records who ratified it and when")
        if self.status is MandateStatus.RATIFIED:
            ratifier = (self.ratified_by or "").lower()
            automated = [mark for mark in _AUTOMATED_RATIFIER if mark in ratifier]
            if automated:
                raise ValueError(
                    f"{self.ratified_by!r} reads as an automated ratifier ({automated[0]!r}): "
                    "an agent may propose what this project optimises and may not ratify it, "
                    "because a hurdle written and signed by the same party is not a hurdle"
                )
        if self.status is not MandateStatus.RATIFIED and any(
            item is not None for item in ratification
        ):
            raise ValueError(
                "ratification details on an unratified mandate: one of the two is wrong, and "
                "guessing which would decide whether a candidate may reach a human"
            )
        if self.minimum_meaningful_improvement_bps <= 0:
            raise ValueError("a zero improvement threshold makes any difference meaningful")
        return self

    @property
    def content_hash(self) -> str:
        """SHA256 of the *economic content*, so a candidate can name what it was judged under.

        `LIFECYCLE_FIELDS` are excluded deliberately. The identity answers "which set of policy
        values was this measured against", and the operator signing an unchanged set of values
        does not change which set they are. Every policy field is inside the hash, so loosening
        the drawdown ceiling or the improvement threshold does produce a different objective --
        which is the direction that has to keep working.
        """
        payload: dict[str, Any] = json.loads(self.model_dump_json())
        content = {key: value for key, value in payload.items() if key not in LIFECYCLE_FIELDS}
        return hashlib.sha256(
            json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def identity(self) -> str:
        return f"economic-objective-v{self.version}-{self.content_hash[:16]}"

    def ratified(self) -> bool:
        """Whether a *human* accepted this. Narrow on purpose, and not the qualification gate."""
        return self.status is MandateStatus.RATIFIED

    def usable(self) -> bool:
        """Whether evidence may be measured against this and reported as clearing a bar.

        A project default qualifies. The reason the earlier design required ratification was that
        an agent must not choose a success criterion *after* seeing results -- and that is solved
        by freezing and versioning the objective beforehand, not by a signature. A frozen
        agent-authored bar and a frozen human-authored one are equally resistant to metric
        shopping; they differ in who to ask about the number, which is what `authority` reports.
        """
        return self.status in USABLE_STATUSES

    @property
    def authority(self) -> str:
        """Who stands behind these numbers, for any artifact measured against them.

        This is the safeguard that replaced the gate (#72). A reader of a qualification claim has
        to be able to tell "measured against criteria a human accepted" from "measured against
        criteria the system chose for itself", and the honest way to give them that is a label on
        the result rather than a stop before it.
        """
        if self.status is MandateStatus.RATIFIED:
            return f"RATIFIED by {self.ratified_by} on {self.ratified_at} ({self.identity})"
        if self.status is MandateStatus.PROJECT_DEFAULT:
            return (
                f"PROJECT_DEFAULT authored by {self.authored_by} ({self.identity}) -- chosen by "
                "the system, not approved by the operator"
            )
        return f"{self.status.value} ({self.identity})"

    def ratification_objections(self) -> tuple[str, ...]:
        """Why this mandate cannot back a claim that a strategy met the project's objective.

        Read wherever evidence is reported as clearing a bar. What it objects to changed with
        #72: a **draft** cannot back such a claim, and neither can a superseded objective, but a
        frozen `PROJECT_DEFAULT` can.

        The earlier version required human ratification here. That was the right instinct aimed
        at the wrong target -- what makes a bar honest is being frozen and versioned before the
        evidence, not being signed. Requiring a signature to reach `LIVE_REVIEW_ELIGIBLE` also
        made human review a prerequisite for its own eligibility, since reviewing the package is
        the event most likely to produce the ratification.

        The provenance distinction did not disappear; it moved to `authority`, which every
        artifact measured against this objective reports. A reader can then tell an
        agent-authored bar from an operator-authored one, which a gate never told them anyway.

        Separated from `usable()` because the caller needs the reason, not the boolean: an
        operator told "not eligible" without being told the objective is still a draft will go
        looking for a defect in the evidence.
        """
        if self.status is MandateStatus.SUPERSEDED:
            return (
                f"{self.identity} is superseded; a candidate judged under it was judged against "
                "an objective the project has since replaced",
            )
        if not self.usable():
            return (
                f"{self.identity} is {self.status.value}: the values it freezes -- the intended "
                "capital range, the minimum meaningful improvement, the turnover cap, the "
                "evaluation horizon and the choice of objective itself -- are a draft rather "
                "than a bar anything was measured against. Freeze it as PROJECT_DEFAULT or have "
                "the operator ratify it; a criterion still being edited is not a criterion",
            )
        return ()


def load_economic_objective(path: str | Path = DEFAULT_MANDATE_PATH) -> EconomicObjective:
    """Read the frozen objective, refusing anything it cannot read as one.

    Raises rather than returning a default. A missing mandate must not become a permissive one:
    the whole point is that no comparison happens until the question is written down, and a
    fallback objective assembled in code would be an agent choosing the project's goal at the
    exact moment nobody is watching.
    """
    location = Path(path)
    try:
        raw: Any = yaml.safe_load(location.read_text(encoding="utf-8"))
    except OSError as error:
        raise MandateError(
            f"no economic objective at {location}: nothing may be compared on return until the "
            "project has written down what it is optimising"
        ) from error
    except yaml.YAMLError as error:
        raise MandateError(f"{location} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise MandateError(f"{location} must be a YAML mapping")
    try:
        return EconomicObjective.model_validate(raw)
    except ValueError as error:
        raise MandateError(f"{location} is not a usable economic objective: {error}") from error


__all__ = [
    "DEFAULT_MANDATE_PATH",
    "LIFECYCLE_FIELDS",
    "USABLE_STATUSES",
    "EconomicObjective",
    "MandateError",
    "MandateStatus",
    "Objective",
    "load_economic_objective",
]
