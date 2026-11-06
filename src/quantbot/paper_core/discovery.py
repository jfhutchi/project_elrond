"""Durable rolling broker coverage and preparation; never broker order authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import SerializerFunctionWrapHandler, model_serializer

from quantbot.brokers.base import BrokerModel
from quantbot.market_data.base import MarketDataRequestError
from quantbot.paper_core.entry_policy import supported_symbol
from quantbot.research.manifest import canonical_result_json, content_id

if TYPE_CHECKING:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker

QUOTE_SCREEN_MAX_AGE_SECONDS = 30


class DiscoverySample(BrokerModel):
    symbol: str
    source_at: datetime
    received_at: datetime
    dollar_volume: Decimal
    spread_bps: Decimal
    score: Decimal
    raw_digest: str
    ask: Decimal | None = None
    scan_receipt_id: str | None = None


class DiscoveryEvidence(BrokerModel):
    contract: Literal["BROKER_DISCOVERY_V1"] = "BROKER_DISCOVERY_V1"
    feed: Literal["iex"] = "iex"
    coverage: Literal["ALPACA_US_EQUITY_IEX_ROLLING"] = "ALPACA_US_EQUITY_IEX_ROLLING"
    catalog_id: str
    catalog_at: datetime
    eligible_count: int
    recorded_at: datetime
    symbols: tuple[str, ...]
    samples: tuple[DiscoverySample, ...]
    scan_receipt_ids: tuple[str, ...]
    preparation_refs: tuple[tuple[str, str], ...] = ()
    research_refs: tuple[tuple[str, str], ...] = ()
    research_gaps: tuple[str, ...] = ()
    limitation: str = (
        "Broker-returned US equities only; IEX is a single-venue feed. Rolling scan, "
        "not simultaneous all-market coverage. Options, crypto and other feeds are unverified."
    )

    @model_serializer(mode="wrap")
    def preserve_existing_receipts(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = dict(handler(self))
        for key in ("research_refs", "research_gaps"):
            if not payload.get(key):
                payload.pop(key)
        return payload

    @property
    def id(self) -> str:
        return content_id(self)


def _age(now: datetime, at: datetime) -> float:
    if now.tzinfo is None or at.tzinfo is None:
        return float("inf")
    return (now - at).total_seconds()


def _candidate_fresh(sample: DiscoverySample, now: datetime) -> bool:
    # New acquisition retains the original quote screen's margin for comparison work.
    # Historical receipt/claim validation below keeps its existing 90-second bound.
    return (
        0 <= _age(now, sample.source_at) <= QUOTE_SCREEN_MAX_AGE_SECONDS
        and 0 <= _age(now, sample.received_at) <= QUOTE_SCREEN_MAX_AGE_SECONDS
    )


def require_fresh_discovery(evidence: DiscoveryEvidence, now: datetime) -> None:
    if (
        not 0 <= _age(now, evidence.recorded_at) <= 90
        or not 0 <= _age(now, evidence.catalog_at) <= 3600
        or any(
            not 0 <= _age(now, s.source_at) <= 90 or not 0 <= _age(now, s.received_at) <= 90
            for s in evidence.samples
        )
    ):
        raise ValueError("DISCOVERY_RECEIPT_STALE")


def screen(symbol: str, raw: object, now: datetime) -> tuple[str | None, DiscoverySample | None]:
    if not isinstance(raw, dict) or not raw:
        return "SNAPSHOT_MISSING", None
    try:
        quote = raw["latestQuote"]
        at = datetime.fromisoformat(quote["t"].replace("Z", "+00:00"))
        ask, bid = Decimal(str(quote["ap"])), Decimal(str(quote["bp"]))
        if (
            not all(v.is_finite() for v in (ask, bid))
            or not 0 < bid <= ask
            or not 0 <= _age(now, at) <= QUOTE_SCREEN_MAX_AGE_SECONDS
        ):
            return "QUOTE_STALE_OR_INVALID", None
        spread = (ask - bid) / bid * 10000
        if spread > 10:
            return "SPREAD_EXCEEDS_COST_RESERVE", None
        minute = raw["minuteBar"]
        minute_at = datetime.fromisoformat(minute["t"].replace("Z", "+00:00"))
        volume = Decimal(str(raw["dailyBar"]["v"])) * Decimal(str(raw["dailyBar"]["c"]))
        if (
            not volume.is_finite()
            or volume <= 0
            or Decimal(str(minute["v"])) <= 0
            or not 0 <= _age(now, minute_at) <= 120
            or Decimal(str(quote["as"])) <= 0
            or Decimal(str(quote["bs"])) <= 0
        ):
            return "LIQUIDITY_UNAVAILABLE", None
        previous = Decimal(str(raw["prevDailyBar"]["c"]))
        score = abs(ask / previous - 1) if previous.is_finite() and previous > 0 else Decimal(0)
        return None, DiscoverySample(
            symbol=symbol,
            source_at=at,
            received_at=now,
            dollar_volume=volume,
            spread_bps=spread,
            score=score,
            raw_digest=content_id(raw),
            ask=ask,
        )
    except (KeyError, TypeError, ValueError, InvalidOperation, ZeroDivisionError):
        return "SNAPSHOT_MALFORMED", None


class DiscoveryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        with self.connection() as c:
            c.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS assets(symbol TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS samples(
                    symbol TEXT PRIMARY KEY,payload TEXT NOT NULL,
                    compared TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS comparisons(
                    boundary TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS preparation(
                    symbol TEXT PRIMARY KEY,payload TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=1)
        try:
            with c:
                yield c
        finally:
            c.close()

    @staticmethod
    def _get(c: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = c.execute("select payload from metadata where key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(c: sqlite3.Connection, key: str, value: Any) -> None:
        c.execute(
            "insert or replace into metadata values(?,?)", (key, canonical_result_json(value))
        )

    @staticmethod
    def _receipt(c: sqlite3.Connection, document: Any) -> str:
        identity = content_id(document)
        c.execute(
            "insert or ignore into receipts values(?,?)",
            (identity, canonical_result_json(document)),
        )
        return identity

    def record_catalog(self, payload: object, *, now: datetime) -> dict[str, Any]:
        if (
            not isinstance(payload, list)
            or not payload
            or any(not isinstance(a, dict) or not isinstance(a.get("symbol"), str) for a in payload)
        ):
            raise ValueError("DISCOVERY_CATALOG_INVALID")
        names = [a["symbol"] for a in payload]
        if len(set(names)) != len(names):
            raise ValueError("DISCOVERY_CATALOG_INVALID")
        filtered: Counter[str] = Counter()
        eligible = []
        for a in payload:
            reason = (
                "ASSET_CLASS_UNSUPPORTED"
                if a.get("class") != "us_equity"
                else "ASSET_NOT_TRADABLE"
                if a.get("status") != "active" or a.get("tradable") is not True
                else "BROKER_SYMBOL_SYNTAX_UNSUPPORTED"
                if not supported_symbol(a["symbol"])
                else None
            )
            if reason:
                filtered[reason] += 1
            else:
                eligible.append(a)
        document = {
            "kind": "BROKER_ASSET_CATALOG_V1",
            "at": now.isoformat(),
            "returned": len(payload),
            "eligible_count": len(eligible),
            "filtered": dict(filtered),
            "raw_digest": content_id(payload),
            "pagination": "UNPAGINATED_TRADING_API_MASTER",
            "scope": "ALPACA_US_EQUITY",
        }
        with self.connection() as c:
            c.execute("begin immediate")
            identity = self._receipt(c, document)
            document["id"] = identity
            c.execute("delete from assets")
            c.executemany(
                "insert into assets values(?,?)",
                [(a["symbol"], canonical_result_json(a)) for a in eligible],
            )
            self._set(c, "catalog", document)
        return document

    def next_scan(self, *, batch_size: int = 500) -> tuple[str, ...]:
        if batch_size < 1:
            raise ValueError("positive request batch size required")
        with self.connection() as c:
            pending = self._get(c, "pending_batches", [])
            while pending:
                active = {r[0] for r in c.execute("select symbol from assets")}
                members = tuple(s for s in pending[0] if s in active)
                if members:
                    if list(members) != pending[0]:
                        pending[0] = list(members)
                        self._set(c, "pending_batches", pending)
                    return members
                pending.pop(0)
                self._set(c, "pending_batches", pending)
            cursor = self._get(c, "cursor", "")
            rows = c.execute(
                "select symbol from assets where symbol>? order by symbol limit ?",
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                rows = c.execute(
                    "select symbol from assets order by symbol limit ?", (batch_size,)
                ).fetchall()
            return tuple(r[0] for r in rows)

    def record_scan(
        self,
        symbols: tuple[str, ...],
        snapshots: dict[str, object],
        *,
        now: datetime,
        market_open: bool,
        failure_reason: str | None = None,
    ) -> str:
        if not symbols or len(set(symbols)) != len(symbols) or set(snapshots) - set(symbols):
            raise ValueError("DISCOVERY_SCAN_SCOPE_INVALID")
        results = []
        accepted: list[DiscoverySample] = []
        filters: Counter[str] = Counter()
        with self.connection() as c:
            c.execute("begin immediate")
            for symbol in symbols:
                raw = snapshots.get(symbol)
                reason, sample = screen(symbol, raw, now)
                reason = failure_reason or reason
                if reason:
                    filters[reason] += 1
                results.append(
                    {
                        "symbol": symbol,
                        "reason": reason,
                        "sample": sample.model_dump(mode="json") if sample else None,
                        "preparation": {k: raw.get(k) for k in ("dailyBar", "prevDailyBar")}
                        if isinstance(raw, dict)
                        else None,
                        "raw_digest": content_id(snapshots.get(symbol)),
                    }
                )
                if sample and market_open:
                    accepted.append(sample)
                    c.execute(
                        "insert into samples(symbol,payload) values(?,?) "
                        "on conflict(symbol) do update set payload=excluded.payload",
                        (symbol, sample.model_dump_json()),
                    )
                else:
                    c.execute("delete from samples where symbol=?", (symbol,))
            document = {
                "kind": "DISCOVERY_SCAN_V1",
                "at": now.isoformat(),
                "market_open": market_open,
                "catalog_id": self._get(c, "catalog")["id"],
                "results": results,
                "filters": dict(filters),
                "transport_gap": failure_reason,
            }
            identity = self._receipt(c, document)
            for sample in accepted:
                c.execute(
                    "update samples set payload=? where symbol=?",
                    (
                        sample.model_copy(update={"scan_receipt_id": identity}).model_dump_json(),
                        sample.symbol,
                    ),
                )
            if not market_open:
                for result in results:
                    raw_prep = result["preparation"]
                    if isinstance(raw_prep, dict):
                        value = {"at": now.isoformat(), "receipt_id": identity, **raw_prep}
                        c.execute(
                            "insert or replace into preparation values(?,?)",
                            (result["symbol"], canonical_result_json(value)),
                        )
            self._set(
                c,
                "scan",
                {
                    "id": identity,
                    "at": now.isoformat(),
                    "filters": dict(filters),
                    "market_open": market_open,
                },
            )
            self._set(c, "cursor", symbols[-1])
            pending = self._get(c, "pending_batches", [])
            if pending and tuple(pending[0]) == symbols:
                self._set(c, "pending_batches", pending[1:])
        return identity

    def isolate_feed_gap(self, symbols: tuple[str, ...], reason: str, now: datetime) -> str:
        with self.connection() as c:
            c.execute("begin immediate")
            pending = self._get(c, "pending_batches", [])
            tail = pending[1:] if pending and tuple(pending[0]) == symbols else pending
            half = len(symbols) // 2
            self._set(c, "pending_batches", [list(symbols[:half]), list(symbols[half:]), *tail])
            return self._receipt(
                c,
                {
                    "kind": "DISCOVERY_FEED_SPLIT_V1",
                    "at": now.isoformat(),
                    "symbols": symbols,
                    "reason": reason,
                    "complete": False,
                },
            )

    def coverage(self) -> dict[str, Any]:
        with self.connection() as c:
            catalog = self._get(c, "catalog", {})
            scan = self._get(c, "scan", {})
            return {**catalog, "scan_filters": scan.get("filters", {}), "last_scan": scan}

    def select_candidates(
        self, *, now: datetime, boundary: str, batch_size: int = 12
    ) -> DiscoveryEvidence:
        with self.connection() as c:
            c.execute("begin immediate")
            prior = c.execute(
                "select payload from comparisons where boundary=?", (boundary,)
            ).fetchone()
            if prior:
                evidence = DiscoveryEvidence.model_validate_json(prior[0])
                # Retain the comparison's original 90-second receipt contract so
                # late bars can retry; new selections below still use 30 seconds.
                require_fresh_discovery(evidence, now)
                return evidence
            catalog = self._get(c, "catalog")
            if catalog is None or not 0 <= _age(now, datetime.fromisoformat(catalog["at"])) <= 3600:
                raise ValueError("DISCOVERY_CATALOG_STALE")
            candidates = []
            preparations = {}
            for payload, compared in c.execute(
                "select s.payload,s.compared from samples s join assets a on a.symbol=s.symbol"
            ):
                sample = DiscoverySample.model_validate_json(payload)
                if _candidate_fresh(sample, now):
                    score = sample.score
                    prep = c.execute(
                        "select payload from preparation where symbol=?", (sample.symbol,)
                    ).fetchone()
                    if prep:
                        raw = json.loads(prep[0])
                        try:
                            close = Decimal(str(raw["dailyBar"]["c"]))
                            if (
                                sample.ask is not None
                                and close.is_finite()
                                and close > 0
                                and 0 <= _age(now, datetime.fromisoformat(raw["at"])) <= 7 * 86400
                            ):
                                score = abs(sample.ask / close - 1)
                                preparations[sample.symbol] = raw["receipt_id"]
                        except (KeyError, TypeError, ValueError, InvalidOperation):
                            pass  # Preparation never grants stale price/asset authority.
                    candidates.append(
                        (compared, -score, -sample.dollar_volume, sample.symbol, sample)
                    )
            chosen = [r[-1] for r in sorted(candidates)[:batch_size]]
            if not chosen:
                raise ValueError("DISCOVERY_NO_FRESH_ELIGIBLE")
            evidence = DiscoveryEvidence(
                catalog_id=catalog["id"],
                catalog_at=datetime.fromisoformat(catalog["at"]),
                eligible_count=catalog["eligible_count"],
                recorded_at=now,
                symbols=tuple(s.symbol for s in chosen),
                samples=tuple(chosen),
                scan_receipt_ids=tuple(
                    sorted({s.scan_receipt_id for s in chosen if s.scan_receipt_id})
                ),
                preparation_refs=tuple(
                    (s.symbol, preparations[s.symbol]) for s in chosen if s.symbol in preparations
                ),
            )
            c.execute("insert into comparisons values(?,?)", (boundary, evidence.model_dump_json()))
            c.executemany(
                "update samples set compared=? where symbol=?",
                [(now.isoformat(), s.symbol) for s in chosen],
            )
            return evidence


async def discover_once(broker: AlpacaCoreBroker, state: DiscoveryStore) -> str | None:
    now = broker.now
    coverage = state.coverage()
    if "at" not in coverage or _age(now, datetime.fromisoformat(coverage["at"])) >= 3600:
        response = await broker.adapter._request(
            "GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"}
        )
        state.record_catalog(json.loads(response.content), now=broker.now)
    symbols = state.next_scan()
    if not symbols:
        return None
    clock = await broker.adapter.get_market_clock()
    try:
        snapshots = await broker.market.get_stock_snapshots(symbols)
    except MarketDataRequestError as error:
        status = next(
            (str(s) for s in (400, 404, 422) if str(error).endswith("HTTP " + str(s))), None
        )
        if status is None:
            raise  # Transient, entitlement, budget and rate failures never advance coverage.
        reason = "PERMANENT_FEED_GAP:" + status
        if len(symbols) > 1:
            return state.isolate_feed_gap(symbols, reason, broker.now)
        return state.record_scan(
            symbols, {}, now=broker.now, market_open=clock.is_open, failure_reason=reason
        )
    return state.record_scan(symbols, snapshots, now=broker.now, market_open=clock.is_open)


async def run_discovery(broker: AlpacaCoreBroker, state: DiscoveryStore) -> None:
    """One background coroutine owned by the existing Trader process and request budget."""
    while True:
        try:
            await discover_once(broker, state)
        except Exception as error:
            # Retain failed scans without advancing the durable cursor or fabricating fresh samples.
            try:
                with state.connection() as c:
                    state._receipt(
                        c,
                        {
                            "kind": "DISCOVERY_FAILURE_V1",
                            "at": broker.now.isoformat(),
                            "error": type(error).__name__,
                            "reason": str(error),
                        },
                    )
            except sqlite3.OperationalError as recording_error:
                code = getattr(recording_error, "sqlite_errorcode", 0)
                if code & 255 not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                    raise
                # A reader can block both the scan commit and its failure receipt.
                # Both transactions rolled back; keep the cursor and retry normally.
                print(
                    json.dumps(
                        {
                            "event": "DISCOVERY_FAILURE_RECEIPT_BUSY",
                            "at": broker.now.isoformat(),
                            "error": type(error).__name__,
                            "sqlite_errorcode": code,
                        }
                    ),
                    flush=True,
                )
        await asyncio.sleep(15)
