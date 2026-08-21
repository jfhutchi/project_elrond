"""Equities on the common interface, and the survivorship failure that is invisible in results.

A cross-sectional study run on a live-only universe does not look broken. It looks fine, and it
is about a universe selected after the fact. Nothing in the numbers shows it, which is why the
provider has to answer the membership question honestly rather than leave it to the caller to
remember.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from quantbot.domain import Bar
from quantbot.market_data.equities import EquityProvider
from quantbot.market_data.instruments import (
    AssetClass,
    CorporateAction,
    CorporateActionKind,
    Instrument,
    universe_from,
)
from quantbot.market_data.pointintime import (
    Capability,
    UnsupportedCapability,
    knowable,
    latest_knowable,
)

RETRIEVED = datetime(2026, 8, 21, 20, 0, tzinfo=UTC)


def instrument(symbol: str, *, listed: date, delisted: date | None = None) -> Instrument:
    return Instrument(
        instrument_id=f"id-{symbol}",
        symbol=symbol,
        venue="XNYS",
        asset_class=AssetClass.EQUITY,
        exposure_market="US equity",
        listed_on=listed,
        delisted_on=delisted,
    )


def bar(symbol: str, day: date, close: str = "100") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
        open=close,
        high=close,
        low=close,
        close=close,
        volume="1000",
        adjustment="1",
    )


def close_at_21z(timestamp: datetime) -> datetime:
    """A session closes at 21:00 UTC. Injected because it is a calendar fact, not a guess."""
    return timestamp.replace(hour=21, minute=0)


def provider(**overrides: object) -> EquityProvider:
    universe = universe_from(
        [
            instrument("SPY", listed=date(1993, 1, 29)),
            # Listed, then gone. The name a live-only universe silently drops.
            instrument("DEAD", listed=date(2015, 1, 2), delisted=date(2020, 6, 30)),
        ]
    )
    return EquityProvider(
        bars={
            "SPY": [bar("SPY", date(2026, 8, 20)), bar("SPY", date(2026, 8, 21))],
            "DEAD": [bar("DEAD", date(2020, 6, 29))],
        },
        universe=universe,
        session_close=close_at_21z,
        actions=overrides.get("actions", ()),  # type: ignore[arg-type]
    )


def test_a_delisted_name_is_still_a_member_of_the_date_it_traded() -> None:
    """The survivorship-honest answer, and the reason DELISTINGS is declared.

    A provider returning only survivors hands every cross-sectional study a flattering universe
    selected after the fact, and nothing in the numbers shows it. This is not a result that looks
    broken; it is a result that looks fine and is about the wrong population.
    """
    members = provider().instruments(as_of=datetime(2019, 1, 2, tzinfo=UTC))

    assert {item.symbol for item in members} == {"SPY", "DEAD"}


def test_the_same_name_is_gone_after_it_stopped_trading() -> None:
    """Membership is a function of time in both directions, or it is not point-in-time."""
    members = provider().instruments(as_of=datetime(2021, 1, 4, tzinfo=UTC))

    assert {item.symbol for item in members} == {"SPY"}


def test_the_survivorship_exposure_is_measurable_rather_than_asserted() -> None:
    """If a cross-sectional result moves with this number, the result is about survivorship.

    Exposing it is what lets a study check that rather than assert the exposure is small.
    """
    equities = provider()

    during = equities.survivorship_bias(as_of=datetime(2019, 1, 2, tzinfo=UTC))
    after = equities.survivorship_bias(as_of=datetime(2021, 1, 4, tzinfo=UTC))

    assert during == pytest.approx(0.5), "one of the two names later stopped trading"
    assert after == 0.0


def test_a_bar_is_not_knowable_during_its_own_session() -> None:
    """The most common look-ahead in daily backtesting.

    A strategy acting on the close of the day it trades is using a number that did not exist when
    the decision was made -- and the result looks prescient rather than broken.
    """
    equities = provider()
    observations = equities.observations(
        "SPY", capability=Capability.BARS, retrieved_at=RETRIEVED
    )

    midday = datetime(2026, 8, 21, 15, 0, tzinfo=UTC)
    after_close = datetime(2026, 8, 21, 21, 0, tzinfo=UTC)

    usable_at_midday = {item.label for item in knowable(observations, midday)}
    usable_after = {item.label for item in knowable(observations, after_close)}

    assert "SPY:2026-08-21" not in usable_at_midday, "today's close is not knowable at midday"
    assert "SPY:2026-08-20" in usable_at_midday, "yesterday's close is"
    assert "SPY:2026-08-21" in usable_after


def test_the_latest_usable_bar_is_the_one_a_decision_may_actually_use() -> None:
    equities = provider()
    observations = equities.observations(
        "SPY", capability=Capability.BARS, retrieved_at=RETRIEVED
    )

    latest = latest_knowable(observations, datetime(2026, 8, 21, 15, 0, tzinfo=UTC))

    assert latest is not None
    assert latest.label == "SPY:2026-08-20"


def test_a_symbol_nobody_loaded_is_not_a_symbol_that_did_not_trade() -> None:
    """Returning () would report an absence of trading rather than an absence of data.

    Same distinction the scout draws between an outage and a zero-result search, and the same
    reason: one is a fact about the market and the other is a fact about our records.
    """
    with pytest.raises(UnsupportedCapability):
        provider().observations("NOBODY", capability=Capability.BARS, retrieved_at=RETRIEVED)


def test_a_capability_this_provider_does_not_serve_raises() -> None:
    with pytest.raises(UnsupportedCapability):
        provider().observations(
            "SPY", capability=Capability.MACRO_SERIES, retrieved_at=RETRIEVED
        )


def test_a_corporate_action_is_knowable_from_its_announcement_not_its_effect() -> None:
    """A backtest adjusting on the effective date is using an announcement it had not seen.

    That shifts the adjustment earlier than the market could have applied it, which is
    look-ahead wearing the clothes of a data-cleaning step.
    """
    split = CorporateAction(
        instrument_id="id-SPY",
        kind=CorporateActionKind.SPLIT,
        announced_on=date(2026, 8, 10),
        effective_on=date(2026, 8, 20),
        factor="2",
    )
    equities = provider(actions=(split,))

    before_announcement = equities.corporate_actions(
        "id-SPY", knowable_by=datetime(2026, 8, 5, tzinfo=UTC)
    )
    after_announcement = equities.corporate_actions(
        "id-SPY", knowable_by=datetime(2026, 8, 15, tzinfo=UTC)
    )

    assert before_announcement == ()
    assert after_announcement == (split,), "announced but not yet effective is still knowable"


def test_corporate_actions_are_keyed_on_instrument_identity_not_ticker() -> None:
    """A symbol is reused by later companies and changes under the same one.

    An action looked up by ticker eventually attaches to the wrong series, and the resulting
    adjustment is silently wrong rather than absent.
    """
    split = CorporateAction(
        instrument_id="id-SPY",
        kind=CorporateActionKind.SPLIT,
        announced_on=date(2026, 8, 10),
        effective_on=date(2026, 8, 20),
        factor="2",
    )
    equities = provider(actions=(split,))

    assert equities.corporate_actions("id-SPY") == (split,)
    assert equities.corporate_actions("SPY") == (), "the ticker is not the identity"


def test_a_study_written_once_reads_equities_the_same_way_it_reads_macro() -> None:
    """#17's acceptance criterion, with the asset class this project actually trades.

    The helper knows nothing about equities. It asks the interface and filters with `knowable`,
    exactly as it would for FRED or EDGAR.
    """

    def usable(source, dataset: str, capability: Capability, as_of: datetime) -> int:
        return len(
            knowable(
                source.observations(dataset, capability=capability, retrieved_at=RETRIEVED),
                as_of,
            )
        )

    equities = provider()

    # RETRIEVED is 20:00 UTC and the session closes at 21:00, so today's bar is not knowable
    # yet -- one of two. This assertion was wrong before it was right, which is the rule doing
    # its job: the intuitive answer is "both bars, they are in the data", and that answer is
    # look-ahead.
    assert usable(equities, "SPY", Capability.BARS, RETRIEVED) == 1
    assert usable(equities, "SPY", Capability.BARS, RETRIEVED.replace(hour=22)) == 2
    assert usable(equities, "SPY", Capability.BARS, RETRIEVED - timedelta(days=30)) == 0
