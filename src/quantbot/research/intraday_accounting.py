"""Permanent pre-engine trial charges for frozen intraday research attempts.

Commit the journal transaction before running the engine. A failure, abandoned run, or
negative result never removes its charge. One frozen rule and its prespecified cost stress
case are one candidate; choosing among alternative rules requires additional attempts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from quantbot.research.datasets import observation_identity
from quantbot.storage.database import decode_utc, encode_utc
from quantbot.storage.schema import search_runs

INTRADAY_SEARCH_NAMESPACE = "intraday:"
INTRADAY_ACTOR = "intraday-research"


def _identity(
    search_id: str, configuration_hash: str, dataset: str, start: date, end: date
) -> dict[str, object]:
    if not re.fullmatch(r"intraday:[A-Za-z0-9][A-Za-z0-9_.:-]*", search_id):
        raise ValueError("intraday search_id must use the reserved intraday: namespace")
    if len(search_id) > 128:
        raise ValueError("intraday search_id exceeds 128 characters")
    if not re.fullmatch(r"[0-9a-f]{64}", configuration_hash):
        raise ValueError("configuration_hash must be a lowercase SHA-256 digest")
    if observation_identity(dataset) != "us-equities-daily":
        raise ValueError("intraday attempts retain the canonical us-equities-daily burden")
    if type(start) is not date or type(end) is not date or end < start:
        raise ValueError("intraday attempt requires an ordered date range")
    return {
        "search_id": search_id,
        "actor": INTRADAY_ACTOR,
        "actor_version": "1",
        "candidates_evaluated": 1,
        "dataset": "us-equities-daily",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "data_role": "DISCOVERY",
        "configuration_hash": configuration_hash,
    }


def _detail(identity: Mapping[str, object]) -> dict[str, object]:
    return dict(identity) | {"phase": "PRE_ENGINE"}


def _verify_row(row: RowMapping) -> None:
    """One authority for retries and burden reads; contradictory metadata is never repaired."""
    identity = _identity(
        str(row["search_id"]),
        str(row["configuration_hash"]),
        str(row["dataset"]),
        date.fromisoformat(str(row["start_date"])),
        date.fromisoformat(str(row["end_date"])),
    )
    if any(row[key] != value for key, value in identity.items()):
        raise ValueError("intraday attempt metadata contradicts its reserved identity")
    if json.loads(str(row["detail_json"])) != _detail(identity):
        raise ValueError("intraday attempt does not hold matching PRE_ENGINE metadata")
    started = decode_utc(str(row["started_at"]))
    finished = decode_utc(str(row["finished_at"]))
    if started != finished:
        raise ValueError("intraday PRE_ENGINE timestamps must identify a single freeze instant")


def record_intraday_attempt(
    session: Session,
    *,
    search_id: str,
    configuration_hash: str,
    dataset: str = "us-equities-daily",
    start: date,
    end: date,
    now: datetime,
) -> str:
    """Append one trial before measurement; caller must commit before invoking the engine.

    Retry with identical immutable inputs returns the original search id without mutation.
    `now` is the insertion time, so a later retry preserves the first freeze timestamp.
    Hash the complete frozen rule, data identity, and prespecified stress case in the caller.
    """
    identity = _identity(search_id, configuration_hash, dataset, start, end)
    timestamp = encode_utc(now)
    existing = (
        session.execute(select(search_runs).where(search_runs.c.search_id == search_id))
        .mappings()
        .one_or_none()
    )
    if existing is not None:
        _verify_row(existing)
        if any(existing[key] != value for key, value in identity.items()):
            raise ValueError(f"intraday attempt {search_id!r} conflicts with its frozen inputs")
        return search_id
    session.execute(
        search_runs.insert().values(
            **identity,
            started_at=timestamp,
            finished_at=timestamp,
            detail_json=json.dumps(_detail(identity), sort_keys=True, separators=(",", ":")),
        )
    )
    return search_id


def verified_intraday_attempt_ids(session: Session) -> frozenset[str]:
    """Return permanently charged ids, refusing any malformed reserved-namespace row."""
    rows = session.execute(
        select(search_runs).where(search_runs.c.search_id.like(f"{INTRADAY_SEARCH_NAMESPACE}%"))
    ).mappings()
    ids: set[str] = set()
    for row in rows:
        try:
            _verify_row(row)
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(
                f"intraday attempt {row['search_id']!r} is malformed: {error}"
            ) from error
        ids.add(str(row["search_id"]))
    return frozenset(ids)
