"""Durable broad news research; observational context has no order/model authority.

No article URL is followed, no provider text is executed or sent to a model. Tags
describe reported topics, not sentiment, predictions or validated causal effects.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from quantbot.anomaly.news import NEWS_URL, _parse_timestamp
from quantbot.research.manifest import canonical_result_json, content_id

if TYPE_CHECKING:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.discovery import DiscoveryStore

TOPICS = {
    "EARNINGS": ("earnings", "revenue", "guidance", "quarterly results"),
    "FINANCING": ("offering", "financing", "debt", "buyback", "dividend"),
    "CORPORATE_ACTION": ("merger", "acquisition", "stock split", "spin-off"),
    "LEGAL_REGULATORY": ("lawsuit", "litigation", "regulator", "fda", "investigation"),
    "OPERATIONS": ("recall", "production", "contract", "layoff", "bankruptcy"),
}


def _initialize(state: DiscoveryStore) -> None:
    with state.connection() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS research_documents(
                id TEXT PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS research_current(
                source_id TEXT PRIMARY KEY,document_id TEXT);
            CREATE TABLE IF NOT EXISTS research_requests(symbol TEXT PRIMARY KEY,requested_at TEXT);
            CREATE TABLE IF NOT EXISTS research_symbols(
                symbol TEXT,document_id TEXT,source_id TEXT,received_at TEXT,published_at TEXT,
                kind TEXT,PRIMARY KEY(symbol,document_id));
        """)
        if "kind" not in {row[1] for row in c.execute("pragma table_info(research_symbols)")}:
            c.execute("alter table research_symbols add column kind TEXT")
            c.execute(
                "update research_symbols set kind=(select json_extract(d.payload,'$.kind') "
                "from research_documents d where d.id=research_symbols.document_id)"
            )
        c.execute(
            "create index if not exists research_symbol_kind_window "
            "on research_symbols(symbol,kind,published_at,received_at)"
        )


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    # Stored as inert text, never rendered as trusted HTML or interpreted as instructions.
    return re.sub(r"<[^>]*>", " ", value)[:limit].strip()


def _valid_uri(value: object) -> bool:
    try:
        return (
            isinstance(value, str)
            and urlsplit(value).scheme == "https"
            and bool(urlsplit(value).hostname)
        )
    except ValueError:
        return False


def record_news_page(
    state: DiscoveryStore,
    payload: object,
    *,
    start: datetime,
    end: datetime,
    received: datetime,
) -> dict[str, Any]:
    if (
        not isinstance(payload, dict)
        or not isinstance(payload.get("news"), list)
        or any(t.tzinfo is None for t in (start, end, received))
        or not start <= end <= received
    ):
        raise ValueError("NEWS_PAGE_INVALID")
    token = payload.get("next_page_token")
    if token is not None and (not isinstance(token, str) or not token or len(token) > 4096):
        raise ValueError("NEWS_PAGE_TOKEN_INVALID")
    _initialize(state)
    accepted = rejected = 0
    with state.connection() as c:
        for item in payload["news"]:
            if not isinstance(item, dict):
                rejected += 1
                continue
            published = _parse_timestamp(item.get("created_at"))
            updated = _parse_timestamp(item.get("updated_at"))
            title = _text(item.get("headline"), 2000)
            symbols = item.get("symbols")
            uri = item.get("url")
            if (
                published is None
                or updated is None
                or not start <= published <= end
                or not published <= updated <= end
                or not title
                or not isinstance(item.get("id"), (str, int))
                or isinstance(item.get("id"), bool)
                or not isinstance(symbols, list)
                or any(not isinstance(s, str) for s in symbols)
                or not _valid_uri(uri)
            ):
                rejected += 1
                continue
            summary = _text(item.get("summary"), 4000)
            excerpt = _text(item.get("content"), 16000)
            text = (title + " " + summary + " " + excerpt).lower()
            document = dict(
                kind="NEWS_RESEARCH_V1",
                source_id="alpaca-news:" + str(item["id"]),
                uri=uri,
                publisher=_text(item.get("source"), 200),
                title=title,
                summary=summary,
                excerpt=excerpt,
                symbols=symbols,
                published_at=published.isoformat(),
                updated_at=updated.isoformat(),
                retrieved_at=received.isoformat(),
                query_end=end.isoformat(),
                parsed_item_sha256=hashlib.sha256(canonical_result_json(item).encode()).hexdigest(),
                parser="broad-news-research-v1",
                epistemic_status="UNTESTED",
                order_authority=False,
                event_tags=sorted(
                    tag for tag, terms in TOPICS.items() if any(term in text for term in terms)
                ),
            )
            # Deduplicate a repeated vintage without backdating a later revision.
            old = c.execute(
                "select d.id,d.payload from research_current p join research_documents d "
                "on p.document_id=d.id where p.source_id=?",
                (document["source_id"],),
            ).fetchone()
            if (
                old
                and json.loads(old[1]).get("parsed_item_sha256") == document["parsed_item_sha256"]
            ):
                identity = old[0]
            else:
                identity = content_id(document)
                c.execute(
                    "insert or ignore into research_documents values(?,?)",
                    (identity, canonical_result_json(document)),
                )
            c.execute(
                "insert or replace into research_current values(?,?)",
                (document["source_id"], identity),
            )
            for symbol in symbols:
                c.execute(
                    "insert or ignore into research_symbols values(?,?,?,?,?,?)",
                    (
                        symbol,
                        identity,
                        document["source_id"],
                        document["retrieved_at"],
                        document["published_at"],
                        document["kind"],
                    ),
                )
            accepted += 1
        receipt = dict(
            kind="NEWS_RESEARCH_PAGE_V1",
            start=start.isoformat(),
            end=end.isoformat(),
            retrieved_at=received.isoformat(),
            accepted=accepted,
            rejected=rejected,
            next_page_token=token,
            provider_coverage="ALPACA_NEWS_ONLY_PARTIAL",
            order_authority=False,
        )
        receipt["id"] = state._receipt(c, receipt)
        state._set(c, "research_last_page", receipt)
    return receipt


