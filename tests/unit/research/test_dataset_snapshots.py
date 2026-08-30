"""Producing a dataset snapshot and resolving it back to its bytes (#48).

The producer and verifier are tested together because that is the only way either can be shown
to work. A hash nothing computes the same way twice is not a content address, and the
demonstration is a round trip -- snapshot, resolve, change one number, watch it fail -- not a
second implementation agreeing with the first.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from quantbot.domain import Bar
from quantbot.research.dataset_snapshots import (
    Resolution,
    bar_content_hash,
    resolve_snapshot,
    snapshot_bars,
)
from quantbot.storage import Database, StorageRepository
from quantbot.storage.schema import bars

PROVIDER = "alpaca:iex:all:1day"
START = date(2026, 8, 3)
END = date(2026, 8, 7)


@pytest.fixture
def database(tmp_path):  # type: ignore[no-untyped-def]
    with Database.create(tmp_path / "snapshots.db") as store:
        yield store


def bar(day: int, *, close: str = "100", adjustment: str = "1", symbol: str = "AAPL") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=datetime(2026, 8, day, 20, 0, tzinfo=UTC),
        open="99",
        high="101",
        low="98",
        close=close,
        volume="1000",
        adjustment=adjustment,
    )


def store(database, bars) -> None:  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        StorageRepository(session).save_bars(bars, provider=PROVIDER)


def take(database, **kwargs):  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        return snapshot_bars(
            session,
            dataset=kwargs.pop("dataset", "AAPL"),
            provider=PROVIDER,
            start=kwargs.pop("start", START),
            end=kwargs.pop("end", END),
            role=kwargs.pop("role", "DISCOVERY"),
            snapshot=kwargs.pop("snapshot", "vintage-1"),
            **kwargs,
        )


def resolve(database, snapshot):  # type: ignore[no-untyped-def]
    with database.transaction() as session:
        return resolve_snapshot(session, snapshot)


def test_a_snapshot_resolves_against_the_store_it_came_from(database) -> None:  # type: ignore[no-untyped-def]
    """The round trip. Without this passing, nothing else here means anything."""
    store(database, [bar(3), bar(4), bar(5)])
    snapshot = take(database)

    result = resolve(database, snapshot)

    assert result.reproduced
    assert result.resolution is Resolution.EXACT
    assert result.observed_observations == 3
    assert "EXACT" in result.explain()


def test_the_write_path_already_refuses_an_in_place_revision(database) -> None:  # type: ignore[no-untyped-def]
    """Recorded because it corrects what these tests originally claimed.

    The first version asserted that a changed price stops a snapshot resolving, through the
    ordinary write path -- and that write never happens. `save_bars` raises `StateConflictError`
    rather than revising a stored bar, so the "silent revision" this module was first described
    as catching is caught one layer down.

    Asserting it here keeps the two responsibilities straight: storage prevents revision *within*
    a store, and the content hash detects difference *between* stores. A resolver justified by a
    failure something else already prevents is justified by nothing.
    """
    from quantbot.storage.repositories import StateConflictError

    store(database, [bar(3)])

    with pytest.raises(StateConflictError, match="adjustment"):
        store(database, [bar(3, adjustment="0.5")])


def test_a_store_rebuilt_with_different_bytes_does_not_resolve(database) -> None:  # type: ignore[no-untyped-def]
    """The case the resolver is actually for.

    Written through raw SQL rather than the repository, deliberately: this simulates the store
    the resolver exists to catch -- one rebuilt from backup, restored, exported and reimported,
    or carried to another machine. `save_bars` guards none of those, because none of them go
    through it.
    """
    store(database, [bar(3), bar(4), bar(5)])
    snapshot = take(database)

    with database.transaction() as session:
        session.execute(
            bars.update()
            .where(bars.c.symbol == "AAPL")
            .where(bars.c.timestamp == "2026-08-04T20:00:00Z")
            .values(close="99.5")
        )

    result = resolve(database, snapshot)

    assert not result.reproduced
    assert result.resolution is Resolution.CONTENT_CHANGED
    assert result.observed_observations == 3, "the rows are all there; their content moved"


def test_a_re_adjusted_series_does_not_resolve_clean() -> None:
    """`adjustment` is in the hash, so a post-split series is not a pre-split one.

    A corporate action re-adjusts historical prices: the same rows under a later factor are
    different numbers describing the same events. Left out of the hash, a re-adjusted export
    would resolve clean against a snapshot taken before the split, and an experiment would be
    judged reproducible against data it never saw.

    Asserted on the hash rather than through storage, because `save_bars` refuses the in-place
    version and this property belongs to the hash regardless of who writes.
    """
    before = [bar(3), bar(4), bar(5)]
    after = [bar(day, adjustment="0.5") for day in (3, 4, 5)]

    assert bar_content_hash(before) != bar_content_hash(after)


def test_a_narrower_range_is_a_row_count_change(database) -> None:  # type: ignore[no-untyped-def]
    """A snapshot whose rows are partly gone is not partly reproducible."""
    store(database, [bar(3), bar(4), bar(5)])
    snapshot = take(database)

    narrowed = snapshot.model_copy(update={"end": date(2026, 8, 4)})
    result = resolve(database, narrowed)

    assert not result.reproduced
    assert result.resolution is Resolution.ROW_COUNT_CHANGED
    assert result.observed_observations == 2


def test_an_empty_store_is_absent_rather_than_changed(database) -> None:  # type: ignore[no-untyped-def]
    """A store that holds none of it was not revised; it does not have the data.

    Reporting that as a content change would send a reader looking for a revision that never
    happened.
    """
    store(database, [bar(3)])
    snapshot = take(database)

    empty = snapshot.model_copy(update={"start": date(2027, 1, 1), "end": date(2027, 1, 5)})
    result = resolve(database, empty)

    assert result.resolution is Resolution.ABSENT
    assert result.observed_hash is None
    assert result.observed_observations == 0


def test_snapshotting_nothing_is_refused(database) -> None:  # type: ignore[no-untyped-def]
    """A vintage of zero observations hashes the same for every dataset and every range.

    That is a content address addressing nothing, and it would resolve clean forever -- the most
    dangerous possible outcome for a reproducibility check.
    """
    with pytest.raises(ValueError, match="snapshot of nothing"):
        take(database)


def test_row_order_does_not_change_the_hash() -> None:
    """A different query plan is a property of the storage engine, not of the dataset."""
    forward = [bar(3), bar(4), bar(5)]
    shuffled = [bar(5), bar(3), bar(4)]

    assert bar_content_hash(forward) == bar_content_hash(shuffled)


def test_trailing_zeros_are_different_data() -> None:
    """`1.10` and `1.1` are equal and are different data.

    One came back from a provider with a trailing digit and the other did not. A hash that
    cannot tell them apart cannot detect a provider changing its formatting, which is a silent
    revision of exactly the kind this is for.
    """
    assert bar_content_hash([bar(3, close="100.10")]) != bar_content_hash(
        [bar(3, close="100.1")]
    )


def test_a_different_symbol_hashes_differently() -> None:
    """Otherwise one symbol's snapshot would resolve against another's rows."""
    assert bar_content_hash([bar(3)]) != bar_content_hash([bar(3, symbol="MSFT")])


def test_the_snapshot_records_what_it_hashed(database) -> None:  # type: ignore[no-untyped-def]
    """Observation count and lineage travel with the hash, or a mismatch cannot be explained."""
    store(database, [bar(3), bar(4)])
    retrieved = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)

    snapshot = take(
        database,
        retrieved_at=retrieved,
        earliest_available_at=retrieved - timedelta(days=1),
    )

    assert snapshot.observations == 2
    assert snapshot.provider == PROVIDER
    assert snapshot.retrieved_at == retrieved
    assert snapshot.transformation_version == "bars-ohlcva-v1"
    assert snapshot.content_hash == bar_content_hash([bar(3), bar(4)])
