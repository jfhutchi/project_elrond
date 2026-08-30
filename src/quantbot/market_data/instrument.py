"""One economic instrument, tracked across the symbols providers happen to call it (#83 part A).

A ticker is a label a venue currently uses, not an identity. It gets reassigned, changes on a
merger, and differs between providers for the same security. Every record in this project
currently keys on that string, so a ticker change silently splits one instrument's history into
two -- or worse, merges two instruments' histories into one when a symbol is recycled.

**The internal id is authoritative and is minted once.** Everything else in this repository is
content-addressed, and that is deliberately not done here: an identity hashed over its attributes
would change whenever an attribute changed, which is exactly what a durable identity must not do.
`instrument_id` is assigned at first sight and never recomputed. Attributes move around it.

**A resolver is a source, never the key.** OpenFIGI, SEC EDGAR or any future vendor supplies
evidence about an instrument; none of them defines it. That keeps Elrond able to record an
instrument nobody could resolve, which is the case that matters -- a resolver outage must not stop
a decision from being attributed.

**Mappings are historical, not current.** A symbol maps to an instrument over a validity window.
Rewriting an old event when a ticker changes would destroy the record of what the system actually
saw, and this project's whole method rests on that record.

**Unresolved and ambiguous are distinct, and neither is a guess.** `AMBIGUOUS` means a resolver
returned several candidates and choosing one would be inventing a fact. `UNRESOLVED` means nobody
has looked or nobody could answer. Collapsing them into a null would lose the difference between
"we do not know" and "the world does not agree".
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AssetClass(StrEnum):
    """Broad class, because resolver coverage and identifier schemes differ by it."""

    EQUITY = "EQUITY"
    ETF = "ETF"
    CRYPTO = "CRYPTO"
    #: Recorded rather than guessed. A resolver that cannot classify is not a reason to pick one.
    UNKNOWN = "UNKNOWN"


class ResolutionStatus(StrEnum):
    """How much is actually known about this instrument's external identity."""

    #: A resolver returned exactly one candidate and it was recorded.
    RESOLVED = "RESOLVED"
    #: A resolver returned several candidates. Choosing one would be inventing a fact, so the
    #: candidates are recorded and the instrument stays usable under its internal id.
    AMBIGUOUS = "AMBIGUOUS"
    #: Nobody has looked, or nobody could answer. Distinct from ambiguous: one is an absence of
    #: evidence, the other is conflicting evidence.
    UNRESOLVED = "UNRESOLVED"


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def mint_instrument_id(*, asset_class: AssetClass, first_symbol: str, first_seen: date) -> str:
    """Assign an internal id once, from the facts true when the instrument was first seen.

    Derived rather than random so it is reproducible from a bootstrap, but derived only from
    *founding* facts -- never from attributes that legitimately change. A symbol change must not
    change the identity, which is the entire purpose of having one.
    """
    payload = f"{asset_class.value}|{first_symbol.upper()}|{first_seen.isoformat()}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"instrument-{digest[:16]}"


class SymbolMapping(FrozenModel):
    """One provider's name for an instrument, over the window it was valid.

    `valid_to` is `None` while current. A closed window is kept rather than deleted, because an
    event recorded under the old symbol must remain interpretable.
    """

    provider: Text
    symbol: Text
    valid_from: date
    valid_to: date | None = None

    @model_validator(mode="after")
    def validate_mapping(self) -> Self:
        if self.valid_to is not None and self.valid_to < self.valid_from:
            raise ValueError("a mapping cannot stop being valid before it started")
        if self.symbol != self.symbol.upper():
            raise ValueError(
                f"{self.symbol!r} is not upper-case; two casings of one ticker would be two "
                "mappings and would silently split an instrument's history"
            )
        return self

    def covers(self, moment: date) -> bool:
        return self.valid_from <= moment and (self.valid_to is None or moment <= self.valid_to)


