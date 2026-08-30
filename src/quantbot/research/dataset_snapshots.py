"""Turn stored bars into a dataset snapshot, and resolve one back to its bytes (#48).

`reproduce.py` lists five capabilities #48 needs. The first is *resolve the dataset snapshots
named by the manifest back to their exact bytes*, and building only that would have produced a
verifier with nothing to verify.

**`DatasetSnapshot` was constructed nowhere in `src/`.** Every construction site in the
repository is a test. `builder.py` takes the snapshots as a parameter and no caller supplies
them, so the manifest machinery was complete and unfed: an experiment could record which
datasets it used only if something first described them, and nothing did.

So this is the producer and the verifier together, which is also the only order in which either
can be shown to work. A hash nothing computes the same way twice is not a content address, and
the way to demonstrate it is not a second implementation agreeing with the first, but a
round trip: snapshot stored bars, resolve the snapshot, change one number, watch it fail.

**What goes into the hash, and why each.**

Symbol, timestamp, open, high, low, close, volume **and adjustment**. The last is the one worth
arguing about. A corporate action re-adjusts historical prices, so the same rows under a later
adjustment factor are different numbers describing the same events -- which is precisely the
"silent revision" `DatasetSnapshot` names as its failure mode. Excluding it would let a
re-adjusted series resolve clean against a snapshot taken before the split.

Rows are sorted before hashing, so a different query plan or a changed index does not mint a new
identity for identical data. That is a property of the storage engine, not of the dataset.

Decimals are hashed as their stored strings rather than as floats. `Decimal("1.10")` and
`Decimal("1.1")` are equal and are *different data* -- one came back from a provider with a
trailing digit and the other did not, and a hash that cannot tell them apart cannot detect a
provider changing its formatting.

**Resolution refuses rather than approximates.** A snapshot naming rows that are no longer there
is not a partially reproducible dataset; it is a dataset that cannot be reproduced, and reporting
it as a near-match would put a number nobody can stand behind into a reproduction verdict.

**What this is not for, discovered while testing it.** `StorageRepository.save_bars` already
raises `StateConflictError` when a re-pull would change a stored bar, so in-place revision cannot
happen through the ordinary write path. The first version of these tests claimed to catch exactly
that, and was wrong: the storage layer catches it first, and a resolver justified by a failure
something else already prevents is a resolver justified by nothing.

Its real job is verification *across* stores rather than within one -- a manifest carried to
another machine, a store rebuilt from backup, an export and reimport, or a range whose rows were
never present. Those are the cases where a hash is the only thing between "the same data" and
"data with the same name", and `save_bars` says nothing about any of them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Literal

from sqlalchemy.orm import Session

from quantbot.domain import Bar
from quantbot.research.manifest import DatasetSnapshot
from quantbot.storage import StorageRepository

#: Bumped when the hashed representation changes. A snapshot taken under one version cannot be
#: compared against a hash computed under another, and pretending otherwise would report every
#: historical snapshot as revised the day this changes.
HASH_VERSION = "bars-ohlcva-v1"

DataRole = Literal["DISCOVERY", "VALIDATION", "PROTECTED_EVALUATION", "FORWARD_PAPER"]


class Resolution(StrEnum):
    """What happened when a snapshot was resolved back against the store."""

    #: The store holds exactly the rows the snapshot describes.
    EXACT = "EXACT"
    #: The rows are there and their content differs. A revision, and the reason `adjustment` is
    #: in the hash.
    CONTENT_CHANGED = "CONTENT_CHANGED"
    #: A different number of rows. Counted separately from a content change because "the data
    #: moved" and "some of the data is gone" call for different responses.
    ROW_COUNT_CHANGED = "ROW_COUNT_CHANGED"
    #: Nothing at all in that range. A store that never held it, or one that was rebuilt.
    ABSENT = "ABSENT"


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """Whether a snapshot still names what it named, and what changed if not."""

    snapshot: DatasetSnapshot
    resolution: Resolution
    observed_hash: str | None
    observed_observations: int

    @property
    def reproduced(self) -> bool:
        return self.resolution is Resolution.EXACT

    def explain(self) -> str:
        if self.reproduced:
            return (
                f"{self.snapshot.dataset}@{self.snapshot.snapshot}: EXACT "
                f"({self.observed_observations} observations)"
            )
        return (
            f"{self.snapshot.dataset}@{self.snapshot.snapshot}: "
            f"{self.resolution.value} -- recorded {self.snapshot.observations} observations "
            f"hashing {self.snapshot.content_hash[:12]}, store holds "
            f"{self.observed_observations} hashing "
            f"{'nothing' if self.observed_hash is None else self.observed_hash[:12]}"
        )


def bar_content_hash(bars: Sequence[Bar]) -> str:
    """Content address of a set of bars.

    Sorted, so a different query plan does not mint a new identity for identical data. Includes
    `adjustment`, so a re-adjusted series does not resolve clean against a snapshot taken before
    the corporate action.

    Note that `save_bars` refuses an in-place revision, so this is not what catches a local
    re-pull. It is what catches a re-adjusted *export*, or a store rebuilt from one.
    """
    digest = hashlib.sha256()
    digest.update(HASH_VERSION.encode("utf-8"))
    for bar in sorted(bars, key=lambda item: (item.timestamp, item.symbol)):
        digest.update(
            "|".join(
                (
                    bar.symbol,
                    bar.timestamp.isoformat(),
                    # The stored strings, not floats. `1.10` and `1.1` are equal and are
                    # different data, and a hash that cannot tell them apart cannot detect a
                    # provider changing its formatting.
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    str(bar.adjustment),
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()


def _bars_in_range(
    session: Session, *, dataset: str, provider: str, start: date, end: date
) -> list[Bar]:
    """The stored bars a snapshot describes.

    Filtered in Python rather than SQL because `list_bars` is the repository's only bar reader
    and adding a date-ranged variant for one caller would put a second bar query in the codebase
    for a filter that costs nothing at this size. If bar volume ever makes that false, the fix is
    a repository method, not a query written here.
    """
    return [
        bar
        for bar in StorageRepository(session).list_bars(symbol=dataset, provider=provider)
        if start <= bar.timestamp.date() <= end
    ]


def snapshot_bars(
    session: Session,
    *,
    dataset: str,
    provider: str,
    start: date,
    end: date,
    role: DataRole,
    snapshot: str,
    retrieved_at: datetime | None = None,
    earliest_available_at: datetime | None = None,
) -> DatasetSnapshot:
    """Describe the stored bars for one symbol and range as a dataset vintage.

    Refuses an empty range rather than returning a snapshot of nothing. A vintage describing zero
    observations would hash identically for every symbol and every range, so it would be a
    content address that addresses nothing -- and it would resolve clean forever.
    """
    bars = _bars_in_range(session, dataset=dataset, provider=provider, start=start, end=end)
    if not bars:
        raise ValueError(
            f"no stored bars for {dataset} from {provider} between {start} and {end}; a "
            "snapshot of nothing hashes the same for every dataset and would resolve clean "
            "forever"
        )
    return DatasetSnapshot(
        dataset=dataset,
        snapshot=snapshot,
        role=role,
        start=start,
        end=end,
        content_hash=bar_content_hash(bars),
        observations=len(bars),
        provider=provider,
        retrieved_at=retrieved_at,
        earliest_available_at=earliest_available_at,
        transformation_version=HASH_VERSION,
    )


def resolve_snapshot(session: Session, snapshot: DatasetSnapshot) -> ResolutionResult:
    """Re-read what a snapshot names and check it is still exactly that.

    The first of #48's five capabilities. It does not rerun an experiment; it answers whether the
    data an experiment claims to have used is still the data in the store.
    """
    bars = _bars_in_range(
        session,
        dataset=snapshot.dataset,
        provider=snapshot.provider,
        start=snapshot.start,
        end=snapshot.end,
    )
    if not bars:
        return ResolutionResult(
            snapshot=snapshot,
            resolution=Resolution.ABSENT,
            observed_hash=None,
            observed_observations=0,
        )

    observed = bar_content_hash(bars)
    if observed == snapshot.content_hash:
        return ResolutionResult(
            snapshot=snapshot,
            resolution=Resolution.EXACT,
            observed_hash=observed,
            observed_observations=len(bars),
        )
    return ResolutionResult(
        snapshot=snapshot,
        resolution=(
            Resolution.ROW_COUNT_CHANGED
            if len(bars) != snapshot.observations
            else Resolution.CONTENT_CHANGED
        ),
        observed_hash=observed,
        observed_observations=len(bars),
    )


__all__ = [
    "HASH_VERSION",
    "Resolution",
    "ResolutionResult",
    "bar_content_hash",
    "resolve_snapshot",
    "snapshot_bars",
]