def research_context(
    state: DiscoveryStore,
    symbols: tuple[str, ...],
    *,
    now: datetime,
) -> tuple[tuple[tuple[str, str], ...], tuple[str, ...]]:
    _initialize(state)
    refs: list[tuple[str, str]] = []
    gaps = ["RESEARCH_UNVALIDATED_CONTEXT", "ALPACA_NEWS_PARTIAL_PROVIDER_COVERAGE"]
    with state.connection() as c:
        last = state._get(c, "research_last_page")
        entitlement = state._get(c, "news_cost_status")
        if entitlement:
            gaps.append(entitlement)
        if (
            not last
            or not 0 <= (now - datetime.fromisoformat(last["retrieved_at"])).total_seconds() <= 180
        ):
            gaps.append("NEWS_RESEARCH_STALE_OR_MISSING")
        if last and last.get("next_page_token"):
            gaps.append("NEWS_WINDOW_PAGINATION_INCOMPLETE")
        if last and now - datetime.fromisoformat(last["end"]) > timedelta(minutes=3):
            gaps.append("NEWS_QUERY_END_STALE")
        gaps.append(state._get(c, "filing_research_status", "SEC_CONTACT_NOT_CONFIGURED"))
        if state._get(c, "sec_company_failures", {}):
            gaps.append("SEC_COMPANY_FAILURES_PRESENT")
        if state._get(c, "sec_coverage", {}).get("unmapped_symbols", 0):
            gaps.append("SEC_UNMAPPED_OR_NON_COMPANY_ASSETS")
        for symbol in symbols:
            as_of: dict[str, tuple[str, dict[str, Any]]] = {}
            rows: list[tuple[str, str]] = []
            for kind, window in (
                ("NEWS_RESEARCH_V1", timedelta(hours=48)),
                ("SEC_COMPANY_RESEARCH_V1", timedelta(days=365)),
            ):
                rows.extend(
                    c.execute(
                        "select d.id,d.payload from research_symbols s join research_documents d "
                        "on s.document_id=d.id where s.symbol=? and s.kind=? and s.published_at>=? "
                        "and s.published_at<=? and s.received_at<=? order by s.received_at,d.rowid",
                        (
                            symbol,
                            kind,
                            (now - window).isoformat(),
                            now.isoformat(),
                            now.isoformat(),
                        ),
                    )
                )
            for identity, raw in rows:
                doc = json.loads(raw)
                published = datetime.fromisoformat(doc["published_at"] or doc["observed_at"])
                window = (
                    timedelta(hours=48)
                    if doc["kind"] == "NEWS_RESEARCH_V1"
                    else timedelta(days=365)
                )
                if now - published > window:
                    continue
                previous = as_of.get(doc["source_id"])
                if previous and (previous[1].get("updated_at") or "") > (
                    doc.get("updated_at") or ""
                ):
                    continue
                as_of[doc["source_id"]] = (identity, doc)
            refs.extend((symbol, identity) for identity, _ in as_of.values())
            current_sec = [d for _, d in as_of.values() if d["kind"] == "SEC_COMPANY_RESEARCH_V1"]
            if not current_sec or any(
                now - datetime.fromisoformat(d["retrieved_at"]) > timedelta(minutes=15)
                for d in current_sec
            ):
                with suppress(sqlite3.Error):
                    c.execute(
                        "insert or ignore into research_requests values(?,?)",
                        (symbol, now.isoformat()),
                    )
                gaps.append("SEC_SELECTED_SYMBOL_REFRESH_PENDING:" + symbol)
    return tuple(refs), tuple(gaps)


