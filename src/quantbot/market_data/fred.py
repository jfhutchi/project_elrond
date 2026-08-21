"""FRED macro series: new statistical capacity, and the vintage problem made concrete (#17).

This exists because the binding constraint on this project is data, not process. SIP history
begins 2016-01-04 and cycles 2-10 searched all of it, so `window_consumption` reports
`EXHAUSTED` and every gate correctly refuses to let anything new become confirmatory evidence
against it. **A new asset class is not a new idea. It is new statistical budget**, and it is the
only thing that can rescue confirmatory research here.

FRED is the cheapest such source: free, no credentials, and decades of history where equities
gave us ten years. It is also the source that makes the point-in-time machinery earn its keep,
because macro series are **revised after publication**.

That revision is the whole reason this module is careful:

* A series has a *release date* separate from its *observation date*. Non-farm payrolls for
  March are published in early April and revised in May and June. A backtest that uses the March
  value on 31 March is using a number that did not exist, and the effect is invisible in results
  -- it simply makes the strategy look prescient.
* `ALFRED` (the archival service) serves the value **as it stood on a given date**, which is what
  a point-in-time backtest actually needs. `fredgraph.csv` serves today's revised series, which
  is what almost every naive study uses by mistake.

So `parse_series` refuses to produce observations without a release date, and
`realtime_series` is the only constructor that yields a `Vintage` a confirmatory run may use.
Asking for a point-in-time view of a source that cannot provide one raises rather than
approximating.

Transport is injected, following `market_data.transports` and `research.models`, so the parsing
and the availability arithmetic are exercised in tests without a network.
"""

from __future__ import annotations

import csv
import hashlib
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from quantbot.market_data.base import MarketDataError
from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.market_data.instruments import AssetClass, Instrument
from quantbot.market_data.pointintime import (
    Availability,
    Capability,
    Observation,
    Vintage,
    require,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

#: Already on the sandbox allowlist (`sandbox/policy.py`), so a research experiment may reach it.
FRED_HOST = "files.stlouisfed.org"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
#: The archival endpoint. The only one whose values are point-in-time.
ALFRED_URL = "https://alfred.stlouisfed.org/graph/fredgraph.csv"

#: FRED publishes in US Eastern; 08:30 ET is the standard release time for the major series.
#: Recorded as an assumption rather than hidden, because it sets the availability timestamp.
#:
#: Held in Eastern and converted per date rather than stored as a fixed UTC hour. 08:30 ET is
#: 13:30 UTC under standard time and 12:30 UTC under daylight time, so a constant 13:00 UTC --
#: which this module used until GPT-5.6 Sol found it on #17 -- published every winter release
#: thirty minutes early. That is look-ahead, in the one module written to prevent it, and it is
#: the invisible kind: it does not break a backtest, it just makes the strategy look prescient.
#: (The summer half of the same error ran thirty minutes late, which is merely conservative.)
RELEASE_TIME_ET = time(hour=8, minute=30)


class FredError(MarketDataError):
    """Raised when a FRED response cannot be read as a series."""


class RevisionUnavailable(MarketDataError):
    """Raised when a point-in-time view is requested from a source that cannot provide one.

    An error rather than a silent fall-back to today's revised values. A backtest quietly fed
    revised macro data looks prescient, and nothing in the result says why.
    """


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MacroSeries(FrozenModel):
    """Identity of a FRED series, as an instrument rather than a bare string."""

    series_id: Text
    title: Text
    #: D, W, M, Q, A. Sets how many observations a window actually contains.
    frequency: Text
    units: Text
    #: Typical lag from the period ending to first publication. Payrolls is about 5 days;
    #: quarterly GDP is nearer 30. Declared per series because it drives availability.
    publication_lag_days: int = Field(ge=0)
    #: Whether this series is revised after first publication. True for most real activity
    #: measures, false for market-observed rates.
    revised: bool = True

    def as_instrument(self) -> Instrument:
        return Instrument(
            instrument_id=f"fred-{self.series_id.lower()}",
            symbol=self.series_id,
            venue="FRED",
            asset_class=AssetClass.MACRO,
            quote_currency="USD",
            exposure_currency="USD",
            exposure_market=f"US macro: {self.title}",
            calendar="XNYS",
            listed_on=date(1900, 1, 1),
        )


#: Series worth having, with their real publication lags. Deliberately short: each one is a
#: separate decision about what is worth searching, not a bulk download.
KNOWN_SERIES: dict[str, MacroSeries] = {
    "T10Y2Y": MacroSeries(
        series_id="T10Y2Y",
        title="10-year minus 2-year Treasury constant maturity",
        frequency="D",
        units="percent",
        # Market-observed and published same day, so effectively no lag and no revision.
        publication_lag_days=1,
        revised=False,
    ),
    "PAYEMS": MacroSeries(
        series_id="PAYEMS",
        title="All employees, total nonfarm",
        frequency="M",
        units="thousands of persons",
        publication_lag_days=5,
        revised=True,
    ),
    "CPIAUCSL": MacroSeries(
        series_id="CPIAUCSL",
        title="Consumer price index for all urban consumers",
        frequency="M",
        units="index 1982-1984=100",
        publication_lag_days=12,
        revised=True,
    ),
    "UNRATE": MacroSeries(
        series_id="UNRATE",
        title="Unemployment rate",
        frequency="M",
        units="percent",
        publication_lag_days=5,
        revised=True,
    ),
}


class FredTransport(Protocol):
    """One call: fetch a CSV body. Injected so parsing is tested without a network."""

    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> str: ...


def period_end(observed: date, frequency: str) -> date:
    """The last day the observation actually covers.

    FRED dates a periodic series by its **period start**: March payrolls are stamped
    `2026-03-01`. The publication lag runs from the end of the period, not the start, so adding
    it to the stamped date puts March data on the 6th of March -- five days before the month it
    describes has finished. That is look-ahead of the most ordinary kind, and it was in the
    first version of this module.
    """
    code = frequency.strip().upper()[:1]
    if code == "D":
        return observed
    if code == "W":
        return observed + timedelta(days=6)
    if code == "M":
        return _month_end(observed.year, observed.month)
    if code == "Q":
        return _month_end(observed.year, observed.month + 2)
    if code == "A":
        return date(observed.year, 12, 31)
    raise FredError(f"unknown series frequency {frequency!r}")


def _month_end(year: int, month: int) -> date:
    year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
    return date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)


