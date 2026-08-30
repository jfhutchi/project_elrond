"""What a feed can actually measure, and the refusal when it cannot (#56 phase 1).

The anomaly engine's core trigger is a volume shock: `interval_rvol = observed / expected`, plus
a robust z against a trailing baseline. That quantity is only meaningful if the feed's printed
volume tracks the volume the market actually traded.

**It does not, on IEX, and the gap is not marginal.** Measured on the research store, which holds
both feeds for the same 23 symbols over the same sessions -- robust z on log volume, 60-session
trailing median/MAD, threshold 4.0, exactly the seed condition in #56, over 2021-2026:

    IEX-triggered events     93
    SIP-triggered events    149
      agreed                 34
      IEX only               59      precision 36.6%
      SIP only              115      recall    22.8%

Two of every three IEX triggers are not real consolidated shocks, and it misses three of every
four that are. The cause is scale: median IEX share of consolidated volume is 2.11%, so its
print is a small, noisy sample of the activity being measured.

**And the sample is not stationary.** Median IEX share by complete year runs 1.47%, 1.84%, 1.86%,
2.32%, 3.99%, 5.36% from 2021 to 2026 -- a 3.6x drift. A trailing baseline computed on IEX is
therefore partly measuring IEX's own growing market share rather than the instrument's activity,
and the drift is upward, which inflates today's observation against its own history. That
contaminates the baseline in the direction that manufactures events.

**So capability is a gate rather than a label.** #56 asks that feed semantics be persisted so
capability is never silently read as full-market volume. Recording the feed honestly on a bad
event still leaves a bad event, and the routing layer still spends on it. A detector asked for a
volume-derived trigger on a venue feed refuses instead, the same way the risk layer refuses an
unmeasurable input rather than substituting one.

Price and volatility triggers are a separate question -- a venue's prints track consolidated
prices far better than its volume tracks consolidated volume -- so they are admissible on a
venue feed and are measured separately before being enabled.
"""

from __future__ import annotations

from enum import StrEnum

from quantbot.market_data.base import MarketDataFeed


class VolumeCoverage(StrEnum):
    """How much of the traded volume a feed reports."""

    #: Every venue's prints. `interval_rvol` means what the detector needs it to mean.
    CONSOLIDATED = "CONSOLIDATED"
    #: One venue's prints. A sample of consolidated activity, and on IEX a 2% one whose share
    #: drifts, so volume-derived triggers are refused.
    SINGLE_VENUE = "SINGLE_VENUE"
    #: No consolidated tape exists to be a fraction of. Crypto venue volume is the venue's own
    #: truth rather than a sample of something larger, so it is not the IEX problem.
    NO_CONSOLIDATED_TAPE = "NO_CONSOLIDATED_TAPE"
    #: The feed is not one this module has characterised. Refuses, because an uncharacterised
    #: feed is exactly the case where assuming coverage is how the defect returns.
    UNKNOWN = "UNKNOWN"


#: Measured or documented per feed. `DELAYED_SIP` carries the consolidated tape and is
#: consolidated for *coverage* purposes; whether its latency suits a live detector is a freshness
#: question that `MarketFreshnessPolicy` answers separately (#74), not a coverage one.
FEED_COVERAGE: dict[MarketDataFeed, VolumeCoverage] = {
    MarketDataFeed.SIP: VolumeCoverage.CONSOLIDATED,
    MarketDataFeed.DELAYED_SIP: VolumeCoverage.CONSOLIDATED,
    MarketDataFeed.IEX: VolumeCoverage.SINGLE_VENUE,
    MarketDataFeed.BOATS: VolumeCoverage.SINGLE_VENUE,
    MarketDataFeed.OTC: VolumeCoverage.SINGLE_VENUE,
    MarketDataFeed.OVERNIGHT: VolumeCoverage.SINGLE_VENUE,
    MarketDataFeed.CRYPTO: VolumeCoverage.NO_CONSOLIDATED_TAPE,
}


def volume_coverage(feed: MarketDataFeed) -> VolumeCoverage:
    """What this feed reports. Unlisted feeds are `UNKNOWN`, which refuses."""
    return FEED_COVERAGE.get(feed, VolumeCoverage.UNKNOWN)


def supports_volume_trigger(feed: MarketDataFeed) -> bool:
    """Whether a volume-derived anomaly may be emitted from this feed at all.

    The measurement in the module docstring is the whole argument. A 37%-precision trigger does
    not become acceptable by being correctly labelled.
    """
    return volume_coverage(feed) in {
        VolumeCoverage.CONSOLIDATED,
        VolumeCoverage.NO_CONSOLIDATED_TAPE,
    }


def volume_trigger_refusal(feed: MarketDataFeed) -> str | None:
    """Why a volume trigger is refused on this feed, or `None` when it is permitted."""
    coverage = volume_coverage(feed)
    if coverage is VolumeCoverage.CONSOLIDATED:
        return None
    if coverage is VolumeCoverage.NO_CONSOLIDATED_TAPE:
        return None
    if coverage is VolumeCoverage.UNKNOWN:
        return (
            f"{feed.value} has no characterised volume coverage, so whether its prints sample "
            "the market or report it is unknown; an uncharacterised feed is refused rather than "
            "assumed to be consolidated"
        )
    return (
        f"{feed.value} reports a single venue's prints rather than the consolidated tape. "
        "Measured against SIP over 2021-2026 a volume-shock trigger on this feed has 36.6% "
        "precision and 22.8% recall, and the venue's share of consolidated volume drifted 3.6x "
        "over the same period, so a trailing baseline partly tracks the venue rather than the "
        "instrument"
    )


__all__ = [
    "FEED_COVERAGE",
    "VolumeCoverage",
    "supports_volume_trigger",
    "volume_coverage",
    "volume_trigger_refusal",
]
