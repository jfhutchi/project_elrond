"""Read-only PAPER daily fees: observed account charges are not exact per-fill costs.

Run this file directly, or use python -m quantbot.reporting.paper_costs. No broker,
credentials, database initialization, order interfaces or third-party dependencies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
ENDPOINT = "https://paper-api.alpaca.markets"


def _time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    return moment


def _money(value: Any) -> Decimal:
    amount = Decimal(str(value))
    if not amount.is_finite():
        raise ValueError("FINITE_AMOUNT_REQUIRED")
    return amount


def _digest(document: Any) -> str:
    encoded = json.dumps(document, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def build_report(
    *,
    trips: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    sessions: list[dict[str, Any]],
    account_id: str,
    as_of: datetime,
) -> dict[str, Any]:
    """No absence-as-zero, fee allocation guesses, or model-to-actual promotion."""
    if as_of.tzinfo is None:
        raise ValueError("AWARE_TIMESTAMP_REQUIRED")
    by_fill = {f["fill_id"]: f for f in fills}
    if len(by_fill) != len(fills):
        raise ValueError("DUPLICATE_OWNED_FILL")
    by_day: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trip in trips:
        if trip.get("evidence_lane") != "BROKER_PAPER":
            continue
        day = _time(trip["source_end"]).astimezone(NY).date().isoformat()
        by_day[day].append(trip)
    eligible_docs = []
    activities: dict[str, dict[str, Any]] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    for source in documents:
        doc = source["document"]
        if _digest(doc) != source["id"]:
            raise ValueError("SOURCE_DIGEST_MISMATCH")
        if doc.get("account_id") != account_id or doc.get("endpoint") != ENDPOINT:
            continue
        if _time(doc["until"]) > as_of:
            continue
        eligible_docs.append(source)
        for row in doc["rows"]:
            identity = row.get("id")
            if not isinstance(identity, str) or not identity:
                raise ValueError("ACTIVITY_ID_REQUIRED")
            if identity in activities and activities[identity] != row:
                raise ValueError("CONFLICTING_ACTIVITY")
            activities[identity] = row
            sources[identity].add(source["id"])
    # Account charges can post on dates with no completed owned trades. Keep
    # those cash dates visible without manufacturing zero trade economics.
    for row in activities.values():
        if row.get("activity_type") not in {"FEE", "CFEE"} or not row.get("date"):
            continue
        if row.get("created_at") and _time(row["created_at"]) > as_of:
            continue
        by_day.setdefault(row["date"], [])
    calendar = {s["session_date"]: s for s in sessions}
    capture_until = max((_time(s["document"]["until"]) for s in eligible_docs), default=None)
    days: list[dict[str, Any]] = []
    for day, owned in sorted(by_day.items()):
        session = calendar.get(day)
        coverage = (
            []
            if session is None
            else [
                d
                for d in eligible_docs
                if _time(d["document"]["after"]) <= _time(session["session_open"])
                and _time(d["document"]["until"]) >= _time(session["session_close"])
                and _time(session["session_close"]) <= as_of
            ]
        )
        covered_ids = {d["id"] for d in coverage}
        owned_ids: set[str] = set()
        gross = Decimal(0)
        cashflow = Decimal(0)
        asset_fee_basis = Decimal(0)
        has_crypto = False
        entry_notional = Decimal(0)
        exit_notional = Decimal(0)
        intraday = True
        per_trade = []
        for trip in owned:
            buys = [by_fill[i] for i in trip["entry_fill_ids"]]
            sells = [by_fill[i] for i in trip["exit_fill_ids"]]
            selected = buys + sells
            ids = {f["fill_id"] for f in selected}
            if owned_ids.intersection(ids):
                raise ValueError("FILL_REUSED_BY_OWNED_LOTS")
            owned_ids.update(ids)
            bought = sum((_money(f["quantity"]) * _money(f["price"]) for f in buys), Decimal(0))
            sold = sum((_money(f["quantity"]) * _money(f["price"]) for f in sells), Decimal(0))
            crypto = "/" in buys[0]["symbol"]
            has_crypto |= crypto
            fee_quantity = _money(trip.get("asset_fee_quantity", "0"))
            fee_basis = _money(trip.get("asset_fee_cost_basis", "0"))
            if trip.get("pnl_basis") is not None or fee_quantity or fee_basis:
                bought_quantity = sum((_money(f["quantity"]) for f in buys), Decimal(0))
                sold_quantity = sum((_money(f["quantity"]) for f in sells), Decimal(0))
                if (
                    not crypto
                    or trip.get("pnl_basis") != "WEIGHTED_ENTRY_BASIS_WITH_ASSET_FEES_V1"
                    or not 0 <= fee_quantity < bought_quantity
                    or bought_quantity - sold_quantity != fee_quantity
                    or fee_basis != fee_quantity * (bought / bought_quantity)
                    or (fee_quantity > 0 and not trip.get("asset_fee_allocation_ids"))
                    or _money(trip.get("cashflow_pnl")) != sold - bought
                ):
                    raise ValueError("ASSET_FEE_BASIS_MISMATCH")
            if sold - bought + fee_basis != _money(trip["gross_pnl"]):
                raise ValueError("GROSS_FILL_MISMATCH")
            intraday &= all(
                _time(f["occurred_at"]).astimezone(NY).date().isoformat() == day for f in selected
            )
            gross += sold - bought + fee_basis
            cashflow += sold - bought
            asset_fee_basis += fee_basis
            entry_notional += bought
            exit_notional += sold
            per_trade.append(
                dict(
                    lot_id=trip["lot_id"],
                    symbol=buys[0]["symbol"],
                    entry_notional=str(bought),
                    gross_realized=str(sold - bought + fee_basis),
                    cashflow_realized=str(sold - bought),
                    asset_fee_quantity=str(fee_quantity),
                    asset_fee_cost_basis=str(fee_basis),
                    actual_fees=trip.get("actual_fees"),
                    native_net=trip.get("net_pnl"),
                    exact_fee_allocation="UNAVAILABLE"
                    if trip.get("net_pnl") is None
                    else "NATIVE_ATTRIBUTED",
                )
            )
        account_fills = {
            i: row
            for i, row in activities.items()
            if row.get("activity_type") == "FILL"
            and _time(row["transaction_time"]).astimezone(NY).date().isoformat() == day
        }
        same = bool(owned) and bool(coverage) and intraday and set(account_fills) == owned_ids
        if same:
            same = all(
                row["symbol"] == by_fill[i]["symbol"]
                and row["side"].upper() == by_fill[i]["side"].upper()
                and row["order_id"] == by_fill[i]["broker_order_id"]
                and _money(row["qty"]) == _money(by_fill[i]["quantity"])
                and _money(row["price"]) == _money(by_fill[i]["price"])
                for i, row in account_fills.items()
            )
        fees = []
        invalid_fees = []
        for identity, row in activities.items():
            if row.get("activity_type") not in {"FEE", "CFEE"} or row.get("date") != day:
                continue
            created = row.get("created_at")
            if not created or row.get("currency") != "USD" or row.get("status") != "executed":
                invalid_fees.append(identity)
                continue
            if _time(created) <= as_of:
                fees.append(
                    dict(
                        id=identity,
                        cash_flow=str(_money(row["net_amount"])),
                        component=row.get("activity_sub_type") or row.get("activity_subtype"),
                        created_at=created,
                        source_document_ids=sorted(sources[identity]),
                    )
                )
        fee_cash = sum((_money(f["cash_flow"]) for f in fees), Decimal(0)) if fees else None
        # XNYS session coverage and prior stock bills cannot establish crypto costs.
        usable = same and not invalid_fees and not has_crypto
        observed_net = gross + fee_cash if usable and fee_cash is not None else None
        scenario = None
        prior = next(
            (
                d
                for d in reversed(days)
                if d["scope_verified"]
                and d["observed_fee_cash_flow"] is not None
                and _money(d["observed_fee_cash_flow"]) < 0
            ),
            None,
        )
        if usable and prior and not any(_money(f["cash_flow"]) > 0 for f in fees):
            baseline = -_money(prior["observed_fee_cash_flow"])
            observed_debit = -fee_cash if fee_cash is not None else Decimal(0)
            remainder = max(Decimal(0), baseline - observed_debit)
            if remainder:
                scenario = dict(
                    kind="EMPIRICAL_PRIOR_SESSION_FEE_SCENARIO",
                    reference_session=prior["session_date"],
                    reference_fee_activity_ids=[f["id"] for f in prior["fee_activities"]],
                    assumed_daily_fee=str(baseline),
                    modeled_additional_fee=str(remainder),
                    modeled_net=str(gross - observed_debit - remainder),
                    actual_cost_authority=False,
                    learning_label_authority=False,
                )
        days.append(
            dict(
                session_date=day,
                round_trips=len(owned),
                gross_realized=str(gross) if owned else None,
                cashflow_realized=str(cashflow) if owned else None,
                asset_fee_cost_basis=str(asset_fee_basis) if owned else None,
                entry_notional=str(entry_notional) if owned else None,
                exit_notional=str(exit_notional) if owned else None,
                opening_equity=session.get("equity") if session else None,
                scope_verified=usable,
                scope_reason="NO_COMPLETED_OWNED_ROUND_TRIPS"
                if not owned
                else "CRYPTO_COST_FINALITY_UNVERIFIED"
                if has_crypto
                else "EXACT_ACCOUNT_FILL_MATCH_FULL_SESSION"
                if usable
                else "INCOMPLETE_OR_MISMATCHED_ACTIVITY_SCOPE",
                source_document_ids=sorted(covered_ids),
                activity_capture_until=capture_until.isoformat() if capture_until else None,
                activity_capture_lag_seconds=(as_of - capture_until).total_seconds()
                if capture_until
                else None,
                fee_status="OBSERVED_FEES_WITH_UNVERIFIED_ACTIVITY"
                if fees and invalid_fees
                else "UNVERIFIED_FEE_ACTIVITY"
                if invalid_fees
                else "POSTED_DAILY_FEES_NO_EXACT_FILL_ALLOCATION"
                if fees
                else "NO_FEE_ACTIVITY_OBSERVED_BY_CAPTURE",
                observed_fee_cash_flow=str(fee_cash) if fee_cash is not None else None,
                net_after_observed_fees=str(observed_net) if observed_net is not None else None,
                all_in_verified_net=None,
                fee_activities=fees,
                invalid_fee_activity_ids=invalid_fees,
                fee_scenario=scenario,
                trades=per_trade,
            )
        )
    return dict(
        contract="PAPER_DAILY_OBSERVED_FEES_V1",
        as_of=as_of.isoformat(),
        account_hash=hashlib.sha256(account_id.encode()).hexdigest(),
        days=days,
        limitations=[
            "Daily fees do not establish exact fill-level allocation or fee finality; "
            "unknown costs remain unknown.",
            "Fee scenarios reuse the latest earlier session's observed debit once per day "
            "and are not verified actual costs or live fee forecasts.",
            "Gross uses execution prices; no additional spread or fill slippage is deducted. "
            "Unsimulated latency, impact and other unobserved costs remain excluded.",
            "Execution-price arithmetic can differ from cent-rounded account cash. "
            "This report does not replace account reconciliation or claim account net return.",
        ],
        production_database_writes=0,
        provider_calls=0,
    )


def read_report(
    path: Path,
    *,
    as_of: datetime,
    max_read_seconds: float = 4,
    max_rows: int = 25_000,
    max_bytes: int = 32_000_000,
) -> dict[str, Any]:
    """Read an existing core store without constructing Database or modifying a ledger."""
    path = path.resolve()
    deadline = time.monotonic() + max_read_seconds
    read_rows = 0
    read_bytes = 0

    def check_budget() -> None:
        if time.monotonic() >= deadline or read_rows > max_rows or read_bytes > max_bytes:
            raise ValueError("PAPER_COST_READ_BUDGET_EXCEEDED")

    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)) as c:
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA query_only=ON")
        c.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)

        def rows(sql: str, params: list[str] | tuple[str, ...] = ()) -> list[sqlite3.Row]:
            nonlocal read_rows, read_bytes
            check_budget()
            result = []
            try:
                for row in c.execute(sql, params):
                    read_rows += 1
                    read_bytes += sum(
                        len(str(value).encode()) for value in row if value is not None
                    )
                    check_budget()
                    result.append(row)
            except sqlite3.OperationalError as error:
                if time.monotonic() >= deadline:
                    raise ValueError("PAPER_COST_READ_BUDGET_EXCEEDED") from error
                raise
            return result

        c.execute("BEGIN")
        identities = rows("SELECT payload FROM core_identities")
        if len(identities) != 1:
            raise ValueError("EXACT_PAPER_IDENTITY_REQUIRED")
        identity = json.loads(identities[0][0])
        if identity["evidence_lane"] != "BROKER_PAPER":
            raise ValueError("PAPER_ONLY_REPORT")
        trips = [json.loads(r[0]) for r in rows("SELECT payload FROM core_round_trips")]
        ids = sorted({i for t in trips for i in (*t["entry_fill_ids"], *t["exit_fill_ids"])})
        fills: list[dict[str, Any]] = []
        for start in range(0, len(ids), 400):
            chunk = ids[start : start + 400]
            fills.extend(
                dict(r)
                for r in rows(
                    "SELECT * FROM fills WHERE fill_id IN (" + ",".join("?" for _ in chunk) + ")",
                    chunk,
                )
            )
        external = [
            json.loads(r[0]) for r in rows("SELECT payload FROM core_external_owner_events")
        ]
        documents = {
            e["source_document"]
            for e in external
            if e.get("activity_type") in {"FEE", "CFEE"} and e.get("source_document")
        }
        documents.update(r[0] for r in rows("SELECT document_id FROM core_provider_progress"))
        sources = []
        for doc_id in sorted(documents):
            found = rows("SELECT payload FROM core_provider_acquisitions WHERE id=?", (doc_id,))
            row = found[0] if found else None
            if row is None:
                raise ValueError("FEE_SOURCE_DOCUMENT_UNAVAILABLE")
            sources.append(dict(id=doc_id, document=json.loads(row[0])))
        sessions = []
        for row in rows(
            "SELECT d.id,d.payload FROM core_session_opening_index i "
            "JOIN core_session_openings d ON d.id=i.document_id"
        ):
            opening = json.loads(row[1])
            if _digest(opening) != row[0]:
                raise ValueError("SESSION_SOURCE_DIGEST_MISMATCH")
            found = rows(
                "SELECT payload FROM core_time_evidence WHERE id=?", (opening["time_evidence_id"],)
            )
            timing = found[0] if found else None
            if timing:
                doc = json.loads(timing[0])
                if _digest(doc) != opening["time_evidence_id"]:
                    raise ValueError("CLOCK_SOURCE_DIGEST_MISMATCH")
                samples = doc.get("samples", [])
                if len(samples) == 2 and not doc.get("objections"):
                    provider = samples[-1]["provider"]
                    if (
                        opening.get("account_id") == identity["account_id"]
                        and _time(samples[0]["provider"]["timestamp"])
                        < _time(provider["timestamp"])
                        and _time(provider["session_close"]) > _time(opening["session_open"])
                        and all(
                            s["provider"].get("provider") == "alpaca"
                            and s["provider"]["account_id"] == identity["account_id"]
                            and s["provider"]["session_id"] == opening["session_date"]
                            and _time(s["provider"]["session_open"])
                            == _time(opening["session_open"])
                            and _time(s["provider"]["session_close"])
                            == _time(provider["session_close"])
                            for s in samples
                        )
                    ):
                        sessions.append(dict(opening, session_close=provider["session_close"]))
        if __package__:
            from quantbot.reporting.paper_cash import read_cash_reconciliation
        else:
            # Direct-file execution places this sibling on sys.path.
            from paper_cash import (  # type: ignore[import-not-found,no-redef]
                read_cash_reconciliation,
            )
        cash_reconciliation = read_cash_reconciliation(
            rows, account_id=identity["account_id"], as_of=as_of
        )
    check_budget()
    report = build_report(
        trips=trips,
        fills=fills,
        documents=sources,
        sessions=sessions,
        account_id=identity["account_id"],
        as_of=as_of,
    )
    check_budget()
    report["cash_reconciliation"] = cash_reconciliation
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = read_report(args.database, as_of=datetime.now(UTC))
    encoded = json.dumps(report, indent=2)
    if args.output:
        if args.output.resolve() == args.database.resolve():
            raise ValueError("OUTPUT_ALIASES_DATABASE")
        with args.output.open("x", encoding="utf-8") as output:
            output.write(encoded)
    print(encoded)


if __name__ == "__main__":
    main()
