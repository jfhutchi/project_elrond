"""The inputs a capacity answer rests on, assembled from state rather than typed by a human (#51).

`assess_capital_capacity` has been correct and unreachable. It takes a turnover, a per-instrument
allocation and a per-instrument traded volume, and nothing produced them -- so the milestone gate
received `None` and reported the question as unassessed, permanently. #51's remaining gap was
never the arithmetic; it was that no path fed it.

**Every field says whether it was observed or modelled.** A capacity verdict computed from a
modelled turnover has the authority of an assumption and the appearance of a measurement, which
is the exact failure #51 exists to prevent one level up from the cost model. So provenance
travels per field rather than per artifact, and the assessment reports which of its inputs were
which.

**Modelled values are project defaults, not operator preferences.** #72 settled that an agent may
choose a conservative, reversible, versioned default rather than wait for someone to type a
number. Each one here is derived from something already written down -- the strategy's own
configured roster, its own indicator periods -- so it moves when the strategy moves instead of
becoming a second declaration that drifts.

**Nothing is substituted for a missing measurement.** A symbol with no stored volume is absent
from the allocation, and an absent allocation blocks the assessment. That is the same fail-closed
rule the risk layer runs on, and it is why this module can return an artifact that says almost
nothing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.research.capital import InstrumentCapacity

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class InputProvenance(StrEnum):
    """Whether a value was measured or derived from a declared assumption."""

    #: Read from durable state: stored bars, the ledger, a frozen config.
    OBSERVED = "OBSERVED"
    #: Derived from a versioned project default, because the measurement does not exist yet.
    #: Never presented as a measurement, and always visible in the assessment.
    MODELLED = "MODELLED"


class TurnoverBasis(StrEnum):
    """Where the round-trip count came from."""

    #: Counted from durable fills. Available only once a strategy has traded enough to count.
    LEDGER_FILLS = "LEDGER_FILLS"
    #: Implied by the strategy's own exit period: a position held `exit_period` sessions turns
    #: over roughly `sessions_per_year / exit_period` times. Derived from the frozen config, so
    #: it moves when the strategy moves rather than sitting as a separate constant.
    CONFIGURED_HOLDING_PERIOD = "CONFIGURED_HOLDING_PERIOD"


#: Trading sessions in a year, for converting a holding period into round trips. 252 is the
#: conventional US equity count and is used only where turnover is already `MODELLED`.
SESSIONS_PER_YEAR = Decimal("252")

#: How many sessions of stored volume a symbol needs before its liquidity counts as observed.
#: Twenty matches the `minimum_volume_sessions` the risk layer already requires before it will
#: let a symbol be traded at all, so the two gates cannot disagree about what "enough" means.
MINIMUM_VOLUME_SESSIONS = 20


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class InstrumentAllocation(FrozenModel):
    """One instrument's share and the liquidity behind it, each with its own provenance."""

    symbol: Text
    weight: Decimal = Field(gt=0, le=1)
    weight_provenance: InputProvenance
    #: Median dollar volume across the observed window. Median rather than mean because one
    #: extraordinary session should not raise the liquidity a strategy is assumed to have.
    session_volume_usd: Decimal = Field(gt=0)
    volume_provenance: InputProvenance
    volume_sessions: int = Field(ge=0)

    def as_capacity(self) -> InstrumentCapacity:
        return InstrumentCapacity(weight=self.weight, session_volume_usd=self.session_volume_usd)


