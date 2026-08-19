"""Point-in-time and survivorship discipline, enforced rather than remembered.

Every assertion here corresponds to a way this project has already produced or narrowly avoided
a wrong answer: a value used before it was published, a universe that quietly dropped everything
that failed, a currency exposure nobody declared, and a provider returning nothing instead of
saying it cannot help.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from quantbot.market_data.instruments import (
    AssetClass,
    CorporateAction,
    CorporateActionKind,
    Instrument,
    InstrumentUniverse,
)
from quantbot.market_data.pointintime import (
    Availability,
    Capability,
    FeatureSpec,
    Observation,
    PointInTimeViolation,
    UnsupportedCapability,
    Vintage,
    knowable,
    latest_knowable,
    require,
    session_availability,
)

CLOSE = datetime(2026, 8, 18, 20, 0, tzinfo=UTC)
NEXT_OPEN = datetime(2026, 8, 19, 13, 30, tzinfo=UTC)


def vintage(**overrides: object) -> Vintage:
    fields: dict[str, object] = {
        "provider": "alpaca",
        "dataset": "sip-us-equities-daily",
        "vintage": "2026-08-18",
        "retrieved_at": CLOSE,
    }
    fields.update(overrides)
    return Vintage(**fields)


def observation(observed: datetime, available: datetime, value: str) -> Observation[str]:
    return Observation(
        label="close",
        availability=Availability(observed_at=observed, available_at=available),
        vintage=vintage(),
        value=value,
    )


def equity(
    symbol: str,
    *,
    listed: date = date(2016, 1, 4),
    delisted: date | None = None,
    exposure_currency: str = "USD",
    exposure_market: str = "US equity",
) -> Instrument:
    return Instrument(
        instrument_id=f"us-{symbol.lower()}",
        symbol=symbol,
        venue="XNAS",
        asset_class=AssetClass.EQUITY,
        exposure_currency=exposure_currency,
        exposure_market=exposure_market,
        listed_on=listed,
        delisted_on=delisted,
    )


def test_a_value_cannot_be_available_before_the_moment_it_describes() -> None:
    with pytest.raises(ValueError, match="cannot be available before"):
        Availability(observed_at=CLOSE, available_at=CLOSE - timedelta(seconds=1))


def test_using_a_value_before_it_was_published_raises() -> None:
    """The failure mode two of cycle 11-12's three errors belonged to."""
    published_late = observation(CLOSE, NEXT_OPEN, "105.00")

    with pytest.raises(PointInTimeViolation) as error:
        published_late.require_known_by(CLOSE + timedelta(minutes=1))
    assert error.value.available_at == NEXT_OPEN

    assert published_late.require_known_by(NEXT_OPEN) is published_late


def test_filtering_to_the_knowable_is_what_removes_look_ahead() -> None:
    series = [
        observation(CLOSE - timedelta(days=2), CLOSE - timedelta(days=2), "100"),
        observation(CLOSE - timedelta(days=1), CLOSE - timedelta(days=1), "101"),
        observation(CLOSE, NEXT_OPEN, "102"),
    ]
    at_close = knowable(series, CLOSE)
    assert [item.value for item in at_close] == ["100", "101"]

    # The most recent knowable value is what a decision may actually use, and it is not the
    # most recent value.
    assert latest_knowable(series, CLOSE) is not None
    assert latest_knowable(series, CLOSE).value == "101"  # type: ignore[union-attr]
    assert latest_knowable(series, NEXT_OPEN).value == "102"  # type: ignore[union-attr]
    assert latest_knowable(series, CLOSE - timedelta(days=10)) is None