async def research_once(broker: AlpacaCoreBroker, state: DiscoveryStore) -> None:
    """One budgeted page per minute, durable pagination; no ticker filter or total cap."""
    now = broker.now
    with state.connection() as c:
        if os.environ.get("QUANTBOT_FREE_NEWS_CONFIRMED") != "1":
            state._set(c, "news_cost_status", "NEWS_DISABLED_FREE_ENTITLEMENT_UNVERIFIED")
            return
        state._set(c, "news_cost_status", "NEWS_EXISTING_FREE_ENTITLEMENT_CONFIRMED")
        last = state._get(c, "research_attempt_at")
        if last and (now - datetime.fromisoformat(last)).total_seconds() < 60:
            return
        state._set(c, "research_attempt_at", now.isoformat())
        completed = state._get(c, "research_news_completed")
        job = state._get(c, "research_news_job") or dict(
            start=(
                max(
                    now - timedelta(hours=48),
                    datetime.fromisoformat(completed) - timedelta(hours=2),
                )
                if completed
                else now - timedelta(hours=48)
            ).isoformat(),
            end=now.isoformat(),
            token=None,
            seen=[],
        )
        state._set(c, "research_news_job", job)
    params = dict(
        start=job["start"], end=job["end"], limit="50", sort="asc", include_content="true"
    )
    if job["token"]:
        params["page_token"] = job["token"]
    try:
        response = await broker.market._request_page(params, endpoint=NEWS_URL)
        if len(response.content) > 8_000_000:
            raise ValueError("NEWS_RESPONSE_TOO_LARGE")
        payload = json.loads(response.content)
        token = payload.get("next_page_token") if isinstance(payload, dict) else None
        if token and token in [*job.get("seen", []), job["token"]]:
            raise ValueError("NEWS_PAGINATION_NO_PROGRESS")
        receipt = record_news_page(
            state,
            payload,
            start=datetime.fromisoformat(job["start"]),
            end=datetime.fromisoformat(job["end"]),
            received=broker.now,
        )
        with state.connection() as c:
            state._receipt(
                c,
                dict(
                    kind="NEWS_RAW_RESPONSE_V1",
                    page_receipt_id=receipt["id"],
                    raw_response_sha256=hashlib.sha256(response.content).hexdigest(),
                    retrieved_at=broker.now.isoformat(),
                ),
            )
        token = receipt["next_page_token"]
        if token and token == job["token"]:
            raise ValueError("NEWS_PAGINATION_NO_PROGRESS")
        with state.connection() as c:
            state._set(
                c,
                "research_news_job",
                {
                    **job,
                    "token": token,
                    "seen": [*job.get("seen", []), job["token"]],
                }
                if token
                else None,
            )
            if not token:
                state._set(c, "research_news_completed", job["end"])
    except Exception as error:
        # No cursor advance and no healthy/empty claim on rate, entitlement or parsing failure.
        with state.connection() as c:
            state._receipt(
                c,
                dict(
                    kind="NEWS_RESEARCH_FAILURE_V1",
                    at=broker.now.isoformat(),
                    error=type(error).__name__,
                ),
            )


async def run_research(broker: Any, state: DiscoveryStore) -> None:
    """Observational worker only; provider failures cannot stop trading or stock discovery."""
    from quantbot.paper_core.discovery_filings import filings_once

    async def news_attempt() -> None:
        await research_once(broker, state)

    async def sec_attempt() -> None:
        await filings_once(state, broker.now)

    while True:
        for source, attempt in (
            ("NEWS", news_attempt),
            ("SEC", sec_attempt),
        ):
            try:
                await attempt()
            except Exception as error:
                print(
                    json.dumps(
                        dict(
                            kind="RESEARCH_LOOP_FAILURE_V1",
                            source=source,
                            at=broker.now.isoformat(),
                            error=type(error).__name__,
                        )
                    ),
                    flush=True,
                )
                with suppress(Exception), state.connection() as c:
                    state._receipt(
                        c,
                        dict(
                            kind="RESEARCH_LOOP_FAILURE_V1",
                            source=source,
                            at=broker.now.isoformat(),
                            error=type(error).__name__,
                        ),
                    )
        await asyncio.sleep(1)
