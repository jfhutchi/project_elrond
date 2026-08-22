"""One hypothesis asking two different source families the same way (#17).

Sol's criticism of `ResearchDataProvider` was that it was a capability and metadata contract
rather than a common *retrieval* surface: no method through which a study could actually ask for
data, so every source needed its own plumbing and the "same hypothesis requests data through a
common interface" criterion was not demonstrable.

`observations()` is that method. These tests are the demonstration: one function, written once,
consuming macro series and SEC filings without knowing which is which.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantbot.domain import Bar
from quantbot.market_data.equities import EquityProvider
from quantbot.market_data.fred import FredProvider
from quantbot.market_data.instruments import (
    AssetClass,
    CorporateAction,
    CorporateActionKind,
    Instrument,
    InstrumentUniverse,
)
from quantbot.market_data.pointintime import (
    Capability,
    Observation,
    UnsupportedCapability,
    knowable,
    require,
)
from quantbot.research.filings import SecFilingProvider, SecFilingScout

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
RETRIEVED = datetime(2026, 8, 21, 16, 0, tzinfo=UTC)

PAYROLLS_CSV = """DATE,PAYEMS
2026-03-01,158000
2026-04-01,158250
"""


class ScriptedFred:
    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> str:
        return PAYROLLS_CSV


class ScriptedEdgar:
    def fetch(self, url: str, params: dict[str, str], *, timeout_seconds: float) -> bytes:
        return (FIXTURES / "edgar_10k_aapl.xml").read_bytes()


def usable_at(provider, dataset: str, capability: Capability, as_of: datetime) -> int:
    """The study. It has no idea which provider it was handed.

    This is the whole point: one function, written once, that a hypothesis uses against any
    source. If it had to branch on provider type there would be no common interface, only a
    common base class.
    """
    require(provider, capability)
    return len(
        knowable(
            provider.observations(dataset, capability=capability, retrieved_at=RETRIEVED),
            as_of,
        )
    )


def test_the_same_study_reads_macro_series_and_filings_through_one_interface() -> None:
    """Bars, macro values and filings share nothing except the property that matters here.

    Each became knowable at some instant, and using it earlier is look-ahead. That is enough to
    build one retrieval surface on, and it is the only thing all three genuinely have in common.
    """
    fred = FredProvider(ScriptedFred())
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))

    macro = usable_at(fred, "PAYEMS", Capability.MACRO_SERIES, RETRIEVED)
    filings = usable_at(edgar, "0000320193:10-K", Capability.FUNDAMENTALS, RETRIEVED)

    assert macro > 0, "the macro series should have knowable observations"
    assert filings > 0, "the filings should have knowable observations"


def test_the_point_in_time_filter_is_the_same_one_for_both() -> None:
    """`knowable()` is what makes the interface worth having.

    Before an early cutoff neither source has anything usable, and the study finds that out the
    same way for both -- rather than each source needing its own look-ahead plumbing that could
    each be wrong in its own way.
    """
    fred = FredProvider(ScriptedFred())
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))
    long_ago = datetime(1990, 1, 1, tzinfo=UTC)

    assert usable_at(fred, "PAYEMS", Capability.MACRO_SERIES, long_ago) == 0
    assert usable_at(edgar, "0000320193:10-K", Capability.FUNDAMENTALS, long_ago) == 0


def test_asking_a_provider_for_something_it_does_not_serve_raises() -> None:
    """An empty tuple would read as an absence of data rather than an absence of capability.

    That distinction is the same one the scout draws between an outage and a zero-result
    search, one layer down.
    """
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))

    with pytest.raises(UnsupportedCapability):
        usable_at(edgar, "0000320193:10-K", Capability.BARS, RETRIEVED)

    with pytest.raises(UnsupportedCapability):
        edgar.instruments(as_of=RETRIEVED)


def test_every_observation_carries_its_own_vintage_whichever_source_it_came_from() -> None:
    """Lineage travels with the value, so a result can say which pull produced it."""
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))

    observations = edgar.observations(
        "0000320193:10-K", capability=Capability.FUNDAMENTALS, retrieved_at=RETRIEVED
    )

    assert observations
    for item in observations:
        assert isinstance(item, Observation)
        assert item.vintage.provider == "sec-edgar"
        assert item.vintage.retrieved_at == RETRIEVED
        # The filing date is both when it happened and when it became knowable: the feed carries
        # no period-covered field, and inventing one would be the look-ahead this refuses.
        assert item.availability.observed_at == item.availability.available_at


def test_a_filings_dataset_must_name_a_form_type() -> None:
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))

    with pytest.raises(ValueError, match="CIK:FORM"):
        edgar.observations(
            "0000320193", capability=Capability.FUNDAMENTALS, retrieved_at=RETRIEVED
        )


def test_the_provider_itself_refuses_a_wrong_capability_not_only_the_helper() -> None:
    """Found by a mutation that survived: removing `require()` from `observations()` changed
    nothing, because the study helper above calls `require()` first.

    So the earlier test proved the helper checks, not the provider -- and a caller that reached
    for `observations()` directly, which the protocol invites, would have got filings back
    while asking for bars. Asserted against the provider with no helper in between.
    """
    edgar = SecFilingProvider(SecFilingScout(ScriptedEdgar(), now=lambda: RETRIEVED))
    fred = FredProvider(ScriptedFred())

    with pytest.raises(UnsupportedCapability):
        edgar.observations(
            "0000320193:10-K", capability=Capability.BARS, retrieved_at=RETRIEVED
        )
    with pytest.raises(UnsupportedCapability):
        fred.observations("PAYEMS", capability=Capability.FUNDAMENTALS, retrieved_at=RETRIEVED)


def instrument(
    identifier: str, symbol: str, *, listed: date, delisted: date | None = None
) -> Instrument:
    return Instrument(
        instrument_id=identifier,
        symbol=symbol,
        venue="XNYS",
        asset_class=AssetClass.EQUITY,
        exposure_market="US equity",
        listed_on=listed,
        delisted_on=delisted,
    )


def equity_provider() -> EquityProvider:
    """Two names live in 2019, one of which stopped trading before the record ends.

    The delisted one is the whole point. A universe assembled today from names that still trade
    would never contain it, and no number in the resulting study would show that it was missing.
    """
    universe = InstrumentUniverse(
        [
            instrument("us-survivor", "AAA", listed=date(2015, 1, 2)),
            instrument("us-gone", "BBB", listed=date(2015, 1, 2), delisted=date(2020, 6, 30)),
        ]
    )
    bars = {
        "AAA": [
            Bar(
                symbol="AAA",
                timestamp=datetime(2019, 3, day, tzinfo=UTC),
                open="100",
                high="101",
                low="99",
                close="100",
                volume="1000",
                adjustment="1",
            )
            for day in (1, 4, 5)
        ]
    }
    return EquityProvider(
        bars,
        universe,
        session_close=lambda moment: moment.replace(hour=21, minute=0),
        actions=[
            CorporateAction(
                instrument_id="us-survivor",
                kind=CorporateActionKind.SPLIT,
                announced_on=date(2019, 3, 4),
                effective_on=date(2019, 3, 20),
                factor="2",
            )
        ],
    )


def test_equity_and_macro_answer_the_same_study_function() -> None:
    """Sol's box, named exactly: equity/ETF and macro through one retrieval interface.

    `usable_at` is the study. It branches on nothing -- it is handed a provider and a capability
    and asks. Two entirely different source families, one call.
    """
    macro = FredProvider(ScriptedFred())
    equities = equity_provider()

    macro_count = usable_at(
        macro, "PAYEMS", Capability.MACRO_SERIES, datetime(2026, 5, 1, tzinfo=UTC)
    )
    equity_count = usable_at(equities, "AAA", Capability.BARS, datetime(2019, 3, 6, tzinfo=UTC))

    assert macro_count > 0, "the macro series was readable through the shared surface"
    assert equity_count > 0, "so were the bars, through the same function"


def test_survivorship_is_measurable_rather_than_asserted() -> None:
    """Sol's second box: survivorship exercised end to end, not declared small.

    The universe as of 2019 contains a name that stopped trading in 2020. A study assembled from
    today's survivors would silently drop it, and every cross-sectional number would be
    flattering in a way nothing in the output reveals. `survivorship_bias` makes the exposure a
    number the study can look at and refuse to proceed on.
    """
    equities = equity_provider()
    as_of = datetime(2019, 6, 30, tzinfo=UTC)

    members = equities.instruments(as_of=as_of)
    exposure = equities.survivorship_bias(as_of=as_of)

    assert {item.symbol for item in members} == {"AAA", "BBB"}, "the delisted name is present"
    assert exposure == pytest.approx(0.5), "half the 2019 universe did not survive"

    # And after the delisting it is genuinely gone, so membership is a function of time rather
    # than a fixed list with a flag nobody reads.
    later = equities.instruments(as_of=datetime(2021, 1, 4, tzinfo=UTC))
    assert {item.symbol for item in later} == {"AAA"}


def test_a_corporate_action_is_filtered_by_announcement_not_effect() -> None:
    """Adjusting on the effective date uses an announcement the market had not seen.

    The split here is announced on the 4th and effective on the 20th. A backtest deciding on the
    10th may know about it; one deciding on the 1st may not, and a provider that filtered on
    `effective_on` would hand it to both.
    """
    equities = equity_provider()

    before = equities.corporate_actions(
        "us-survivor", knowable_by=datetime(2019, 3, 1, tzinfo=UTC)
    )
    after = equities.corporate_actions(
        "us-survivor", knowable_by=datetime(2019, 3, 10, tzinfo=UTC)
    )

    assert before == (), "an unannounced action must not be visible"
    assert len(after) == 1, "an announced action is visible before it takes effect"
    assert after[0].effective_on > date(2019, 3, 10), "and it has not taken effect yet"
