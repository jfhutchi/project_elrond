"""What an instrument actually is, including the ones that stopped existing (#17).

Two distinctions this project has already been bitten by.

**A listing is not an economic exposure.** Cycle 17 tested 22 global country indices through
US-listed ETFs, so every return measured carries unhedged currency risk that was never part of
the hypothesis -- and `REFUTED.md` #12 found FX itself unusable at Sharpe -0.14. "Exposure to
the Japanese equity market" and "USD-denominated ETF tracking it" are different instruments
with different risk, so they are different fields here rather than one ticker string.

**A universe is a function of time, not a list.** A membership set built from what trades today
silently deletes everything that failed, which biases every cross-sectional result in the same
optimistic direction. Cycle 10 was only interpretable because delisted history was available:
511 names including 241 that stopped trading, and whole-market momentum produced more return
with *less* Sharpe (0.63 against SPY's 0.80). Without the delisted names it would have looked
like a winner. So membership is `as_of`, and a delisted instrument is present with its
termination date rather than absent.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from datetime import date
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from quantbot.domain import TradingCalendar

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"
    CRYPTO = "CRYPTO"
    FX = "FX"
    #: Economic series such as FRED releases. Revised after publication, so vintages matter.
    MACRO = "MACRO"
    COMMODITY = "COMMODITY"
    #: Implied volatility, skew, put/call ratios. Refuted #9 killed *trading* options at this
    #: account size; using options-implied information as a signal has never been tested here.
    OPTIONS_DERIVED = "OPTIONS_DERIVED"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Instrument(FrozenModel):
    """One tradable or observable thing, identified independently of its current ticker."""

    #: Stable across ticker changes. A symbol is reused by later companies; this is not.
    instrument_id: Text
    #: The listing ticker as of `listed_on`. Not an identity.
    symbol: Text
    venue: Text
    asset_class: AssetClass
    #: The currency it settles in.
    quote_currency: Text = "USD"
    #: The currency of the risk it actually carries. EWJ quotes USD and exposes JPY.
    exposure_currency: Text = "USD"
    #: The market whose returns it tracks, e.g. "US equity", "JP equity", "BTC".
    exposure_market: Text
    calendar: TradingCalendar = "XNYS"
    listed_on: date
    #: Set when the instrument stopped trading. Present rather than deleted: a universe that
    #: drops these is survivorship-biased in the flattering direction.
    delisted_on: date | None = None

    @model_validator(mode="after")
    def validate_life(self) -> Self:
        if self.delisted_on is not None and self.delisted_on < self.listed_on:
            raise ValueError("an instrument cannot be delisted before it listed")
        for field in ("quote_currency", "exposure_currency"):
            value: str = getattr(self, field)
            if value != value.upper():
                raise ValueError(f"{field} must be uppercase")
        return self

    @property
    def carries_currency_risk(self) -> bool:
        """True when holding this exposes the account to FX it did not ask for."""
        return self.quote_currency != self.exposure_currency

    def tradable_on(self, day: date) -> bool:
        if day < self.listed_on:
            return False
        return self.delisted_on is None or day <= self.delisted_on


class InstrumentUniverse:
    """A membership set that is a function of time, so survivorship cannot be silent."""

    def __init__(self, instruments: Iterable[Instrument]) -> None:
        self._instruments = tuple(instruments)
        identifiers = [instrument.instrument_id for instrument in self._instruments]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("instrument ids must be unique within a universe")

    def __len__(self) -> int:
        return len(self._instruments)

    def __iter__(self) -> Iterator[Instrument]:
        return iter(self._instruments)

    def members(self, as_of: date) -> tuple[Instrument, ...]:
        """Everything tradable on that date, including names that later stopped trading."""
        return tuple(
            instrument for instrument in self._instruments if instrument.tradable_on(as_of)
        )

    def survivors(self, as_of: date) -> tuple[Instrument, ...]:
        """Members of `as_of` that were still trading at the end of the record.

        Provided so the difference between this and `members` can be measured rather than
        assumed. If a cross-sectional result changes materially between the two, the result is
        about survivorship.
        """
        return tuple(
            instrument for instrument in self.members(as_of) if instrument.delisted_on is None
        )

    def survivorship_bias(self, as_of: date) -> float:
        """Share of the `as_of` universe that a live-only listing would have dropped."""
        members = self.members(as_of)
        if not members:
            return 0.0
        return 1.0 - len(self.survivors(as_of)) / len(members)

    def unhedged(self) -> tuple[Instrument, ...]:
        """Instruments whose returns carry currency risk the hypothesis may not have declared."""
        return tuple(
            instrument for instrument in self._instruments if instrument.carries_currency_risk
        )

    def by_asset_class(self, asset_class: AssetClass) -> tuple[Instrument, ...]:
        return tuple(
            instrument
            for instrument in self._instruments
            if instrument.asset_class is asset_class
        )

    def exposure_markets(self) -> frozenset[str]:
        return frozenset(instrument.exposure_market for instrument in self._instruments)


class CorporateActionKind(StrEnum):
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"
    SPIN_OFF = "SPIN_OFF"
    #: The ticker changed; the instrument did not.
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    DELISTING = "DELISTING"


class CorporateAction(FrozenModel):
    """An event that changes what a price series means, recorded so it stays reproducible."""

    instrument_id: Text
    kind: CorporateActionKind
    #: The session the action takes effect on.
    effective_on: date
    #: When the action became knowable. Announcements precede effect, and a backtest that
    #: adjusts before this date is using information it did not have.
    announced_on: date
    #: Split or dividend factor, as a decimal string. Empty for symbol changes.
    factor: str = ""
    detail: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_action(self) -> Self:
        if self.announced_on > self.effective_on:
            raise ValueError("a corporate action cannot take effect before it was announced")
        needs_factor = (CorporateActionKind.SPLIT, CorporateActionKind.DIVIDEND)
        if self.kind in needs_factor and not self.factor:
            raise ValueError(f"a {self.kind.value} must carry its factor")
        return self


def universe_from(instruments: Sequence[Instrument]) -> InstrumentUniverse:
    return InstrumentUniverse(instruments)


__all__ = [
    "AssetClass",
    "CorporateAction",
    "CorporateActionKind",
    "Instrument",
    "InstrumentUniverse",
    "universe_from",
]
