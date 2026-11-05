"""Free SEC submissions/XBRL research, deterministic and without order authority."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx

from quantbot.anomaly.news import _parse_timestamp
from quantbot.paper_core.discovery_research import _initialize
from quantbot.research.filings import HttpxFilingTransport
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.research.scout import SourceUnavailable

if TYPE_CHECKING:
    from quantbot.paper_core.discovery import DiscoveryStore

METRICS = (
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "NetIncomeLoss",
    "NetCashProvidedByUsedInOperatingActivities",
    "EarningsPerShareDiluted",
)


def record_company(
    state: DiscoveryStore,
    cik: str,
    symbols: tuple[str, ...],
    submissions: object,
    facts: object,
    *,
    received: datetime,
    raw_response_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not isinstance(submissions, dict) or str(submissions.get("cik", "")).zfill(10) != cik:
        raise ValueError("SEC_SUBMISSIONS_CIK_MISMATCH")
    if facts is not None and (
        not isinstance(facts, dict) or str(facts.get("cik", "")).zfill(10) != cik
    ):
        raise ValueError("SEC_FACTS_CIK_MISMATCH")
    if received.tzinfo is None:
        raise ValueError("SEC_RESEARCH_TIME_INVALID")
    recent = submissions.get("filings", {}).get("recent", {})
    required = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
    if any(not isinstance(recent.get(k), list) for k in required):
        raise ValueError("SEC_SUBMISSIONS_ARRAYS_INVALID")
    count = len(recent["accessionNumber"])
    if any(len(recent[k]) != count for k in required):
        raise ValueError("SEC_SUBMISSIONS_ARRAYS_MISMATCH")
    filings = []
    for i in range(count):
        form = recent["form"][i]
        if form not in {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A", "20-F", "6-K"}:
            continue
        filed = date.fromisoformat(recent["filingDate"][i])
        if not received.date() - timedelta(days=365) <= filed <= received.date():
            continue
        accepted = None
        if len(recent.get("acceptanceDateTime", [])) == count:
            accepted = _parse_timestamp(recent["acceptanceDateTime"][i])
        if accepted and accepted > received:
            continue
        filings.append(
            dict(
                accession=recent["accessionNumber"][i],
                form=form,
                filed_date=filed.isoformat(),
                accepted_at=accepted.isoformat() if accepted else None,
                publication_precision="INSTANT" if accepted else "DATE",
                report_date=recent["reportDate"][i],
                primary_document=recent["primaryDocument"][i],
                items=recent.get("items", [""] * count)[i]
                if len(recent.get("items", [])) == count
                else "",
            )
        )
    values = []
    taxonomy = facts.get("facts", {}).get("us-gaap", {}) if isinstance(facts, dict) else {}
    for metric in METRICS:
        units = taxonomy.get(metric, {}).get("units", {})
        for unit, rows in units.items():
            eligible = []
            for row in rows:
                try:
                    filed = date.fromisoformat(row["filed"])
                    value = Decimal(str(row["val"]))
                    if filed > received.date() or not value.is_finite():
                        continue
                    eligible.append(row)
                except (KeyError, ValueError, TypeError, InvalidOperation):
                    continue
            if eligible:
                row = max(eligible, key=lambda r: (r["filed"], r.get("end", "")))
                values.append(
                    dict(
                        metric=metric,
                        unit=unit,
                        value=str(row["val"]),
                        start=row.get("start"),
                        end=row.get("end"),
                        filed_date=row["filed"],
                        form=row.get("form"),
                        accession=row.get("accn"),
                        freshness="STALE"
                        if received.date() - date.fromisoformat(row["filed"]) > timedelta(days=365)
                        else "RECENT_FILED_DATE",
                    )
                )
    document = dict(
        kind="SEC_COMPANY_RESEARCH_V1",
        source_id="sec-company:" + cik,
        symbols=symbols,
        uri=f"https://data.sec.gov/submissions/CIK{cik}.json",
        facts_uri=f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json",
        cik=cik,
        published_at=None,
        updated_at=None,
        observed_at=received.isoformat(),
        timestamp_scope="AGGREGATE_OBSERVATION; PROVIDER_DATES_RETAINED_PER_FILING_AND_FACT",
        retrieved_at=received.isoformat(),
        available_at=received.isoformat(),
        recent_filings=filings,
        financial_facts=values,
        gaps=[
            "SEC_ROLLING_COMPANY_COVERAGE",
            "CUSTOM_AND_IFRS_FACTS_NOT_ANALYZED",
            "FINANCIAL_PERIODS_UNNORMALIZED",
            "NO_FILING_NARRATIVE_MODEL",
        ]
        + (["SEC_FACTS_UNAVAILABLE"] if facts is None else [])
        + (
            ["SEC_OLDER_SUBMISSION_PAGES_NOT_FETCHED"]
            if submissions.get("filings", {}).get("files")
            else []
        ),
        parsed_payload_sha256=content_id({"submissions": submissions, "facts": facts}),
        raw_response_sha256=raw_response_hashes or {},
        parser="sec-company-research-v1",
        epistemic_status="UNTESTED",
        order_authority=False,
    )
    _initialize(state)
    identity = content_id(document)
    with state.connection() as c:
        c.execute(
            "insert or ignore into research_documents values(?,?)",
            (identity, canonical_result_json(document)),
        )
        c.execute(
            "insert or replace into research_current values(?,?)", (document["source_id"], identity)
        )
        for symbol in symbols:
            c.execute(
                "insert or ignore into research_symbols values(?,?,?,?,?,?)",
                (
                    symbol,
                    identity,
                    document["source_id"],
                    document["retrieved_at"],
                    document["observed_at"],
                    document["kind"],
                ),
            )
    return document


def _collect(state: DiscoveryStore, contact: str, now: datetime) -> None:
    _initialize(state)
    # Existing SEC transport enforces spacing, response limits and declared identity.
    # Redirects are disabled so that the approved contact goes only to these fixed SEC hosts.
    transport = HttpxFilingTransport(
        contact, client=httpx.Client(follow_redirects=False), min_interval_seconds=1
    )
    try:
        with state.connection() as c:
            state._set(c, "sec_attempt_cik", None)
            mapping = state._get(c, "sec_company_mapping")
        if not mapping or now - datetime.fromisoformat(mapping["retrieved_at"]) > timedelta(days=1):
            raw = transport.fetch(
                "https://www.sec.gov/files/company_tickers.json", {}, timeout_seconds=5
            )
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not payload:
                raise ValueError("SEC_MAPPING_INVALID")
            mapping = dict(
                retrieved_at=datetime.now(UTC).isoformat(),
                payload=payload,
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                uri="https://www.sec.gov/files/company_tickers.json",
            )
            with state.connection() as c:
                state._set(c, "sec_company_mapping", mapping)
        with state.connection() as c:
            eligible = {r[0] for r in c.execute("select symbol from assets")}
            cursor = state._get(c, "sec_company_cursor", "")
            requests = [
                r[0]
                for r in c.execute(
                    "select symbol from research_requests order by requested_at,symbol"
                )
            ]
            priority_turn = state._get(c, "sec_priority_turn", True)
            failures = state._get(c, "sec_company_failures", {})
        companies: dict[str, list[str]] = {}
        for row in mapping["payload"].values():
            if (
                isinstance(row, dict)
                and row.get("ticker") in eligible
                and isinstance(row.get("cik_str"), int)
                and not isinstance(row["cik_str"], bool)
                and 0 < row["cik_str"] < 10**10
            ):
                companies.setdefault(str(row["cik_str"]).zfill(10), []).append(row["ticker"])
        if not companies:
            raise ValueError("SEC_NO_VERIFIED_COMPANY_MAPPING")
        names = sorted(companies)
        with state.connection() as c:
            state._set(
                c,
                "sec_coverage",
                dict(
                    mapped_companies=len(names),
                    mapped_symbols=sum(len(v) for v in companies.values()),
                    unmapped_symbols=len(eligible) - sum(len(v) for v in companies.values()),
                    complete_universe_researched=False,
                ),
            )
        by_symbol = {symbol: cik for cik, symbols in companies.items() for symbol in symbols}
        available = [
            cik
            for cik in names
            if cik not in failures or now >= datetime.fromisoformat(failures[cik]["retry_at"])
        ]
        if not available:
            with state.connection() as c:
                state._set(c, "filing_research_status", "SEC_COMPANY_RETRY_BACKOFF")
            return
        prioritized = next(
            (by_symbol[s] for s in requests if s in by_symbol and by_symbol[s] in available), None
        )
        priority = bool(prioritized and priority_turn)
        cik = (
            prioritized
            if priority
            else next((value for value in available if value > cursor), available[0])
        )
        assert cik is not None
        with state.connection() as c:
            state._set(c, "sec_attempt_cik", cik)
            state._set(c, "sec_attempt_priority", priority)
            state._set(c, "sec_attempt_symbols", companies[cik])
            for symbol in requests:
                if symbol not in by_symbol:
                    c.execute("delete from research_requests where symbol=?", (symbol,))
                    state._receipt(
                        c,
                        dict(
                            kind="SEC_COMPANY_MAPPING_GAP_V1",
                            symbol=symbol,
                            at=now.isoformat(),
                            order_authority=False,
                        ),
                    )
        raw = transport.fetch(
            f"https://data.sec.gov/submissions/CIK{cik}.json", {}, timeout_seconds=5
        )
        facts = None
        raw_hashes = {"submissions": hashlib.sha256(raw).hexdigest()}
        try:
            raw_facts = transport.fetch(
                f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", {}, timeout_seconds=5
            )
            raw_hashes["companyfacts"] = hashlib.sha256(raw_facts).hexdigest()
            facts = json.loads(raw_facts)
        except Exception:
            pass  # Record unavailable facts separately from successfully retrieved filings.
        received = datetime.now(UTC)
        document = record_company(
            state,
            cik,
            tuple(companies[cik]),
            json.loads(raw),
            facts,
            received=received,
            raw_response_hashes=raw_hashes,
        )
        with state.connection() as c:
            if not priority:
                state._set(c, "sec_company_cursor", cik)
            state._set(c, "sec_priority_turn", not priority)
            for symbol in companies[cik]:
                c.execute("delete from research_requests where symbol=?", (symbol,))
            failures = state._get(c, "sec_company_failures", {})
            failures.pop(cik, None)
            state._set(c, "sec_company_failures", failures)
            state._set(c, "filing_research_status", "SEC_ROLLING_PARTIAL_COVERAGE")
            state._receipt(
                c,
                dict(
                    kind="SEC_RESEARCH_RECEIPT_V1",
                    at=received.isoformat(),
                    cik=cik,
                    document_id=content_id(document),
                    mapping_sha256=mapping["raw_sha256"],
                    mapped_companies=len(names),
                    order_authority=False,
                ),
            )
    finally:
        transport.close()


async def filings_once(state: DiscoveryStore, now: datetime) -> None:
    contact = os.environ.get("QUANTBOT_SEC_CONTACT", "").strip()
    with state.connection() as c:
        if not contact:
            state._set(c, "filing_research_status", "SEC_CONTACT_NOT_CONFIGURED")
            return
        next_at = state._get(c, "sec_next_attempt")
        if next_at and now < datetime.fromisoformat(next_at):
            return
        state._set(c, "sec_next_attempt", (now + timedelta(seconds=15)).isoformat())
    try:
        await asyncio.to_thread(_collect, state, contact, now)
    except Exception as error:
        with state.connection() as c:
            cik = state._get(c, "sec_attempt_cik")
            if cik:
                failures = state._get(c, "sec_company_failures", {})
                failures[cik] = dict(
                    at=now.isoformat(),
                    error=type(error).__name__,
                    retry_at=(now + timedelta(minutes=5)).isoformat(),
                )
                state._set(c, "sec_company_failures", failures)
                for symbol in state._get(c, "sec_attempt_symbols", []):
                    c.execute("delete from research_requests where symbol=?", (symbol,))
                # Keep an explicit gap and retry on the next sweep; one bad company
                # must not indefinitely prevent research on all other mapped companies.
                if not state._get(c, "sec_attempt_priority", False):
                    state._set(c, "sec_company_cursor", cik)
                state._set(c, "sec_priority_turn", False)
            delay = (
                timedelta(minutes=5)
                if isinstance(error, SourceUnavailable)
                else timedelta(seconds=15)
            )
            state._set(c, "sec_next_attempt", (now + delay).isoformat())
            state._set(c, "filing_research_status", "SEC_RESEARCH_UNAVAILABLE")
            state._receipt(
                c,
                dict(
                    kind="SEC_RESEARCH_FAILURE_V1",
                    at=now.isoformat(),
                    error=type(error).__name__,
                    order_authority=False,
                ),
            )
