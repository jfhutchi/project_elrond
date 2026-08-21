"""Macro data, and the revision problem that makes a naive study look prescient."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from quantbot.market_data.fred import (
    ALFRED_URL,
    FRED_CSV_URL,
    KNOWN_SERIES,
    FredError,
    FredProvider,
    RevisionUnavailable,
    content_hash,
    observations_available_at,
    parse_series,
    period_end,
)
from quantbot.market_data.instruments import AssetClass
from quantbot.market_data.pointintime import Capability, UnsupportedCapability, Vintage, require

RETRIEVED = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

PAYROLLS_CSV = """DATE,PAYEMS
2026-03-01,158000
2026-04-01,158250
2026-05-01,.
2026-06-01,158600
"""


def vintage(**overrides: object) -> Vintage:
    fields: dict[str, object] = {
        "provider": "fred",
        "dataset": "PAYEMS",
        "vintage": "fred:2026-08-19",
        "retrieved_at": RETRIEVED,
    }
    fields.update(overrides)
    return Vintage(**fields)


class FakeFred:
    """Records what was asked for, so the endpoint choice is observable."""

    def __init__(self, body: str = PAYROLLS_CSV) -> None:
        self.body = body
        self.calls: list[tuple[str, dict[str, str]]] = []

    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> str:
        self.calls.append((url, params))
        return self.body


def test_an_observation_is_not_available_on_the_date_it_describes() -> None:
    """March payrolls are published in April. Using them on 31 March is using a number that
    did not exist, and the effect is invisible in results."""
    series = KNOWN_SERIES["PAYEMS"]
    observations = parse_series(PAYROLLS_CSV, series, vintage=vintage())

    march = observations[0]
    # FRED stamps a monthly series with its period *start*, so the lag runs from the month end.
    assert march.availability.observed_at.date() == date(2026, 3, 1)
    assert march.availability.available_at.date() == date(2026, 4, 5)

    # Not knowable at any point during the month it describes. The first version of this module
    # made it available on 6 March, five days before March had finished.
    assert not march.availability.known_by(datetime(2026, 3, 31, 23, 59, tzinfo=UTC))

    # The release hour matters: a strategy acting on the previous close cannot use a value
    # published at 8:30am ET, and rounding to midnight would grant half a day of hindsight.
    assert not march.availability.known_by(datetime(2026, 4, 5, 6, 0, tzinfo=UTC))
    assert march.availability.known_by(datetime(2026, 4, 5, 13, 0, tzinfo=UTC))


def test_a_missing_period_is_skipped_rather_than_filled() -> None:
    observations = parse_series(PAYROLLS_CSV, KNOWN_SERIES["PAYEMS"], vintage=vintage())
    assert [item.value for item in observations] == [
        Decimal("158000"),
        Decimal("158250"),
        Decimal("158600"),
    ]
    assert all(item.availability.observed_at.date() != date(2026, 5, 1) for item in observations)


def test_an_actual_release_date_overrides_the_assumed_lag() -> None:
    """The lag is an assumption; a real release date is a fact, and facts win."""
    observations = parse_series(
        PAYROLLS_CSV,
        KNOWN_SERIES["PAYEMS"],
        vintage=vintage(),
        release_dates={date(2026, 3, 1): date(2026, 4, 3)},
    )
    assert observations[0].availability.available_at.date() == date(2026, 4, 3)
    # The rows without a supplied release date still use period end plus the declared lag.
    assert observations[1].availability.available_at.date() == date(2026, 5, 5)


def test_the_release_instant_follows_eastern_daylight_saving_not_a_fixed_utc_hour() -> None:
    """08:30 ET is 13:30 UTC in winter and 12:30 UTC in summer. A constant is wrong twice.

    This module stored a fixed 13:00 UTC until GPT-5.6 Sol found it reviewing #17. The summer
    half of that error was merely conservative -- thirty minutes late. The winter half published
    every release thirty minutes *early*, which is look-ahead, in the module written to prevent
    look-ahead, and of the invisible kind: it does not break a backtest, it makes the strategy
    look prescient.

    Asserting the exact instant rather than the date, because the date was already right.
    """
    body = """
DATE,PAYEMS
2026-01-01,158000
2026-06-01,158600
"""
    winter, summer = parse_series(
        body,
        KNOWN_SERIES["PAYEMS"],
        vintage=vintage(),
        release_dates={date(2026, 1, 1): date(2026, 2, 6), date(2026, 6, 1): date(2026, 7, 2)},
    )

    assert winter.availability.available_at == datetime(2026, 2, 6, 13, 30, tzinfo=UTC)
    assert summer.availability.available_at == datetime(2026, 7, 2, 12, 30, tzinfo=UTC)

    # The defect itself: at the old constant the winter release was already knowable.
    assert not winter.availability.known_by(datetime(2026, 2, 6, 13, 0, tzinfo=UTC))
    assert winter.availability.known_by(datetime(2026, 2, 6, 13, 30, tzinfo=UTC))


def test_a_confirmatory_run_on_a_revised_series_must_use_the_archival_endpoint() -> None:
    """`fredgraph.csv` serves today's revised values, which is what a naive study uses."""
    transport = FakeFred()
    provider = FredProvider(transport)

    # As of a date after every value in the body was published, so this test is about which
    # endpoint gets called rather than about the leak check.
    provider.series_for_confirmatory_use(
        "PAYEMS", as_of=date(2026, 8, 1), retrieved_at=RETRIEVED
    )
    url, params = transport.calls[-1]
    assert url == ALFRED_URL
    assert params["realtime_start"] == "2026-08-01"
    assert params["realtime_end"] == "2026-08-01"

    # A market-observed rate is not revised, so today's series is already point-in-time and
    # the cheaper endpoint is correct.
    transport = FakeFred("DATE,T10Y2Y\n2026-06-01,0.35\n")
    provider = FredProvider(transport)
    provider.series_for_confirmatory_use(
        "T10Y2Y", as_of=date(2026, 8, 1), retrieved_at=RETRIEVED
    )
    assert transport.calls[-1][0] == FRED_CSV_URL


