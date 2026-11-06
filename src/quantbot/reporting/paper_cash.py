"""Compare retained posted movements to a certified snapshot; grants no authority."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Any, Protocol

ENDPOINT = "https://paper-api.alpaca.markets"


class ReadRows(Protocol):
    def __call__(
        self, sql: str, params: list[str] | tuple[str, ...] = ()
    ) -> list[sqlite3.Row]: ...


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError(reason)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                    separators=(",", ":")).encode()).hexdigest()


def _time(value: str) -> datetime:
    at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(at.tzinfo is not None, "AMBIGUOUS_ACTIVITY_TIME")
    return at


def _amount(value: Any) -> Decimal:
    result = Decimal(str(value))
    _require(result.is_finite(), "NONFINITE_CASH_MOVEMENT")
    return result


def read_cash_reconciliation(
    rows: ReadRows, *, account_id: str, as_of: datetime
) -> dict[str, Any]:
    """Use the caller's read-only snapshot and budget for every query."""
    result: dict[str, Any] = dict(
                  available=False, status="UNAVAILABLE", reasons=[], snapshot_at=None,
                  baseline_cash=None, observed_cash=None, calculated_cash=None, residual=None,
                  posted_fee_cash_flow=None, posted_fee_activity_count=0,
                  positions_match=False, exact_cash_complete=None, snapshot_stale=None,
                  unknown_fee_fill_count=None, unknown_inherited_net_cash_count=None,
                  fee_finality="UNKNOWN", coverage_complete=False, actual_cost_authority=False,
                  learning_label_authority=False, contract="RETAINED_POSTED_CASH_COMPARISON_V1")

    def document(table: str, identity: str) -> dict[str, Any]:
        found = rows(f"SELECT payload FROM {table} WHERE id=?", (identity,))
        _require(len(found) == 1, "CASH_SOURCE_DOCUMENT_UNAVAILABLE")
        value: dict[str, Any] = json.loads(found[0][0])
        _require(_digest(value) == identity, "CASH_SOURCE_DIGEST_MISMATCH")
        return value

    try:
        required = {"core_fixture_baselines", "core_reconciliation_proofs",
                    "core_inventory_certifications", "core_quantity_certifications",
                    "broker_orders", "order_intents", "core_intent_links", "core_cost_items"}
        tables = {r[0] for r in rows("SELECT name FROM sqlite_master WHERE type='table'")}
        _require(required <= tables,
                 "CASH_EVIDENCE_SCHEMA_UNAVAILABLE")
        latest = rows("SELECT generation,proof_id FROM (SELECT generation,proof_id FROM "
                      "core_inventory_certifications UNION ALL SELECT generation,proof_id FROM "
                      "core_quantity_certifications) ORDER BY generation DESC LIMIT 1")
        _require(bool(latest), "CERTIFIED_CASH_SNAPSHOT_UNAVAILABLE")
        proof_id = latest[0][1]
        proof = document("core_reconciliation_proofs", proof_id)
        snapshot = _time(proof["recorded_at_utc"])
        result.update(snapshot_at=snapshot.isoformat(), proof_id=proof_id,
                      baseline_id=proof["baseline_id"], exact_cash_complete=proof["cash_complete"],
                      observed_cash=str(_amount(proof["account"]["cash"])),
                      snapshot_age_seconds=(as_of - snapshot).total_seconds(),
                      snapshot_stale=(as_of - snapshot).total_seconds() > 120)
        _require(snapshot <= as_of and proof["complete"] and proof["generation"] == latest[0][0],
                 "CERTIFIED_SNAPSHOT_TIME_OR_GENERATION_UNVERIFIED")
        _require(proof["evidence_lane"] == "BROKER_PAPER" and
                 proof["account"]["account_id"] == account_id and
                 proof["account"].get("currency") == "USD", "CASH_ACCOUNT_MISMATCH")
        baseline = document("core_fixture_baselines", proof["baseline_id"])
        origin = baseline["provider_acquisition"]
        _require(baseline["source"] == "BROKER_PAPER_APPROVED_INITIAL_STATE" and
                 baseline["provider"] == origin["provider"] == "alpaca" and
                 baseline["endpoint"] == origin["endpoint"] == ENDPOINT and
                 baseline["account"] == origin["account"] and
                 baseline["account"]["account_id"] == account_id and
                 baseline["account"].get("currency") == "USD" and
                 baseline["source_ids"] == [_digest(origin)], "CASH_BASELINE_BINDING_MISMATCH")
        start = _time(baseline["captured_at"])
        _require(start <= snapshot and _time(origin["captured_at"]) == start,
                 "CASH_BASELINE_TIME_MISMATCH")
        cash = _amount(baseline["account"]["cash"])
        result["baseline_cash"] = str(cash)
        quantities = {s: _amount(q) for s, q in baseline["quantities"]}
        _require(len(quantities) == len(baseline["quantities"]), "DUPLICATE_BASELINE_POSITION")
        fills = [dict(r) for r in rows(
            "SELECT f.* FROM fills f JOIN broker_orders b ON b.broker_order_id=f.broker_order_id "
            "JOIN order_intents i ON i.client_order_id=b.client_order_id "
            "JOIN core_intent_links l ON l.intent_id=i.intent_id")]
        fill_ids = {f["fill_id"] for f in fills}
        _require(len(fill_ids) == len(fills) == len(proof["fill_ids"]) and
                 fill_ids == set(proof["fill_ids"]), "CERTIFIED_SNAPSHOT_FILL_SET_STALE")
        costs: dict[str, dict[str, Any]] = {}
        for row in rows("SELECT id,payload FROM core_cost_items"):
            item = json.loads(row[1])
            _require(_digest(item) == row[0], "COST_SOURCE_DIGEST_MISMATCH")
            if item["component"] == "FEE" and _time(item["recorded_at_utc"]) <= snapshot:
                prior = costs.get(item["fill_id"])
                if prior is None or item["revision"] > prior["revision"]:
                    costs[item["fill_id"]] = item
        missing_fees = [f["fill_id"] for f in fills if
                        (costs.get(f["fill_id"], {}).get("source", f.get("fee_source"))
                         not in {"BROKER_REPORTED", "CONTRACTUAL_ZERO"} or
                         costs.get(f["fill_id"], {}).get("amount", f.get("fee")) is None)]
        result.update(unknown_fee_fill_count=len(missing_fees),
                      unknown_fee_fill_ids=missing_fees[:5])
        orders = {r["broker_order_id"]: dict(r) for r in rows("SELECT * FROM broker_orders")}
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for fill in fills:
            _require(start < _time(fill["occurred_at"]) <= snapshot, "FILL_OUTSIDE_SNAPSHOT")
            order = orders[fill["broker_order_id"]]
            _require(order["broker_order_id"] in proof["order_ids"] and
                     _time(order["submitted_at"]) >= start, "BASELINE_CROSSING_ORDER_UNSUPPORTED")
            groups[fill["broker_order_id"]].append(fill)
        sources = {}
        events = [json.loads(r[0]) for r in rows("SELECT payload FROM core_external_owner_events")]
        unknown_external = [e["event_id"] for e in events if e.get("net_cash") is None]
        result.update(unknown_inherited_net_cash_count=len(unknown_external),
                      unknown_inherited_net_cash_ids=unknown_external[:5])
        seen = set(fill_ids)
        fee_cash = Decimal(0)
        fee_count = 0
        for event in events:
            _require(event["account_id"] == account_id, "EXTERNAL_CASH_ACCOUNT_MISMATCH")
            _require(start < _time(event["occurred_at"]) <= snapshot,
                     "EXTERNAL_EVENT_OUTSIDE_SNAPSHOT")
            source_id = event["source_document"]
            if source_id not in sources:
                source = document("core_provider_acquisitions", source_id)
                _require(source["account_id"] == account_id and source["endpoint"] == ENDPOINT,
                         "ACTIVITY_SOURCE_ACCOUNT_MISMATCH")
                _require(_time(source["after"]) < _time(source["until"]) <= snapshot,
                         "ACTIVITY_SOURCE_AFTER_SNAPSHOT")
                raw = {r["id"]: r for r in source["rows"]}
                _require(len(raw) == len(source["rows"]), "DUPLICATE_SOURCE_ACTIVITY")
                sources[source_id] = raw
            _require(event["event_id"] in sources[source_id] and event["event_id"] not in seen,
                     "EXTERNAL_ACTIVITY_MISSING_OR_DUPLICATED")
            seen.add(event["event_id"])
            raw = sources[source_id][event["event_id"]]
            if raw["activity_type"] == "FILL":
                sign = 1 if raw["side"].upper() == "BUY" else -1
                _require(raw["side"].upper() in {"BUY", "SELL"} and
                         raw["symbol"] == event["symbol"] and
                         raw["order_id"] == event["order_id"] and
                         _time(raw["transaction_time"]) == _time(event["occurred_at"]) and
                         sign * _amount(raw["qty"]) == _amount(event["quantity_delta"]),
                         "INHERITED_FILL_SOURCE_MISMATCH")
                _require(raw["order_id"] not in {f["broker_order_id"] for f in fills},
                         "MIXED_ORDER_OWNERSHIP")
                groups[raw["order_id"]].append(dict(fill_id=raw["id"], symbol=raw["symbol"],
                    side=raw["side"].upper(), quantity=raw["qty"], price=raw["price"],
                    occurred_at=raw["transaction_time"], cumulative=raw["cum_qty"],
                    net_cash=event["net_cash"]))
                if event["net_cash"] is not None:
                    _require(_amount(raw["net_amount"]) == _amount(event["net_cash"]),
                             "INHERITED_NET_CASH_SOURCE_MISMATCH")
            else:
                _require(raw["activity_type"] == event["activity_type"] and
                         raw.get("status") == "executed" and raw.get("currency") == "USD" and
                         _time(raw["created_at"]) == _time(event["occurred_at"]) and
                         _amount(event["quantity_delta"]) == 0 and
                         _amount(raw["net_amount"]) == _amount(event["net_cash"]),
                         "POSTED_CASH_ACTIVITY_UNVERIFIED")
                amount = _amount(raw["net_amount"])
                cash += amount
                if raw["activity_type"] in {"FEE", "CFEE"}:
                    fee_cash += amount
                    fee_count += 1
        for order_id, executions in groups.items():
            # Provider timestamps can invert adjacent partial executions; cumulative
            # quantity establishes their sequence independently of arrival time.
            executions.sort(key=lambda f: (
                _amount(f.get("cumulative", 0)), _time(f["occurred_at"])))
            cumulative = total = Decimal(0)
            first = executions[0]
            sign = 1 if first["side"] == "BUY" else -1
            for fill in executions:
                qty, price = _amount(fill["quantity"]), _amount(fill["price"])
                _require(qty > 0 and price > 0 and fill["side"] in {"BUY", "SELL"} and
                         (fill["symbol"], fill["side"]) == (first["symbol"], first["side"]),
                         "INVALID_ORDER_EXECUTION")
                cumulative += qty
                _require("cumulative" not in fill or _amount(fill["cumulative"]) == cumulative,
                         "ORDER_EXECUTION_PREFIX_INCOMPLETE")
                total += qty * price
                quantities[fill["symbol"]] = quantities.get(fill["symbol"], Decimal(0)) + sign * qty
            _require(order_id in orders, "CANONICAL_ORDER_UNAVAILABLE")
            order = orders[order_id]
            _require(_time(order["submitted_at"]) >= start, "BASELINE_CROSSING_ORDER_UNSUPPORTED")
            _require(cumulative == _amount(order["filled_quantity"]) and
                     (order["symbol"], order["side"]) == (first["symbol"], first["side"]),
                     "ORDER_FILLED_QUANTITY_MISMATCH")
            rounded = total.quantize(Decimal(".01"), rounding=ROUND_HALF_EVEN)
            _require(abs(total - rounded) != Decimal(".005"), "ORDER_CENT_TIE_UNVERIFIED")
            known = [f for f in executions if f.get("net_cash") is not None]
            _require(not known or len(known) == len(executions), "PARTIAL_NET_CASH_COVERAGE")
            cash += (sum((_amount(f["net_cash"]) for f in known), Decimal(0))
                     if known else -sign * rounded)
        expected = {s: q for s, q in quantities.items() if q}
        result["positions_match"] = (
            expected == {s: _amount(q) for s, q in proof["expected_quantities"]}
            == {s: _amount(q) for s, q in proof["actual_quantities"]})
        _require(result["positions_match"], "RETAINED_POSITION_MOVEMENTS_MISMATCH")
        residual = _amount(proof["account"]["cash"]) - cash
        result.update(available=True,
                      status="MATCHED_RETAINED_MOVEMENTS" if residual == 0 else "MISMATCH",
                      reasons=["RETAINED_MOVEMENTS_ONLY_ACTIVITY_COVERAGE_UNVERIFIED"],
                      calculated_cash=str(cash), residual=str(residual),
                      posted_fee_cash_flow=str(fee_cash) if fee_count else None,
                      posted_fee_activity_count=fee_count,
                      source_document_ids=sorted(sources), order_count=len(groups))
        if result["snapshot_stale"]:
            result["reasons"].append("RECORDED_SNAPSHOT_OLDER_THAN_FUNDS_VALIDITY_WINDOW")
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        if str(error) == "PAPER_COST_READ_BUDGET_EXCEEDED":
            raise
        result["reasons"] = [str(error) if isinstance(error, ValueError)
                             else "CASH_SOURCE_SCHEMA_UNVERIFIED"]
    return result
