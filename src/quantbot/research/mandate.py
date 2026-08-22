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

**`PROVISIONAL` is load-bearing.** `config/economic-objective-v1.yaml` transcribes rules already
recorded in `STATUS.md` and `CLAUDE.md`, which is why writing it down is bookkeeping rather than
an agent setting the project's goals. Transcription is not ratification, though, and the
difference is exactly what decides whether a candidate may be put in front of a human for live
review. So it is a field, `ratified()` is a method, and an unratified mandate freezes the
objective without authorising anything.
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
    #: Written down, not yet ratified. Freezes the objective; authorises nothing.
    PROVISIONAL = "PROVISIONAL"
    #: The operator has accepted it. Only this may back a live-review claim.
    RATIFIED = "RATIFIED"
    #: A later version supersedes it. Kept because candidates assessed under it name it.
    SUPERSEDED = "SUPERSEDED"


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
        """SHA256 of the canonical objective, so a candidate can name what it was judged under."""
        payload: dict[str, Any] = json.loads(self.model_dump_json())
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    @property
    def identity(self) -> str:
        return f"economic-objective-v{self.version}-{self.content_hash[:16]}"

    def ratified(self) -> bool:
        return self.status is MandateStatus.RATIFIED

    def live_review_objections(self) -> tuple[str, ...]:
        """Why this mandate cannot yet back a human live-review claim.

        Separated from `ratified()` because the caller needs the reason, not the boolean: an
        operator told "not eligible" without being told the objective is still unratified will
        go looking for a defect in the evidence.
        """
        if self.status is MandateStatus.SUPERSEDED:
            return (
                f"{self.identity} is superseded; a candidate judged under it was judged against "
                "an objective the project has since replaced",
            )
        if not self.ratified():
            return (
                f"{self.identity} is {self.status.value}: the economic objective it freezes was "
                "transcribed from the project's own documents rather than ratified by the "
                "operator, and only the operator decides what this project is optimising",
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
    "EconomicObjective",
    "MandateError",
    "MandateStatus",
    "Objective",
    "load_economic_objective",
]