def test_the_publication_lag_runs_from_the_period_end_not_the_period_start() -> None:
    """The look-ahead bug this module shipped with, pinned so it cannot come back.

    A monthly series stamped 2026-03-01 covers all of March. Adding the lag to the stamp puts
    the value in the market before the month it describes has finished.
    """
    assert period_end(date(2026, 3, 1), "M") == date(2026, 3, 31)
    assert period_end(date(2026, 12, 1), "M") == date(2026, 12, 31)
    assert period_end(date(2026, 1, 1), "Q") == date(2026, 3, 31)
    assert period_end(date(2026, 10, 1), "Q") == date(2026, 12, 31)
    assert period_end(date(2026, 6, 15), "D") == date(2026, 6, 15)
    assert period_end(date(2026, 6, 15), "W") == date(2026, 6, 21)
    assert period_end(date(2026, 1, 1), "A") == date(2026, 12, 31)

    with pytest.raises(FredError, match="unknown series frequency"):
        period_end(date(2026, 1, 1), "fortnightly")

    # Every observation is published strictly after the period it covers.
    for observation in parse_series(
        PAYROLLS_CSV, KNOWN_SERIES["PAYEMS"], vintage=vintage()
    ):
        covered = period_end(observation.availability.observed_at.date(), "M")
        assert observation.availability.available_at.date() > covered


def test_a_realtime_pull_that_leaks_later_values_is_refused() -> None:
    """If the endpoint ignored the parameters the data is not point-in-time, and saying so
    is better than a backtest that quietly looks prescient."""
    transport = FakeFred()
    provider = FredProvider(transport)

    with pytest.raises(RevisionUnavailable, match="not point-in-time"):
        # Asking as of March, but the body carries values published through June.
        provider.realtime_series("PAYEMS", as_of=date(2026, 3, 15), retrieved_at=RETRIEVED)

    # Asked as of a date after everything was published, the same body is fine.
    assert provider.realtime_series(
        "PAYEMS", as_of=date(2026, 12, 31), retrieved_at=RETRIEVED
    )


def test_a_series_nobody_declared_is_refused_rather_than_guessed() -> None:
    """A publication lag guessed at the call site is a look-ahead bug waiting to happen."""
    with pytest.raises(FredError, match="rather than guessing one"):
        FredProvider(FakeFred()).latest_series("GDPNOW", retrieved_at=RETRIEVED)


def test_the_provider_serves_macro_and_says_so_about_everything_else() -> None:
    provider = FredProvider(FakeFred())
    require(provider, Capability.MACRO_SERIES)
    for capability in (Capability.BARS, Capability.DELISTINGS, Capability.OPTIONS_DERIVED):
        with pytest.raises(UnsupportedCapability):
            require(provider, capability)


def test_a_revision_is_visible_as_a_different_vintage_and_a_different_hash() -> None:
    """Silent revision is the failure mode that makes an old conclusion unreproducible."""
    provider = FredProvider(FakeFred())
    first = provider.vintage("PAYEMS", retrieved_at=RETRIEVED)
    archival = provider.vintage("PAYEMS", retrieved_at=RETRIEVED, realtime=True)
    assert first.vintage != archival.vintage
    assert first.vintage.startswith("fred:")
    assert archival.vintage.startswith("alfred:")

    original = parse_series(PAYROLLS_CSV, KNOWN_SERIES["PAYEMS"], vintage=vintage())
    revised = parse_series(
        PAYROLLS_CSV.replace("158250", "158100"), KNOWN_SERIES["PAYEMS"], vintage=vintage()
    )
    assert content_hash(original) != content_hash(revised)


def test_a_macro_series_is_an_instrument_with_its_own_asset_class() -> None:
    """New statistical capacity only counts if the system can tell it is a different thing."""
    instrument = KNOWN_SERIES["T10Y2Y"].as_instrument()
    assert instrument.asset_class is AssetClass.MACRO
    assert instrument.instrument_id == "fred-t10y2y"
    assert not instrument.carries_currency_risk

    universe = FredProvider(FakeFred()).instruments(as_of=RETRIEVED)
    assert len(universe) == len(KNOWN_SERIES)
    assert {item.asset_class for item in universe} == {AssetClass.MACRO}


def test_how_many_observations_a_strategy_could_actually_have_used() -> None:
    observations = parse_series(PAYROLLS_CSV, KNOWN_SERIES["PAYEMS"], vintage=vintage())
    assert observations_available_at(observations, datetime(2026, 3, 5, tzinfo=UTC)) == 0
    assert observations_available_at(observations, datetime(2026, 4, 10, tzinfo=UTC)) == 1
    assert observations_available_at(observations, datetime(2027, 1, 1, tzinfo=UTC)) == 3


def test_an_unreadable_response_is_an_error_not_an_empty_series() -> None:
    for body in ("", "<html>503</html>", "DATE,PAYEMS\n", "WRONG,HEADER\n2026-01-01,1\n"):
        with pytest.raises(FredError):
            parse_series(body, KNOWN_SERIES["PAYEMS"], vintage=vintage())
    with pytest.raises(FredError, match="unreadable row"):
        parse_series(
            "DATE,PAYEMS\n2026-13-99,abc\n", KNOWN_SERIES["PAYEMS"], vintage=vintage()
        )
