"""Ask a news provider what was public before an anomaly, and report honestly when it could not.

`catalyst.py` decides which of the three states an anomaly is in. This is the half that actually
consults a source, and almost all of its substance is in the failure paths -- because the whole
value of the three-state design is destroyed by an adapter that reports an outage as an empty
result. `NO_PUBLIC_CATALYST_FOUND` is a finding about the world; a source that could not answer
must produce `SourceHealth.UNAVAILABLE` and nothing else, so the assessment lands on
`CATALYST_UNKNOWN` instead.

**The query is bounded at the observation.** Alpaca will happily return items published after any
timestamp asked for, and an adapter that fetched a whole day and filtered afterwards would be
one refactor away from look-ahead. `assess_catalysts` filters too -- that is the tested guard --
but the request itself never asks for the future, so the mistake has to be made twice.

**A missing publication time is not "recent".** Alpaca supplies `created_at` on every item today;
an item without one is passed through with `published_at=None`, which makes the whole assessment
`CATALYST_UNKNOWN` rather than silently dropping the item or dating it to the retrieval.

Verified against the live endpoint 2026-08-30: the paper account **is** entitled to
`/v1beta1/news`, which was previously assumed unavailable. Items carry `created_at`, `headline`,
`source`, `id`, `url` and the symbols they were tagged with.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from pydantic import SecretStr

from quantbot.anomaly.catalyst import CatalystCandidate, SourceHealth, SourceResult

#: The provider name recorded on every result, so a stored assessment says who was asked.
SOURCE_NAME = "alpaca-news"

NEWS_URL = "https://data.alpaca.markets/v1beta1/news"

#: Alpaca returns at most 50 per page. One page is deliberate: this asks "was there a public
#: reason", not "enumerate the coverage". Paging would multiply provider load for a question
#: whose answer does not change after the first qualifying item.
PAGE_LIMIT = 50


class NewsTransport(Protocol):
    """The same injected-transport shape `market_data` uses, for the same testing reason."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        timeout_seconds: Decimal,
    ) -> Any: ...


def _parse_timestamp(value: object) -> datetime | None:
    """Alpaca's `created_at`, or None if it cannot be placed in time.

    None is a real answer here and propagates to `CATALYST_UNKNOWN`. Substituting the retrieval
    time would date an item to when Elrond looked rather than when the world learned it, which is
    precisely the collapse `CatalystCandidate` keeps two fields apart to prevent.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def candidates_from_payload(
    payload: Mapping[str, object], *, retrieved_at: datetime
) -> tuple[CatalystCandidate, ...]:
    """Map a decoded Alpaca news response onto candidates.

    Separate from the fetch so the mapping is testable without a transport, and so a provider
    response that changes shape fails here rather than three layers away.
    """
    items = payload.get("news")
    if not isinstance(items, list):
        return ()
    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        identifier = item.get("id")
        headline = item.get("headline")
        if identifier is None or not isinstance(headline, str) or not headline.strip():
            # No stable id or no headline: nothing that can be cited later, so it is not a
            # candidate. Counted by its absence rather than invented.
            continue
        candidates.append(
            CatalystCandidate(
                source=str(item.get("source") or SOURCE_NAME),
                external_id=str(identifier),
                published_at=_parse_timestamp(item.get("created_at")),
                retrieved_at=retrieved_at,
                headline=headline.strip()[:2000],
                url=str(item["url"]) if isinstance(item.get("url"), str) else None,
            )
        )
    return tuple(candidates)


class AlpacaNewsSource:
    """One catalyst source, reporting its own health rather than its emptiness."""

    name = SOURCE_NAME

    def __init__(
        self,
        transport: NewsTransport,
        *,
        api_key: SecretStr | str,
        api_secret: SecretStr | str,
        timeout_seconds: Decimal = Decimal("10"),
    ) -> None:
        """Credentials stay wrapped until the header is built.

        `SecretStr` is what the settings hold and what the rest of this codebase passes around;
        unwrapping at the boundary rather than on the way in means a stray `repr` of this object
        during debugging cannot print the key.
        """
        self._transport = transport
        self._key = api_key if isinstance(api_key, SecretStr) else SecretStr(api_key)
        self._secret = (
            api_secret if isinstance(api_secret, SecretStr) else SecretStr(api_secret)
        )
        self._timeout = timeout_seconds

    async def fetch(
        self,
        symbols: Sequence[str],
        *,
        window_start: datetime,
        observed_at: datetime,
        retrieved_at: datetime,
    ) -> SourceResult:
        """Consult the provider for items published in the window, or say why it could not.

        Returns a `SourceResult` and never raises for a provider failure. The caller is recording
        research context beside a trading cycle, and an enrichment lookup must not be able to
        stop one -- but the failure is carried in the result rather than swallowed, so the
        assessment lands on `CATALYST_UNKNOWN` and never on a negative finding.
        """
        if not symbols:
            return SourceResult(
                source=self.name,
                health=SourceHealth.UNAVAILABLE,
                detail="no symbols were requested, so nothing was asked of the provider",
            )
        params = {
            "symbols": ",".join(sorted(symbols)),
            "start": window_start.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            # Bounded at the observation, so the request itself cannot ask for the future.
            "end": observed_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            "limit": str(PAGE_LIMIT),
            "sort": "desc",
        }
        try:
            response = await self._transport.request(
                "GET",
                NEWS_URL,
                headers={
                    "APCA-API-KEY-ID": self._key.get_secret_value(),
                    "APCA-API-SECRET-KEY": self._secret.get_secret_value(),
                },
                params=params,
                timeout_seconds=self._timeout,
            )
        except Exception as error:  # noqa: BLE001 -- a transport failure is a health state
            return SourceResult(
                source=self.name,
                health=SourceHealth.TIMEOUT,
                detail=f"the provider could not be reached: {error!r}"[:500],
            )

        status = getattr(response, "status_code", 0)
        if status == 429:
            # The state this whole design exists for. Rate limits cluster when markets are busy,
            # which is exactly when anomalies fire, so reading one as "no news" would manufacture
            # discoveries precisely when the provider was struggling.
            return SourceResult(
                source=self.name,
                health=SourceHealth.RATE_LIMITED,
                detail="the provider rate-limited this lookup (HTTP 429)",
            )
        if status != 200:
            return SourceResult(
                source=self.name,
                health=SourceHealth.UNAVAILABLE,
                detail=f"the provider answered HTTP {status}",
            )

        try:
            payload = json.loads(getattr(response, "content", b"") or b"{}")
        except ValueError as error:
            return SourceResult(
                source=self.name,
                health=SourceHealth.UNAVAILABLE,
                detail=f"the provider returned an unreadable body: {error}"[:500],
            )
        if not isinstance(payload, dict):
            return SourceResult(
                source=self.name,
                health=SourceHealth.UNAVAILABLE,
                detail="the provider returned a body that was not an object",
            )

        return SourceResult(
            source=self.name,
            health=SourceHealth.HEALTHY,
            candidates=candidates_from_payload(payload, retrieved_at=retrieved_at),
        )


__all__ = [
    "NEWS_URL",
    "PAGE_LIMIT",
    "SOURCE_NAME",
    "AlpacaNewsSource",
    "NewsTransport",
    "candidates_from_payload",
]