def test_a_feature_is_never_available_before_its_slowest_input() -> None:
    """Where look-ahead usually enters: the feature looks recent, an input behind it was not.

    The oldest input here is published *after* the newest one arrives -- a backfilled or
    revised value, which is the case that actually discriminates. Timing the feature from its
    newest observation instead of its slowest availability gives an answer three days early,
    and that is exactly the mistake this asserts against.
    """
    lag = timedelta(hours=17, minutes=30)
    specification = FeatureSpec(
        name="sma",
        lookback_observations=3,
        publication_lag=lag,
        transformation_version="v1",
        inputs=("close",),
    )
    backfilled = Availability(
        observed_at=CLOSE - timedelta(days=2),
        # Published three days late, so it lands after the newest bar exists.
        available_at=CLOSE + timedelta(days=1),
    )
    prompt = [
        Availability(observed_at=CLOSE - timedelta(days=1), available_at=CLOSE - timedelta(days=1)),
        Availability(observed_at=CLOSE, available_at=CLOSE),
    ]
    inputs = [backfilled, *prompt]

    # The feature is gated by the slowest input, not by the newest observation.
    assert specification.available_at(inputs) == CLOSE + timedelta(days=1) + lag
    assert specification.available_at(inputs) != CLOSE + lag

    # Without the backfilled input the same window is usable at the close plus the lag, which
    # is what makes the difference above attributable to availability rather than to ordering.
    prompt_only = FeatureSpec(
        name="sma",
        lookback_observations=2,
        publication_lag=lag,
        transformation_version="v1",
        inputs=("close",),
    )
    assert prompt_only.available_at(prompt) == CLOSE + lag

    # And a shorter history than the lookback cannot produce a value at all.
    with pytest.raises(ValueError, match="needs 3 observations"):
        specification.available_at(inputs[:2])


def test_a_materialized_feature_carries_its_lineage_and_its_lag() -> None:
    specification = FeatureSpec(
        name="sma_200",
        lookback_observations=2,
        publication_lag=timedelta(hours=17, minutes=30),
        transformation_version="2026-08-18",
        inputs=("close",),
    )
    inputs = [
        observation(CLOSE - timedelta(days=1), CLOSE - timedelta(days=1), "100"),
        observation(CLOSE, CLOSE, "102"),
    ]
    feature = specification.materialize(
        Decimal("101"),
        inputs=[item.availability for item in inputs],
        vintage=vintage(),
    )

    assert feature.label == "sma_200@2026-08-18"
    assert feature.availability.observed_at == CLOSE
    assert feature.availability.available_at == CLOSE + timedelta(hours=17, minutes=30)
    assert feature.vintage.provider == "alpaca"
    # Computed at the close, it is not usable at the close.
    with pytest.raises(PointInTimeViolation):
        feature.require_known_by(CLOSE)


def test_a_universe_includes_what_stopped_trading() -> None:
    """Cycle 10 was interpretable only because delisted history was available.

    511 names including 241 that stopped trading; whole-market momentum produced more return
    and less Sharpe. A live-only universe would have shown a winner.
    """
    universe = InstrumentUniverse(
        [
            equity("AAPL"),
            equity("MSFT"),
            equity("ENRN", delisted=date(2020, 6, 30)),
            equity("SVB", delisted=date(2023, 3, 10)),
        ]
    )

    during = universe.members(date(2019, 1, 2))
    assert {instrument.symbol for instrument in during} == {"AAPL", "MSFT", "ENRN", "SVB"}

    # The same date, restricted to what survived to the end of the record, is a smaller and
    # systematically better-performing set. The difference is the bias, and it is measurable.
    assert {instrument.symbol for instrument in universe.survivors(date(2019, 1, 2))} == {
        "AAPL",
        "MSFT",
    }
    assert universe.survivorship_bias(date(2019, 1, 2)) == 0.5

    # After a delisting the name leaves membership, but it does not leave the record: the
    # universe still knows it existed and when it terminated.
    assert {instrument.symbol for instrument in universe.members(date(2024, 1, 2))} == {
        "AAPL",
        "MSFT",
    }
    assert len(universe) == 4


def test_an_instrument_not_yet_listed_is_absent_rather_than_backfilled() -> None:
    universe = InstrumentUniverse([equity("IPO", listed=date(2021, 5, 3))])
    assert universe.members(date(2021, 5, 2)) == ()
    assert len(universe.members(date(2021, 5, 3))) == 1


