"""Point-in-time exposure classification for the traded universe (#59, slice 1).

`vetoes.py` shipped `SECTOR_CONCENTRATION` as a name with no evaluator, blocked on this. The
reason recorded there was that classifying a historical position by a symbol's sector *as of
today* is look-ahead inside the risk layer, and a backtest of whether the veto helped would then
use information the veto could not have had.

**That argument is right for single stocks and does not bind for this universe.** Every one of
the 23 instruments in `config/strategy-v1-2.yaml` is an ETF, and an ETF's exposure is its
published mandate rather than a vendor's estimate of a company's business. There is nothing to
revise and nothing to license. What remains true is that mandates *change* -- Communication
Services was created in September 2018 and constituents left XLK -- so the table carries an
`effective_from` and refuses to classify any decision earlier than it.

**Concentration, not sector.** The veto this feeds is named for exposure rather than sector
because a sector-shaped rule would not catch this portfolio's actual concentration. The roster
holds 10 of 23, and the dangerous states are not "too much technology": SPY + QQQ + XLK + IWM +
MDY is one exposure wearing five tickers, TLT + IEF + TIP is duration, and a sector table would
rate the first of those as perfectly diversified across zero overlapping sectors. A veto that
reports a limit as enforced while the concentration it exists to catch walks through it is worse
than no veto.

**A symbol may carry several exposures and counts in each.** That is conservative in the only
safe direction: more ways to trip a limit, never fewer.

**Two kinds of claim, kept apart (Sol, #59).** A mandate exposure is what a fund *is*, read off
the issuer's material -- definitional, unlicensed, not subject to revision. A factor exposure is
what a fund *behaves like* under stress: HYG's equity beta in a drawdown is real, and is exactly
why a concentration limit should see it, but it is an empirical claim assigned by a declared
method rather than a prospectus. The factor model carries its own method, as-of date and version,
so a later counterfactual study can say which mechanism a veto actually used.

**Provenance belongs to the assignment, not to the group.** `US_EQUITY` is a mandate claim for SPY
and a factor claim for HYG. The exposure means the same thing either way; what differs is how much
evidence stands behind saying this instrument has it -- so it is recorded per edge.

**Unknown blocks.** A symbol absent from the table, or a decision dated before the table was
verified, is `UNKNOWN`. It must reject rather than proceed, because an unclassified instrument
belongs to no group and would therefore add to no group's concentration -- passing every limit by
virtue of not being recognised. That is the fail-open direction this project keeps finding, and
it is the same rule as `unassessed` blocking everywhere else here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from quantbot.domain import Position

FiniteDecimal = Annotated[Decimal, Field(allow_inf_nan=False)]

DEFAULT_CLASSIFICATION_PATH = Path("config/exposure-classification-v1.yaml")


class ExposureGroup(StrEnum):
    """Exposures an instrument can carry, by mandate.

    Asset class and region sit beside sector deliberately. Sector alone cannot express that SPY
    and XLK overlap, which is the concentration most available to this strategy.
    """

    US_EQUITY = "US_EQUITY"
    INTERNATIONAL_EQUITY = "INTERNATIONAL_EQUITY"
    EMERGING_EQUITY = "EMERGING_EQUITY"
    REAL_ESTATE = "REAL_ESTATE"
    COMMODITY = "COMMODITY"
    PRECIOUS_METAL = "PRECIOUS_METAL"
    #: Holds US Treasury securities. A statement about what is *in* the fund; rate sensitivity
    #: is a behaviour and lives in `DURATION_FACTOR`.
    TREASURY = "TREASURY"
    INFLATION_LINKED = "INFLATION_LINKED"
    CREDIT = "CREDIT"
    #: Interest-rate sensitivity. A factor rather than a holding: LQD is rate-sensitive and owns
    #: no Treasuries, so a book of TLT + IEF + LQD is more concentrated than its mandates suggest.
    DURATION_FACTOR = "DURATION_FACTOR"

    MATERIALS = "MATERIALS"
    ENERGY = "ENERGY"
    FINANCIALS = "FINANCIALS"
    INDUSTRIALS = "INDUSTRIALS"
    TECHNOLOGY = "TECHNOLOGY"
    CONSUMER_STAPLES = "CONSUMER_STAPLES"
    UTILITIES = "UTILITIES"
    HEALTH_CARE = "HEALTH_CARE"
    CONSUMER_DISCRETIONARY = "CONSUMER_DISCRETIONARY"


class Provenance(StrEnum):
    """How an instrument came to be assigned an exposure.

    Recorded because the two carry different evidence, and a veto that cannot say which one it
    acted on is a veto whose basis a later study cannot reconstruct.
    """

    #: Read off the issuer's published mandate. Definitional.
    MANDATE = "MANDATE"
    #: Assigned by the declared stress model. Empirical, and weaker.
    FACTOR = "FACTOR"


class FactorModel(BaseModel):
    """Empirical stress assignments, versioned apart from the mandate table.

    No fitted parameters: the assignments are declared and documented, for the same reason the
    capital-capacity stress shape is. A coefficient nobody measured, carried around with the
    authority of one, is the failure both modules are built to refuse.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    method: str = Field(min_length=1)
    as_of: date
    version: int = Field(ge=1)
    assignments: Mapping[str, frozenset[ExposureGroup]]

    @property
    def identity(self) -> str:
        payload = {
            "method": self.method,
            "as_of": self.as_of.isoformat(),
            "version": self.version,
            "assignments": {
                symbol: sorted(group.value for group in groups)
                for symbol, groups in sorted(self.assignments.items())
            },
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"factor-model-v{self.version}-{digest[:16]}"


class ClassificationError(RuntimeError):
    """Raised rather than falling back. A missing table must not become a permissive one."""


class ExposureClassification(BaseModel):
    """The frozen, versioned mapping, hashed so a study can name the definition it used.

    Sol's requirement on #57 and #59 is the same one: two studies across a definition change are
    two studies. `identity` is what makes that mechanical.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    #: Where the mandates were read from. Provenance, not decoration: a hard veto depending on an
    #: unattributed table is a rule nobody can check.
    source: str = Field(min_length=1)
    #: The date the mandates were verified. **Not** an inception date, and classification is
    #: refused before it -- see the module docstring on why this is deliberately conservative.
    effective_from: date
    #: What each fund holds by its published mandate.
    mandate: Mapping[str, frozenset[ExposureGroup]]
    #: What each fund behaves like under stress. Optional, and its absence is not neutrality --
    #: it means no empirical claim has been registered, so only mandate exposures are measured.
    factor_model: FactorModel | None = None

    @property
    def symbols(self) -> Mapping[str, frozenset[ExposureGroup]]:
        """Every symbol the table can classify, and every exposure it carries either way."""
        merged = {symbol: set(groups) for symbol, groups in self.mandate.items()}
        if self.factor_model is not None:
            for symbol, groups in self.factor_model.assignments.items():
                merged.setdefault(symbol, set()).update(groups)
        return {symbol: frozenset(groups) for symbol, groups in merged.items()}

    @model_validator(mode="after")
    def validate_classification(self) -> Self:
        if self.factor_model is not None:
            unknown = sorted(set(self.factor_model.assignments) - set(self.mandate))
            if unknown:
                # A factor assignment for an instrument with no mandate row would classify a
                # symbol entirely on the weaker kind of claim, which is the direction that must
                # not happen silently.
                raise ValueError(
                    f"{', '.join(unknown)} carries a factor assignment with no mandate entry; an "
                    "instrument cannot be classified on stress behaviour alone"
                )
        for symbol, groups in self.mandate.items():
            if not symbol or symbol != symbol.strip().upper():
                raise ValueError(f"{symbol!r} must be uppercase without surrounding whitespace")
            if not groups:
                # An empty group set reads as "classified" while behaving as unclassified, which
                # is the fail-open shape. A symbol with no known exposure must be absent instead.
                raise ValueError(
                    f"{symbol} is listed with no exposure groups; omit it so it classifies as "
                    "unknown and blocks, rather than being classified into nothing and passing"
                )
        return self

    @property
    def identity(self) -> str:
        payload = {
            "version": self.version,
            "source": self.source,
            "effective_from": self.effective_from.isoformat(),
            "mandate": {
                symbol: sorted(group.value for group in groups)
                for symbol, groups in sorted(self.mandate.items())
            },
            # The factor model's identity is folded in rather than its contents, so a change to
            # either half moves the classification identity and two studies stay two studies.
            "factor_model": None if self.factor_model is None else self.factor_model.identity,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return f"exposure-classification-v{self.version}-{digest[:16]}"

    def groups_for(self, symbol: str, *, as_of: date) -> frozenset[ExposureGroup] | None:
        """The symbol's exposures, or `None` when it is unknown as of that date.

        `None` is the unknown state and every caller must treat it as blocking. A decision dated
        before `effective_from` is unknown however well-listed the symbol is: applying a mapping
        backwards is a claim about a composition nobody checked.
        """
        if as_of < self.effective_from:
            return None
        return self.symbols.get(symbol)

    def provenance_for(
        self, symbol: str, *, as_of: date
    ) -> Mapping[ExposureGroup, Provenance] | None:
        """Each exposure and how it was assigned, or `None` when the symbol is unknown.

        A group claimed by both halves reports `MANDATE`: the stronger evidence wins the label,
        because saying a definitional exposure was empirically inferred understates what is known.
        """
        if self.groups_for(symbol, as_of=as_of) is None:
            return None
        found: dict[ExposureGroup, Provenance] = {}
        if self.factor_model is not None:
            for group in self.factor_model.assignments.get(symbol, frozenset()):
                found[group] = Provenance.FACTOR
        for group in self.mandate.get(symbol, frozenset()):
            found[group] = Provenance.MANDATE
        return found


def load_exposure_classification(
    path: str | Path = DEFAULT_CLASSIFICATION_PATH,
) -> ExposureClassification:
    """Read the frozen table, refusing anything it cannot read as one.

    Raises rather than returning a default, for the reason `load_economic_objective` does: a
    fallback assembled in code would be an agent deciding what the portfolio's exposures are at
    the exact moment nobody is watching.
    """
    location = Path(path)
    try:
        raw: Any = yaml.safe_load(location.read_text(encoding="utf-8"))
    except OSError as error:
        raise ClassificationError(
            f"no exposure classification at {location}: concentration cannot be measured against "
            "a table that is not there, and an absent table must not read as an empty one"
        ) from error
    except yaml.YAMLError as error:
        raise ClassificationError(f"{location} is not valid YAML: {error}") from error
    if not isinstance(raw, dict):
        raise ClassificationError(f"{location} must be a YAML mapping")
    try:
        return ExposureClassification.model_validate(raw)
    except ValueError as error:
        raise ClassificationError(
            f"{location} is not a usable exposure classification: {error}"
        ) from error


class ExposureState(BaseModel):
    """Committed exposure per group, the order's groups, and anything that could not be classified.

    Assembled from measured positions rather than asserted by a caller. `unclassified` is carried
    explicitly instead of being dropped: a committed position nobody could classify makes every
    group total an understatement, and an understated numerator is the permissive direction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification_identity: str
    order_groups: frozenset[ExposureGroup] | None
    #: How each of the order's exposures was assigned. Empty when the order is unclassified.
    #: Carried so a rejection can say whether it acted on a prospectus or on a stress model --
    #: #57 compares refusals, and two refusals under different mechanisms are not one dataset.
    order_provenance: Mapping[ExposureGroup, Provenance] = {}
    committed_by_group: Mapping[ExposureGroup, FiniteDecimal]
    #: Symbols held or ordered that the table could not classify as of the session.
    unclassified: tuple[str, ...]


def build_exposure_state(
    *,
    classification: ExposureClassification,
    symbol: str,
    positions: Sequence[Position],
    as_of: date,
) -> ExposureState:
    """Reduce measured positions onto exposure groups as of a session.

    Takes positions rather than a session handle, like every other input builder here: a risk
    rule that fetched its own inputs would decide what it was allowed to see.

    A position counts once in every group its symbol carries, and its **absolute** market value is
    what accumulates. Netting a long against a short across two instruments that share an exposure
    would report the concentration away.
    """
    committed: dict[ExposureGroup, Decimal] = {}
    unclassified: list[str] = []

    order_groups = classification.groups_for(symbol, as_of=as_of)
    order_provenance = classification.provenance_for(symbol, as_of=as_of) or {}
    if order_groups is None:
        unclassified.append(symbol)

    for position in positions:
        groups = classification.groups_for(position.symbol, as_of=as_of)
        if groups is None:
            unclassified.append(position.symbol)
            continue
        value = abs(position.market_value)
        for group in groups:
            committed[group] = committed.get(group, Decimal("0")) + value

    return ExposureState(
        classification_identity=classification.identity,
        order_groups=order_groups,
        order_provenance=order_provenance,
        committed_by_group=committed,
        # Sorted and de-duplicated so the reason text is stable across runs; a rejection reason
        # that reorders between identical inputs is a rejection two studies cannot group on.
        unclassified=tuple(sorted(set(unclassified))),
    )


__all__ = [
    "DEFAULT_CLASSIFICATION_PATH",
    "ClassificationError",
    "ExposureClassification",
    "ExposureGroup",
    "ExposureState",
    "FactorModel",
    "Provenance",
    "build_exposure_state",
    "load_exposure_classification",
]