class InstrumentIdentity(FrozenModel):
    """What Elrond knows about one economic instrument, and what it does not."""

    instrument_id: Text
    asset_class: AssetClass
    #: The symbol this instrument is currently traded under, for display. Never a key.
    current_symbol: Text
    status: ResolutionStatus
    #: External identifiers, each optional and each attributable to the source that supplied it.
    figi: str | None = None
    composite_figi: str | None = None
    share_class_figi: str | None = None
    #: Kept separately attributable to SEC data, per #83: a CIK asserted by a market-data vendor
    #: and one read from EDGAR are different claims and should not share a field silently.
    sec_cik: str | None = None
    sec_cik_source: str | None = None
    exchange_mic: str | None = None
    mappings: tuple[SymbolMapping, ...] = ()
    #: Candidates a resolver could not disambiguate. Populated only when `AMBIGUOUS`.
    ambiguous_candidates: tuple[str, ...] = ()
    #: Which resolver answered, when, and at what version. A mapping with no provenance is an
    #: assertion, and this project does not store those.
    resolver: str | None = None
    resolver_version: str | None = None
    resolved_at: datetime | None = None
    source_refs: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.status is ResolutionStatus.RESOLVED and self.resolver is None:
            raise ValueError(
                "a RESOLVED identity must name the resolver that answered; without provenance "
                "it is an assertion rather than a resolution"
            )
        if self.status is ResolutionStatus.AMBIGUOUS and len(self.ambiguous_candidates) < 2:
            raise ValueError(
                "AMBIGUOUS means a resolver returned several candidates, so fewer than two is "
                "not ambiguity -- it is an unresolved or resolved identity mislabelled"
            )
        if self.status is not ResolutionStatus.AMBIGUOUS and self.ambiguous_candidates:
            raise ValueError("only an AMBIGUOUS identity may carry unresolved candidates")
        if self.sec_cik is not None and self.sec_cik_source is None:
            raise ValueError(
                "a CIK must say where it came from; one asserted by a market-data vendor and one "
                "read from EDGAR are different claims"
            )
        providers = [(item.provider, item.symbol, item.valid_from) for item in self.mappings]
        if len(providers) != len(set(providers)):
            raise ValueError("a provider cannot map the same symbol from the same date twice")
        for provider in {item.provider for item in self.mappings}:
            open_windows = [
                item
                for item in self.mappings
                if item.provider == provider and item.valid_to is None
            ]
            if len(open_windows) > 1:
                raise ValueError(
                    f"{provider} has {len(open_windows)} open symbol windows; an instrument has "
                    "one current symbol per provider or the mapping is not a function"
                )
        return self

    def symbol_at(self, provider: str, moment: date) -> str | None:
        """What this provider called the instrument on that date, or `None` if it did not.

        The whole point of the layer: an event from 2019 stays interpretable after a 2024 ticker
        change, because the question is always asked with a date.
        """
        for mapping in self.mappings:
            if mapping.provider == provider and mapping.covers(moment):
                return mapping.symbol
        return None

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    def as_row(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "asset_class": self.asset_class.value,
            "current_symbol": self.current_symbol,
            "status": self.status.value,
            "figi": self.figi,
            "composite_figi": self.composite_figi,
            "share_class_figi": self.share_class_figi,
            "sec_cik": self.sec_cik,
            "sec_cik_source": self.sec_cik_source,
            "exchange_mic": self.exchange_mic,
            "ambiguous_candidates": ",".join(self.ambiguous_candidates),
            "resolver": self.resolver,
            "resolver_version": self.resolver_version,
            "resolved_at": None if self.resolved_at is None else self.resolved_at.isoformat(),
            "source_refs": ",".join(self.source_refs),
        }

    def mapping_rows(self) -> list[dict[str, object]]:
        return [
            {
                "instrument_id": self.instrument_id,
                "provider": mapping.provider,
                "symbol": mapping.symbol,
                "valid_from": mapping.valid_from.isoformat(),
                "valid_to": None if mapping.valid_to is None else mapping.valid_to.isoformat(),
            }
            for mapping in self.mappings
        ]


def unresolved_instrument(
    *, symbol: str, asset_class: AssetClass, first_seen: date, provider: str
) -> InstrumentIdentity:
    """Record an instrument nobody has resolved yet, rather than refusing to record it.

    This is the case that matters. A resolver outage, a rate limit or an asset class no vendor
    covers must not stop a decision being attributed to an instrument -- so the internal identity
    exists first and external evidence attaches to it later.
    """
    return InstrumentIdentity(
        instrument_id=mint_instrument_id(
            asset_class=asset_class, first_symbol=symbol, first_seen=first_seen
        ),
        asset_class=asset_class,
        current_symbol=symbol.upper(),
        status=ResolutionStatus.UNRESOLVED,
        mappings=(
            SymbolMapping(provider=provider, symbol=symbol.upper(), valid_from=first_seen),
        ),
    )


def rename(
    identity: InstrumentIdentity, *, provider: str, new_symbol: str, effective: date
) -> InstrumentIdentity:
    """Record a ticker change by closing the old window and opening a new one.

    History is preserved rather than rewritten. An event recorded under the old symbol stays
    interpretable, which is the property that makes the identity worth having.
    """
    updated: list[SymbolMapping] = []
    for mapping in identity.mappings:
        if mapping.provider == provider and mapping.valid_to is None:
            if effective <= mapping.valid_from:
                raise ValueError(
                    "a rename cannot take effect at or before the window it closes; the two "
                    "windows would overlap and `symbol_at` would have two answers"
                )
            updated.append(
                mapping.model_copy(update={"valid_to": _previous_day(effective)})
            )
        else:
            updated.append(mapping)
    updated.append(
        SymbolMapping(provider=provider, symbol=new_symbol.upper(), valid_from=effective)
    )
    return identity.model_copy(
        update={"mappings": tuple(updated), "current_symbol": new_symbol.upper()}
    )


def _previous_day(moment: date) -> date:
    from datetime import timedelta

    return moment - timedelta(days=1)


def resolve_symbol(
    identities: Sequence[InstrumentIdentity], *, provider: str, symbol: str, moment: date
) -> InstrumentIdentity | None:
    """Which instrument this provider meant by this symbol on that date.

    Returns `None` rather than guessing when no mapping covers the date. A recycled ticker is
    exactly the case a nearest-match would get wrong, and silently.
    """
    wanted = symbol.upper()
    for identity in identities:
        if identity.symbol_at(provider, moment) == wanted:
            return identity
    return None


__all__ = [
    "AssetClass",
    "InstrumentIdentity",
    "ResolutionStatus",
    "SymbolMapping",
    "mint_instrument_id",
    "rename",
    "resolve_symbol",
    "unresolved_instrument",
]