def release_instant(released: date) -> datetime:
    """The UTC instant a release published on `released` became knowable.

    Converted through `America/New_York` for that specific date, so the answer follows the
    daylight-saving rule in force rather than an average of it.
    """
    return datetime.combine(released, RELEASE_TIME_ET, tzinfo=XNYS_TIMEZONE).astimezone(UTC)


def _release_datetime(observed: date, *, series: MacroSeries) -> datetime:
    """When a value for `observed` could first be known.

    The end of the period it covers, plus the publication lag, at the release time. The time of
    day matters: a series released at 08:30 ET is not usable by a strategy acting on the previous
    close, and rounding to midnight would quietly grant half a day of hindsight.
    """
    released = period_end(observed, series.frequency) + timedelta(
        days=series.publication_lag_days
    )
    return release_instant(released)


def parse_series(
    body: str,
    series: MacroSeries,
    *,
    vintage: Vintage,
    release_dates: dict[date, date] | None = None,
) -> tuple[Observation[Decimal], ...]:
    """Parse a FRED CSV into observations that know when they became knowable.

    `release_dates` maps an observation date to the date its value was actually published. When
    ALFRED supplies it, availability is exact. When it is absent the declared publication lag is
    used, which is an assumption and is recorded as one on the vintage rather than treated as
    fact.
    """
    rows = list(csv.reader(body.strip().splitlines()))
    if len(rows) < 2:
        raise FredError(f"{series.series_id}: response contains no observations")
    header = [column.strip().upper() for column in rows[0]]
    if len(header) < 2 or header[0] not in {"DATE", "OBSERVATION_DATE"}:
        raise FredError(f"{series.series_id}: unexpected header {rows[0]}")

    observations: list[Observation[Decimal]] = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        raw_date, raw_value = row[0].strip(), row[1].strip()
        # FRED writes "." for a missing period. Skipping is correct; substituting is not.
        if raw_value in {"", "."}:
            continue
        try:
            observed = date.fromisoformat(raw_date)
            value = Decimal(raw_value)
        except (ValueError, InvalidOperation) as error:
            raise FredError(f"{series.series_id}: unreadable row {row}") from error

        released = (release_dates or {}).get(observed)
        available_at = (
            release_instant(released)
            if released is not None
            else _release_datetime(observed, series=series)
        )
        observations.append(
            Observation(
                label=f"fred:{series.series_id}",
                availability=Availability(
                    observed_at=datetime.combine(observed, time(), tzinfo=UTC),
                    available_at=available_at,
                ),
                vintage=vintage,
                value=value,
            )
        )
    if not observations:
        raise FredError(f"{series.series_id}: every observation was missing")
    return tuple(observations)


def content_hash(observations: Iterable[Observation[Decimal]]) -> str:
    """Content address of a pulled series, so a later revision is detectable."""
    digest = hashlib.sha256()
    for item in sorted(observations, key=lambda entry: entry.availability.observed_at):
        digest.update(
            f"{item.availability.observed_at.date().isoformat()}={item.value}".encode()
        )
    return digest.hexdigest()