class CapacityInputs(FrozenModel):
    """Everything the ladder needs, frozen and content-addressed with its provenance intact."""

    version: int = Field(default=1, ge=1)
    strategy_id: Text
    configuration_hash: Text
    objective_identity: Text
    cost_model_identity: Text
    as_of: date
    #: Where the price and volume observations came from, so a provider change is new inputs.
    market_data_source: Text
    #: Round trips a year. `None` blocks the assessment rather than defaulting.
    turnover: Decimal | None = Field(default=None, gt=0)
    turnover_basis: TurnoverBasis | None = None
    turnover_provenance: InputProvenance | None = None
    #: Benchmark-relative excess before costs. `None` blocks: there is nothing for capacity to
    #: erode, and inventing one would manufacture the edge the ladder is meant to test.
    gross_excess_bps: Decimal | None = None
    gross_excess_provenance: InputProvenance | None = None
    allocations: tuple[InstrumentAllocation, ...] = ()
    #: Why an input is missing, in the operator's terms. Empty when everything resolved.
    unavailable: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_inputs(self) -> Self:
        if self.turnover is not None and (
            self.turnover_basis is None or self.turnover_provenance is None
        ):
            raise ValueError("a turnover must say where it came from and whether it was measured")
        if self.gross_excess_bps is not None and self.gross_excess_provenance is None:
            raise ValueError("a gross excess must say whether it was measured")
        symbols = [item.symbol for item in self.allocations]
        if len(symbols) != len(set(symbols)):
            raise ValueError("an instrument cannot carry two allocations")
        if self.allocations:
            total = sum((item.weight for item in self.allocations), Decimal("0"))
            if not (Decimal("0.99") <= total <= Decimal("1.01")):
                raise ValueError(
                    f"allocation weights total {total}, which is not a portfolio; a capacity "
                    "answer computed from weights that do not sum to one is measuring a "
                    "different strategy"
                )
        return self

    @property
    def complete(self) -> bool:
        """Whether the ladder can run at all. False means the assessment is `UNASSESSED`."""
        return (
            self.turnover is not None
            and self.gross_excess_bps is not None
            and bool(self.allocations)
        )

    @property
    def modelled_inputs(self) -> tuple[str, ...]:
        """Every field that rests on a project default rather than a measurement."""
        modelled: list[str] = []
        if self.turnover_provenance is InputProvenance.MODELLED:
            basis = self.turnover_basis.value if self.turnover_basis else "unstated basis"
            modelled.append(f"turnover ({basis})")
        if self.gross_excess_provenance is InputProvenance.MODELLED:
            modelled.append("gross_excess_bps")
        if any(item.weight_provenance is InputProvenance.MODELLED for item in self.allocations):
            modelled.append("instrument weights")
        if any(item.volume_provenance is InputProvenance.MODELLED for item in self.allocations):
            modelled.append("instrument volumes")
        return tuple(modelled)

    def instruments(self) -> Mapping[str, InstrumentCapacity]:
        return {item.symbol: item.as_capacity() for item in self.allocations}

    @property
    def identity(self) -> str:
        payload = json.loads(self.model_dump_json())
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"capacity-inputs-v{self.version}-{digest[:16]}"


def median_dollar_volume(
    closes: Sequence[Decimal], volumes: Sequence[Decimal]
) -> Decimal | None:
    """Median dollar volume across the supplied sessions, or `None` when there are none.

    Median rather than mean: one extraordinary session should not raise the liquidity a strategy
    is assumed to have, and the whole point of this number is what a book absorbs on an ordinary
    day.
    """
    if not closes or len(closes) != len(volumes):
        return None
    dollars = sorted(close * volume for close, volume in zip(closes, volumes, strict=True))
    middle = len(dollars) // 2
    if len(dollars) % 2 == 1:
        return dollars[middle]
    return (dollars[middle - 1] + dollars[middle]) / 2


def turnover_from_holding_period(exit_period: int) -> Decimal:
    """Round trips a year implied by holding a position for `exit_period` sessions.

    A project default, derived from the strategy's own configured exit period rather than chosen
    independently, so it moves when the strategy moves. Modelled, never presented as measured.
    """
    if exit_period <= 0:
        raise ValueError("a holding period must be positive to imply a turnover")
    return (SESSIONS_PER_YEAR / Decimal(exit_period)).quantize(Decimal("0.01"))


__all__ = [
    "MINIMUM_VOLUME_SESSIONS",
    "SESSIONS_PER_YEAR",
    "CapacityInputs",
    "InputProvenance",
    "InstrumentAllocation",
    "TurnoverBasis",
    "median_dollar_volume",
    "turnover_from_holding_period",
]
