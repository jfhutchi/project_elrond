"""What confirmatory evidence is left, by dataset, window and universe (#50).

`window_consumption` already answers "is *this* range spent" for a range someone asks about. That
is the wrong shape for the question an operator and the research director actually need, which is:

> What observations remain unused for confirmatory testing, and what classes of hypotheses can
> legitimately use them?

Nothing answered that. The consequence is the misreading #50 was opened about: closing #17 built
real data-platform capability -- point-in-time contracts, survivorship handling, corporate
actions, crypto, FRED/ALFRED macro -- and none of it replenished the exhausted US-equity evidence
budget. `DATA_PLATFORM_ENGINEERING_COMPLETE` and `UNUSED_CONFIRMATORY_CAPACITY_AVAILABLE` are
different facts, and the second is the one that decides whether an expensive search is worth
running.

**Declared sources, derived status.** `DATA_SOURCES` is hand-maintained, for the same reason
`OBSERVATION_ALIASES` is: what a dataset observes, and which claims it can legitimately bear on,
is a judgement somebody has to make and be accountable for. Everything else on a row --
consumption, exhaustion, who spent it, which family, what remains, what is reserved -- is
computed from durable state. A declared status would be a number nobody updates.

**Two providers of the same market outcome are one row.** Rows are keyed by *observation
identity*, so `sip-us-equities-daily` and `alpaca-us-equities-daily` collapse into one entry with
one budget. Provider diversity establishes data correctness and can repair a bad bar; it does not
duplicate the market event, and counting it twice would hand out significance that was already
spent (#40).

**Rolling sources grow, and only by observations that exist.** A feed that keeps producing
sessions accrues genuinely unsearched ones: nobody searched a session that had not happened. The
first version of this module derived that from the *calendar* -- the day after the last consumed
date -- and GPT-5.6 Sol's review predicted the defect before the code shipped, which is what
happened: on 2026-08-22 the equity row reported "unsearched sessions from 2026-08-22", a
**Saturday**. A weekend is not capacity, and neither is a holiday, an outage, or a gap in the
vendor's history.

New capacity is now the set of **session dates the database actually holds observations for**,
after the last consumed one, attributed to this source by its `bars.provider` prefix. A source
with no local observation store claims none, which is conservative and correct: the project
cannot confirm anything against data it does not have.

**`UNSUPPORTED` is derived, not declared.** A capability nothing implements reports as
unsupported by comparing `DATA_SOURCES` against the capabilities the implemented providers
actually serve. `OPTIONS_DERIVED` is declared in `Capability` and in `AssetClass` and has no
provider behind it; a row saying `AVAILABLE` for it would be capacity the project cannot spend.

**What this does not do.** It does not decide whether a hypothesis may legitimately use a source.
`answers` is prose, deliberately: "does this macro series bear on this equity claim" is a question
about mechanism, and a machine-readable applicability field would invite an agent to satisfy it by
matching strings. `discovery.admissible` already refuses a candidate whose declared windows are
exhausted, before the power gate and the critic, which is the mechanical half.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from types import MappingProxyType

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from quantbot.market_data.calendar import XNYS_TIMEZONE
from quantbot.market_data.equities import EQUITY_CAPABILITIES
from quantbot.market_data.instruments import AssetClass
from quantbot.market_data.pointintime import Capability
from quantbot.research.datasets import equivalent_datasets, observation_identity
from quantbot.research.registry import (
    PRIOR_WINDOW_CONSUMPTION,
    DataRole,
    HypothesisRegistry,
    WindowState,
)
from quantbot.storage.database import decode_utc
from quantbot.storage.schema import bars, hypothesis_data_windows

#: Capabilities an implemented provider actually serves, as opposed to ones the enum declares.
#:
#: Assembled from the providers' own constants rather than restated, and
#: `test_every_implemented_provider_is_represented_in_served_capabilities` walks `src/` to check
#: nothing was added without landing here. A new provider that serves `OPTIONS_DERIVED` flips
#: that row to `AVAILABLE` by existing, which is the direction that has to work: an inventory
#: that has to be remembered is one that goes stale in the flattering direction.
SERVED_CAPABILITIES: frozenset[Capability] = frozenset(
    EQUITY_CAPABILITIES | {Capability.MACRO_SERIES, Capability.FUNDAMENTALS}
)


class CapacityStatus(StrEnum):
    """What can still be done with a source, in the order of decreasing usefulness."""

    #: Nothing has been searched here. Confirmatory work may proceed.
    AVAILABLE = "AVAILABLE"
    #: A live registration holds it. Not spent, not free -- and the claim lapses (#22).
    RESERVED = "RESERVED"
    #: Searched, but not past the point where the luck bar makes confirmation hopeless.
    PARTIALLY_CONSUMED = "PARTIALLY_CONSUMED"
    #: Spent. A result here can only ever be exploratory, however good it looks.
    EXHAUSTED = "EXHAUSTED"
    #: Declared as a capability with no implemented provider behind it. Not capacity.
    UNSUPPORTED = "UNSUPPORTED"
    #: A provider exists and cannot answer point-in-time questions, so a backtest against it
    #: cannot be trusted about what was knowable when.
    QUALITY_BLOCKED = "QUALITY_BLOCKED"


@dataclass(frozen=True, slots=True)
class DataSource:
    """One declared source of observations, and what a claim must be about to use it."""

    dataset: str
    asset_class: AssetClass
    capability: Capability
    #: Earliest outcome the project can actually obtain, not the earliest the vendor has.
    earliest: date
    #: `None` for a live feed: capacity grows with the calendar, so there is no fixed end.
    latest: date | None
    #: Whether the source answers "what was knowable on date D" rather than "what is true now".
    point_in_time: bool
    permitted_roles: tuple[DataRole, ...]
    #: What a claim has to be about for these observations to bear on it. Prose on purpose.
    answers: str
    #: `bars.provider` prefixes whose rows are observations of this source. The provider key is
    #: composite (`alpaca:sip:split:1Day`), so a prefix identifies a feed without pinning the
    #: adjustment or timeframe. Empty means the project keeps no local observation store for
    #: this source, and therefore claims no new sessions for it -- the conservative answer, and
    #: the true one: nothing can be confirmed against data that is not here.
    observation_prefixes: tuple[str, ...] = ()

    @property
    def observation_identity(self) -> str:
        return observation_identity(self.dataset)


#: The sources this project can actually reach, and what each one can legitimately answer.
#:
#: Hand-maintained and short, like `OBSERVATION_ALIASES`. Each entry is a claim about the world
#: that somebody checked; deriving it from what happens to be in the local database would report
#: whatever was last downloaded as though it were the project's evidence budget.
DATA_SOURCES: tuple[DataSource, ...] = (
    DataSource(
        dataset="sip-us-equities-daily",
        asset_class=AssetClass.EQUITY,
        capability=Capability.BARS,
        earliest=date(2016, 1, 4),
        latest=None,
        point_in_time=True,
        permitted_roles=(
            DataRole.DISCOVERY,
            DataRole.VALIDATION,
            DataRole.PROTECTED_EVALUATION,
        ),
        answers=(
            "US equity and ETF daily-bar claims. Cycles 2-10 spent 68 trials across this range "
            "on momentum, trend, volatility and overnight effects; a new claim about daily "
            "equity prices lands on an exhausted window whatever family it is filed under"
        ),
        observation_prefixes=("alpaca:sip:", "alpaca:iex:"),
    ),
    DataSource(
        dataset="crypto-daily",
        asset_class=AssetClass.CRYPTO,
        capability=Capability.BARS,
        earliest=date(2021, 1, 1),
        latest=None,
        point_in_time=True,
        permitted_roles=(
            DataRole.DISCOVERY,
            DataRole.VALIDATION,
            DataRole.PROTECTED_EVALUATION,
        ),
        answers=(
            "Crypto claims, and cross-asset generality for a mechanism that claims it. NOT new "
            "evidence for a US-equity-specific effect: a different market is a different "
            "question, and reusing it as confirmation for the equity claim is the rename "
            "defect wearing an asset class"
        ),
    ),
    DataSource(
        dataset="fred-macro-monthly",
        asset_class=AssetClass.MACRO,
        capability=Capability.MACRO_SERIES,
        earliest=date(1960, 1, 1),
        latest=None,
        point_in_time=True,
        permitted_roles=(
            DataRole.DISCOVERY,
            DataRole.VALIDATION,
            DataRole.PROTECTED_EVALUATION,
        ),
        answers=(
            "Claims that genuinely concern macro information, with ALFRED vintages so a "
            "revised figure cannot be read as what was published. Adding a macro series does "
            "not create an independent equity sample -- it creates a new information "
            "dimension whose own dependence structure and effective sample must be modelled"
        ),
    ),
    DataSource(
        dataset="sec-edgar-filings",
        asset_class=AssetClass.EQUITY,
        capability=Capability.FUNDAMENTALS,
        earliest=date(2009, 1, 1),
        latest=None,
        point_in_time=True,
        permitted_roles=(
            DataRole.DISCOVERY,
            DataRole.VALIDATION,
            DataRole.PROTECTED_EVALUATION,
        ),
        answers=(
            "Claims about disclosed fundamentals, dated by filing acceptance rather than by "
            "period end. Untouched by cycles 2-10, which never looked at filings"
        ),
    ),
    DataSource(
        dataset="options-implied-daily",
        asset_class=AssetClass.OPTIONS_DERIVED,
        capability=Capability.OPTIONS_DERIVED,
        earliest=date(2016, 1, 4),
        latest=None,
        point_in_time=True,
        permitted_roles=(),
        answers=(
            "Implied volatility, skew and put/call ratios as *signals*. REFUTED.md #9 killed "
            "trading options at this account size; using options-implied information as an "
            "input has never been tested here. There is no provider, so this is a research "
            "direction rather than capacity"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CapacityRow:
    """One observation identity, what has been spent on it, and what is left."""

    source: DataSource
    #: Every dataset name that observes the same thing. Two vendors, one budget (#40).
    aliases: tuple[str, ...]
    status: CapacityStatus
    trials: int
    consumers: tuple[str, ...]
    by_family: Mapping[str, int]
    #: Session dates this database holds observations for that no search has consumed. Real
    #: sessions, not calendar days: a weekend has no bar and therefore no capacity.
    unsearched_sessions: tuple[date, ...]
    #: Registrations holding a live claim on part of this range.
    reserved_by: tuple[str, ...]

    @property
    def observation_identity(self) -> str:
        return self.source.observation_identity

    @property
    def unconsumed_from(self) -> date | None:
        """The first unsearched session, or `None` when there is not one."""
        return self.unsearched_sessions[0] if self.unsearched_sessions else None

    @property
    def usable_for_confirmation(self) -> bool:
        """Whether a confirmatory experiment against this source could mean anything.

        Unsearched sessions rescue an exhausted rolling source, and that is not a loophole: a
        session that had not happened when the searching was done was not searched. It is also
        not much, and the count says how much rather than implying an open-ended stream.
        """
        if self.status in {CapacityStatus.UNSUPPORTED, CapacityStatus.QUALITY_BLOCKED}:
            return False
        if self.status is CapacityStatus.EXHAUSTED:
            return bool(self.unsearched_sessions)
        return True

    def explain(self) -> str:
        parts = [f"{self.observation_identity}: {self.status.value}"]
        if self.trials:
            parts.append(f"{self.trials} trials spent")
        if self.reserved_by:
            parts.append("reserved by " + ", ".join(self.reserved_by))
        if self.unsearched_sessions:
            parts.append(
                f"{len(self.unsearched_sessions)} unsearched session(s) from "
                f"{self.unsearched_sessions[0].isoformat()}"
            )
        return "; ".join(parts)


def _sessions_after(
    session: Session,
    *,
    prefixes: Sequence[str],
    after: date | None,
    until: date,
) -> tuple[date, ...]:
    """Session dates this database holds observations for, strictly after `after`.

    The answer to "is there anything new to search", made of observations rather than of
    calendar arithmetic. A Saturday has no bar, so it cannot appear here; neither can a holiday,
    an outage, or a stretch the vendor never served. `after=None` means nothing has been
    consumed, in which case every stored session in range is unsearched.

    Keyed on the **Eastern** session date for the reason `_session_equity` is: a daily bar
    stamped at the close is a different UTC date, and counting by UTC would shift every session
    by one and invent one at each end.
    """
    if not prefixes:
        return ()
    statement = select(bars.c.timestamp).where(
        or_(*(bars.c.provider.startswith(prefix) for prefix in prefixes))
    )
    found: set[date] = set()
    for row in session.execute(statement):
        session_date = decode_utc(str(row[0])).astimezone(XNYS_TIMEZONE).date()
        if session_date > until:
            continue
        if after is not None and session_date <= after:
            continue
        found.add(session_date)
    return tuple(sorted(found))


def stored_sessions(session: Session, source: DataSource, *, as_of: date) -> tuple[date, ...]:
    """Every session this store holds for `source`, regardless of what has been consumed.

    `CapacityRow.unsearched_sessions` answers "is there anything new", which collapses two very
    different states into zero: a range that has been fully searched, and a source with no local
    observations at all. A proposer has to tell those apart -- the first means wait, the second
    means this dataset cannot be studied here whatever its status says -- so the unconsumed
    question is asked separately rather than inferred.

    Public, and thin on purpose: the alternative was a second implementation of "which rows are
    observations of this source" somewhere else, which would eventually disagree with this one,
    and the direction it disagreed in would decide whether a question got asked.
    """
    return _sessions_after(
        session, prefixes=source.observation_prefixes, after=None, until=as_of
    )


def _consumed_span(
    session: Session, *, aliases: Sequence[str], start: date, end: date
) -> tuple[date, date] | None:
    """The outer bounds of everything ever *consumed* on this identity, across all names.

    Reservations are excluded for the reason `window_consumption` excludes them: a reservation
    is a trial someone intends to spend, not one they have spent, and it lapses.
    """
    spans: list[tuple[date, date]] = [
        (prior_start, prior_end)
        for name, prior_start, prior_end, _ in PRIOR_WINDOW_CONSUMPTION
        if name in aliases and prior_start <= end and prior_end >= start
    ]
    rows = session.execute(
        select(
            hypothesis_data_windows.c.start_date,
            hypothesis_data_windows.c.end_date,
        ).where(
            hypothesis_data_windows.c.dataset.in_(aliases),
            hypothesis_data_windows.c.start_date <= end.isoformat(),
            hypothesis_data_windows.c.end_date >= start.isoformat(),
            hypothesis_data_windows.c.state == WindowState.CONSUMED.value,
        )
    ).all()
    spans.extend(
        (date.fromisoformat(str(row[0])), date.fromisoformat(str(row[1]))) for row in rows
    )
    if not spans:
        return None
    return min(span[0] for span in spans), max(span[1] for span in spans)


def _reservations(
    session: Session, *, aliases: Sequence[str], start: date, end: date, as_of: date
) -> tuple[str, ...]:
    """Live claims on this range. A lapsed reservation is not a claim and is not listed."""
    rows = session.execute(
        select(
            hypothesis_data_windows.c.hypothesis_id,
            hypothesis_data_windows.c.version,
            hypothesis_data_windows.c.reserved_until,
        ).where(
            hypothesis_data_windows.c.dataset.in_(aliases),
            hypothesis_data_windows.c.start_date <= end.isoformat(),
            hypothesis_data_windows.c.end_date >= start.isoformat(),
            hypothesis_data_windows.c.state == WindowState.RESERVED.value,
        )
    ).all()
    return tuple(
        sorted(
            {
                f"{row[0]} v{row[1]}"
                for row in rows
                if row[2] is not None and date.fromisoformat(str(row[2])) >= as_of
            }
        )
    )


_CONSUMPTION_STATUS: Mapping[str, CapacityStatus] = MappingProxyType(
    {
        "UNTOUCHED": CapacityStatus.AVAILABLE,
        "PARTIALLY_CONSUMED": CapacityStatus.PARTIALLY_CONSUMED,
        "EXHAUSTED": CapacityStatus.EXHAUSTED,
    }
)


def capacity_row(session: Session, source: DataSource, *, as_of: date) -> CapacityRow:
    """One source's real position, derived from durable state rather than declared."""
    aliases = equivalent_datasets(source.dataset)
    end = source.latest if source.latest is not None else as_of
    if source.capability not in SERVED_CAPABILITIES:
        # Before any consumption query, because an unsupported capability has no observations to
        # have spent. Reporting `AVAILABLE` here would be capacity the project cannot spend.
        return CapacityRow(
            source=source,
            aliases=aliases,
            status=CapacityStatus.UNSUPPORTED,
            trials=0,
            consumers=(),
            by_family={},
            unsearched_sessions=(),
            reserved_by=(),
        )
    if not source.point_in_time:
        return CapacityRow(
            source=source,
            aliases=aliases,
            status=CapacityStatus.QUALITY_BLOCKED,
            trials=0,
            consumers=(),
            by_family={},
            unsearched_sessions=(),
            reserved_by=(),
        )

    consumption = HypothesisRegistry(session).window_consumption(
        source.dataset, source.earliest, end
    )
    status = _CONSUMPTION_STATUS[consumption.status]
    reserved = _reservations(
        session, aliases=aliases, start=source.earliest, end=end, as_of=as_of
    )
    if status is CapacityStatus.AVAILABLE and reserved:
        status = CapacityStatus.RESERVED

    # Observations, not calendar days. The earlier version added a day to the last consumed
    # date and reported that as capacity, which produced a Saturday -- exactly the defect #50's
    # review predicted. A session exists when the database holds a bar for it and not otherwise.
    span = _consumed_span(session, aliases=aliases, start=source.earliest, end=end)
    unsearched = _sessions_after(
        session,
        prefixes=source.observation_prefixes,
        after=span[1] if span is not None else None,
        until=min(end, as_of),
    )
    return CapacityRow(
        source=source,
        aliases=aliases,
        status=status,
        trials=consumption.trials,
        consumers=consumption.consumers,
        by_family=consumption.by_family,
        unsearched_sessions=unsearched,
        reserved_by=reserved,
    )


def capacity_inventory(
    session: Session, *, as_of: date, sources: Sequence[DataSource] = DATA_SOURCES
) -> tuple[CapacityRow, ...]:
    """Every declared source, with what has been spent on it and what remains.

    The answer an operator needs before believing that a completed data-platform issue created
    research budget, and the answer the research director needs before spending compute on a
    search whose result could never be confirmed.
    """
    return tuple(capacity_row(session, source, as_of=as_of) for source in sources)


__all__ = [
    "DATA_SOURCES",
    "SERVED_CAPABILITIES",
    "CapacityRow",
    "CapacityStatus",
    "DataSource",
    "capacity_inventory",
    "capacity_row",
    "stored_sessions",
]
