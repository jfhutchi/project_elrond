"""Equities behind the common retrieval interface, with survivorship made measurable (#17).

`FredProvider` and `SecFilingProvider` answer `observations()`; equities -- the asset class this
project actually trades -- did not, so the one source whose survivorship problem is worst was the
one a study could not reach through the common surface.

**The membership answer is the point.** `instruments()` returns everything tradable on the date,
*including* names that later stopped trading. A provider that returned only survivors would give
every cross-sectional study a flattering answer with nothing in the data to show it. That failure
is invisible: the numbers look fine, they are simply about a universe selected after the fact.

`InstrumentUniverse` already computes `members`, `survivors` and `survivorship_bias`. This binds
them to the retrieval interface, so a study measures the difference rather than assuming it.

**A daily bar becomes knowable at its session close**, not at midnight and not on its date. A
strategy acting on the close of the day it trades is using a number that did not exist when the
decision was made, and that is the most common look-ahead in daily backtesting. `session_close`
is injected rather than assumed, because it is a calendar fact this module has no business
guessing for a venue it was not told about.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime

from quantbot.domain import Bar
from quantbot.market_data.instruments import (
    CorporateAction,
    Instrument,
    InstrumentUniverse,
)
from quantbot.market_data.pointintime import (
    Availability,
    Capability,
    Observation,
    UnsupportedCapability,
    Vintage,
    require,
)

#: What this provider serves. `DELISTINGS` is here because `instruments()` returns names that
#: stopped trading; a provider that omitted it while doing so would be under-declaring the one
#: capability that makes its answers trustworthy.
EQUITY_CAPABILITIES = frozenset(
    {Capability.BARS, Capability.DELISTINGS, Capability.CORPORATE_ACTIONS}
)


class EquityProvider:
    """Stored bars and a point-in-time universe, behind `ResearchDataProvider`.

    Takes the bars and the universe rather than a database session. The provider's job is to
    answer point-in-time questions about data it was given; letting it fetch would make its
    answers depend on when it ran, which is the property the whole module exists to remove.
    """

    name = "equities"

    def __init__(
        self,
        bars: Mapping[str, Sequence[Bar]],
        universe: InstrumentUniverse,
        *,
        session_close: Callable[[datetime], datetime],
        actions: Sequence[CorporateAction] = (),
    ) -> None:
        self._bars = {symbol: tuple(series) for symbol, series in bars.items()}
        self._universe = universe
        self._session_close = session_close
        self._actions = tuple(actions)

    def capabilities(self) -> frozenset[Capability]:
        return EQUITY_CAPABILITIES

    def vintage(self, dataset: str, *, retrieved_at: datetime) -> Vintage:
        return Vintage(
            provider=self.name,
            dataset=dataset,
            vintage=f"equities:{retrieved_at.date().isoformat()}",
            retrieved_at=retrieved_at,
        )

    def instruments(self, *, as_of: datetime) -> tuple[Instrument, ...]:
        """Everything tradable on that date, including names that later stopped trading.

        This is the survivorship-honest answer and the reason `DELISTINGS` is declared. Returning
        only survivors would hand every cross-sectional study a flattering universe selected
        after the fact, and nothing in the numbers would show it.
        """
        return self._universe.members(as_of.date())

    def survivorship_bias(self, *, as_of: datetime) -> float:
        """Share of the `as_of` universe a live-only listing would have dropped.

        Exposed so a study can *measure* the exposure rather than assert it is small. If a
        cross-sectional result moves materially with this number, the result is about
        survivorship rather than about the hypothesis.
        """
        return self._universe.survivorship_bias(as_of.date())

    def corporate_actions(
        self, instrument_id: str, *, knowable_by: datetime | None = None
    ) -> tuple[CorporateAction, ...]:
        """Actions for one instrument, optionally filtered to what was announced by a date.

        Keyed on `instrument_id` rather than symbol, deliberately. A symbol is reused by later
        companies and changes under the same company; an action looked up by ticker eventually
        attaches to the wrong series, and the resulting adjustment is silently wrong rather
        than absent.

        `knowable_by` filters on `announced_on`, not `effective_on`. A backtest that adjusts a
        price on the effective date is using an announcement it had not seen, which shifts the
        adjustment earlier than the market could have applied it.
        """
        matching = tuple(
            action for action in self._actions if action.instrument_id == instrument_id
        )
        if knowable_by is None:
            return matching
        cutoff = knowable_by.date()
        return tuple(action for action in matching if action.announced_on <= cutoff)

    def observations(
        self, dataset: str, *, capability: Capability, retrieved_at: datetime
    ) -> tuple[Observation[object], ...]:
        """Bars for one symbol, each knowable at its session close.

        A bar dated today is not knowable during today. Availability is the session close, so a
        decision made intraday cannot use it -- the most common look-ahead in daily backtesting,
        and one that produces a strategy which looks prescient rather than one that looks broken.
        """
        require(self, capability)
        series = self._bars.get(dataset)
        if series is None:
            # Not an empty tuple. A symbol nobody loaded and a symbol with no bars are different
            # facts, and returning () for the first would report an absence of trading.
            raise UnsupportedCapability(self.name, capability)
        marker = self.vintage(dataset, retrieved_at=retrieved_at)
        return tuple(
            Observation(
                label=f"{dataset}:{bar.timestamp.date().isoformat()}",
                availability=Availability(
                    observed_at=bar.timestamp,
                    available_at=self._session_close(bar.timestamp),
                ),
                vintage=marker,
                value=bar,
            )
            for bar in series
        )


__all__ = ["EQUITY_CAPABILITIES", "EquityProvider"]