def test_a_usd_listing_of_a_foreign_market_is_flagged_as_carrying_currency_risk() -> None:
    """Cycle 17's global test ran through US-listed country ETFs, so FX is in every return.

    `REFUTED.md` #12 found FX itself unusable at Sharpe -0.14, which makes an undeclared
    currency exposure a confound rather than a detail.
    """
    japan = Instrument(
        instrument_id="us-ewj",
        symbol="EWJ",
        venue="ARCX",
        asset_class=AssetClass.ETF,
        quote_currency="USD",
        exposure_currency="JPY",
        exposure_market="JP equity",
        listed_on=date(1996, 3, 12),
    )
    domestic = equity("SPY")

    assert japan.carries_currency_risk
    assert not domestic.carries_currency_risk
    assert InstrumentUniverse([japan, domestic]).unhedged() == (japan,)


def test_asset_classes_and_exposure_markets_are_queryable() -> None:
    """A new asset class is only new statistical budget if it is a genuinely different one."""
    universe = InstrumentUniverse(
        [
            equity("SPY"),
            Instrument(
                instrument_id="crypto-btcusd",
                symbol="BTC/USD",
                venue="CRYPTO",
                asset_class=AssetClass.CRYPTO,
                exposure_currency="USD",
                exposure_market="BTC",
                calendar="CRYPTO24",
                listed_on=date(2021, 1, 1),
            ),
        ]
    )
    assert len(universe.by_asset_class(AssetClass.CRYPTO)) == 1
    assert universe.exposure_markets() == frozenset({"US equity", "BTC"})
    assert universe.by_asset_class(AssetClass.CRYPTO)[0].calendar == "CRYPTO24"


def test_a_corporate_action_cannot_take_effect_before_it_was_announced() -> None:
    with pytest.raises(ValueError, match="before it was announced"):
        CorporateAction(
            instrument_id="us-aapl",
            kind=CorporateActionKind.SPLIT,
            effective_on=date(2020, 8, 31),
            announced_on=date(2020, 9, 1),
            factor="4",
        )
    with pytest.raises(ValueError, match="must carry its factor"):
        CorporateAction(
            instrument_id="us-aapl",
            kind=CorporateActionKind.DIVIDEND,
            effective_on=date(2020, 8, 31),
            announced_on=date(2020, 7, 30),
        )
    action = CorporateAction(
        instrument_id="us-aapl",
        kind=CorporateActionKind.SPLIT,
        effective_on=date(2020, 8, 31),
        announced_on=date(2020, 7, 30),
        factor="4",
    )
    assert action.effective_on > action.announced_on


def test_an_unsupported_capability_raises_instead_of_returning_nothing() -> None:
    """Silence is how a survivorship-free study quietly becomes a live-only one."""

    class LiveOnlyProvider:
        name = "live-only"

        def capabilities(self) -> frozenset[Capability]:
            return frozenset({Capability.BARS})

        def vintage(self, dataset: str) -> Vintage:
            return vintage(dataset=dataset)

        def instruments(self, *, as_of: datetime) -> tuple[Instrument, ...]:
            return (equity("SPY"),)

    provider = LiveOnlyProvider()
    require(provider, Capability.BARS)
    with pytest.raises(UnsupportedCapability, match="DELISTINGS"):
        require(provider, Capability.DELISTINGS)


def test_a_revision_supersedes_its_earlier_vintage_visibly() -> None:
    """A macro series is revised after publication; the value known then is not today's."""
    first = vintage(dataset="fred-cpi", vintage="2026-08-15")
    revised = vintage(dataset="fred-cpi", vintage="2026-09-15", supersedes="2026-08-15")
    assert revised.supersedes == first.vintage
    assert first.vintage != revised.vintage


def test_session_availability_is_the_close_plus_the_vendor_lag() -> None:
    immediate = session_availability(CLOSE)
    assert immediate.observed_at == immediate.available_at == CLOSE

    lagged = session_availability(CLOSE, publication_lag=timedelta(minutes=45))
    assert lagged.available_at == CLOSE + timedelta(minutes=45)
    assert not lagged.known_by(CLOSE)

    with pytest.raises(ValueError, match="timezone-aware"):
        session_availability(datetime(2026, 8, 18, 20, 0))