class FredProvider:
    """A research data provider over FRED, honest about which endpoint it used."""

    name = "fred"

    def __init__(self, transport: FredTransport, *, timeout_seconds: float = 30.0) -> None:
        self._transport = transport
        self._timeout = timeout_seconds

    def capabilities(self) -> frozenset[Capability]:
        """Macro series only. Asking for bars or delistings raises rather than returning []."""
        return frozenset({Capability.MACRO_SERIES})

    def vintage(self, dataset: str, *, retrieved_at: datetime, realtime: bool = False) -> Vintage:
        return Vintage(
            provider=self.name,
            dataset=dataset,
            # A same-day re-pull of a revised series is a different vintage, so the marker
            # carries the endpoint as well as the date.
            vintage=f"{'alfred' if realtime else 'fred'}:{retrieved_at.date().isoformat()}",
            retrieved_at=retrieved_at,
        )

    def observations(
        self, dataset: str, *, capability: Capability, retrieved_at: datetime
    ) -> tuple[Observation[object], ...]:
        """The common retrieval surface (#17), serving today's series.

        Deliberately the `latest_series` path and not the ALFRED one. A uniform interface cannot
        silently decide which vintage a caller gets: `realtime_series` needs an `as_of` date that
        only the caller knows, and defaulting it to today would hand a confirmatory run
        today-revised numbers under a method whose name promises nothing about vintage. A study
        that needs point-in-time values on a revised series asks for them explicitly, and
        `Vintage.realtime` records which it got.
        """
        require(self, capability)
        return self.latest_series(dataset, retrieved_at=retrieved_at)

    def latest_series(
        self, series_id: str, *, retrieved_at: datetime
    ) -> tuple[Observation[Decimal], ...]:
        """Today's series, with availability from the declared lag.

        Suitable for exploration. **Not** point-in-time for a revised series: the values are as
        revised today, so a confirmatory run on one must use `realtime_series` instead.
        """
        series = _known(series_id)
        body = self._transport.fetch(
            FRED_CSV_URL, {"id": series_id}, timeout_seconds=self._timeout
        )
        return parse_series(
            body, series, vintage=self.vintage(series_id, retrieved_at=retrieved_at)
        )

    def realtime_series(
        self, series_id: str, *, as_of: date, retrieved_at: datetime
    ) -> tuple[Observation[Decimal], ...]:
        """The series **as it stood on `as_of`**, which is what a backtest actually needs.

        Uses ALFRED's real-time parameters. This is the only path whose values a confirmatory
        experiment on a revised series may use.
        """
        series = _known(series_id)
        body = self._transport.fetch(
            ALFRED_URL,
            {
                "id": series_id,
                "realtime_start": as_of.isoformat(),
                "realtime_end": as_of.isoformat(),
            },
            timeout_seconds=self._timeout,
        )
        observations = parse_series(
            body,
            series,
            vintage=self.vintage(series_id, retrieved_at=retrieved_at, realtime=True),
        )
        # A real-time pull cannot contain a value published after the date requested. If it
        # does, the endpoint did not honour the parameters and the data is not point-in-time.
        cutoff = datetime.combine(as_of, time(hour=23, minute=59), tzinfo=UTC)
        leaked = [item for item in observations if item.availability.available_at > cutoff]
        if leaked:
            raise RevisionUnavailable(
                f"{series_id}: the real-time pull for {as_of} returned {len(leaked)} values "
                "published after that date; the response is not point-in-time"
            )
        return observations

    def series_for_confirmatory_use(
        self, series_id: str, *, as_of: date, retrieved_at: datetime
    ) -> tuple[Observation[Decimal], ...]:
        """Refuse the revised-series shortcut that makes a backtest look prescient."""
        series = _known(series_id)
        if series.revised:
            return self.realtime_series(
                series_id, as_of=as_of, retrieved_at=retrieved_at
            )
        # A market-observed rate is not revised, so today's series is point-in-time already.
        return self.latest_series(series_id, retrieved_at=retrieved_at)

    def instruments(self, *, as_of: datetime) -> tuple[Instrument, ...]:
        _ = as_of
        return tuple(series.as_instrument() for series in KNOWN_SERIES.values())


def _known(series_id: str) -> MacroSeries:
    series = KNOWN_SERIES.get(series_id.upper())
    if series is None:
        raise FredError(
            f"{series_id} is not a declared series; add it to KNOWN_SERIES with its real "
            "publication lag rather than guessing one at the call site"
        )
    return series


def observations_available_at(
    observations: Sequence[Observation[Decimal]], as_of: datetime
) -> int:
    """How many of these a strategy could actually have used at a point in time."""
    return sum(1 for item in observations if item.availability.known_by(as_of))


__all__ = [
    "ALFRED_URL",
    "FRED_CSV_URL",
    "FRED_HOST",
    "KNOWN_SERIES",
    "RELEASE_TIME_ET",
    "FredError",
    "FredProvider",
    "FredTransport",
    "MacroSeries",
    "RevisionUnavailable",
    "content_hash",
    "period_end",
    "release_instant",
    "observations_available_at",
    "parse_series",
]
