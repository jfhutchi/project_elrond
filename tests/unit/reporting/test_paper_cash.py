"""Retained posted cash can match while actual trade fees remain unknown."""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from quantbot.reporting.paper_costs import read_report

BASE = "2026-11-05T14:00:00+00:00"
AT = "2026-11-06T18:00:00+00:00"
AS_OF = datetime(2026, 11, 6, 18, 0, tzinfo=UTC)
ENDPOINT = "https://paper-api.alpaca.markets"


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def put(connection, table, value, identity=None):
    key = identity or digest(value)
    connection.execute(f"INSERT INTO {table} VALUES (?,?)", (key, json.dumps(value)))
    return key


def fixture(path, fault=None):
    with sqlite3.connect(path) as c:
        for table in (
            "core_identities", "core_round_trips", "core_external_owner_events",
            "core_provider_acquisitions", "core_session_openings", "core_time_evidence",
            "core_fixture_baselines", "core_reconciliation_proofs", "core_cost_items",
        ):
            c.execute(f"CREATE TABLE {table}(id TEXT PRIMARY KEY,payload TEXT)")
        for table in ("core_provider_progress", "core_session_opening_index"):
            c.execute(f"CREATE TABLE {table}(document_id TEXT)")
        for table in ("core_inventory_certifications", "core_quantity_certifications"):
            c.execute(f"CREATE TABLE {table}(generation INTEGER,proof_id TEXT)")
        c.execute("CREATE TABLE fills(fill_id TEXT PRIMARY KEY,broker_order_id TEXT,symbol TEXT,"
                  "side TEXT,quantity TEXT,price TEXT,occurred_at TEXT,fee TEXT,fee_source TEXT)")
        c.execute("CREATE TABLE broker_orders(broker_order_id TEXT PRIMARY KEY,"
                  "client_order_id TEXT,"
                  "symbol TEXT,side TEXT,filled_quantity TEXT,submitted_at TEXT)")
        c.execute("CREATE TABLE order_intents(intent_id TEXT,client_order_id TEXT)")
        c.execute("CREATE TABLE core_intent_links(intent_id TEXT)")
        put(c, "core_identities", dict(account_id="paper", evidence_lane="BROKER_PAPER"))
        account = dict(account_id="paper", currency="USD", cash="100.00")
        origin = dict(account=account, endpoint=ENDPOINT, provider="alpaca", captured_at=BASE)
        baseline = dict(account=account, endpoint=ENDPOINT, provider="alpaca", captured_at=BASE,
                        source="BROKER_PAPER_APPROVED_INITIAL_STATE", quantities=[["OLD", "2"]],
                        provider_acquisition=origin, source_ids=[digest(origin)])
        baseline_id = put(c, "core_fixture_baselines", baseline)
        raw = []
        for i, qty, cumulative in ((1, "0.5", "0.5"), (2, "0.5", "1")):
            raw.append(dict(id=f"old-{i}", activity_type="FILL", order_id="inherited",
                            symbol="OLD", side="sell", qty=qty, cum_qty=cumulative,
                            price="12.005" if fault == "tie" else "12.006",
                            transaction_time=f"2026-11-05T15:00:0{3-i}+00:00"))
        fee = dict(id="fee", activity_type="FEE", created_at="2026-11-06T16:00:00+00:00",
                   date="2026-11-06", status="executed", currency="USD", net_amount="-0.01")
        if fault != "no_fee":
            raw.append(fee)
        document = dict(account_id="paper", endpoint=ENDPOINT, after=BASE, until=AT, rows=raw)
        if fault == "account":
            document["account_id"] = "foreign"
        if fault == "future_source":
            document["until"] = "2026-11-06T18:00:01+00:00"
        if fault == "prefix":
            raw[0]["cum_qty"] = "1.5"
        if fault == "duplicate":
            raw.append(dict(raw[0]))
        source_id = digest(document)
        if fault != "missing":
            put(c, "core_provider_acquisitions", document, source_id)
        for row in raw[:2]:
            put(c, "core_external_owner_events", dict(account_id="paper", event_id=row["id"],
                owner="legacy", symbol="OLD", order_id="inherited", quantity_delta="-0.5",
                occurred_at=row["transaction_time"], net_cash=None, source_document=source_id))
        if fault != "no_fee":
            put(c, "core_external_owner_events", dict(account_id="paper", event_id="fee",
                owner="ACCOUNT_NONTRADE", symbol="", quantity_delta="0", net_cash="-0.01",
                activity_type="FEE", occurred_at=fee["created_at"], source_document=source_id))
        c.execute("INSERT INTO fills VALUES "
                  "('buy','owned','NEW','BUY','1','10.004',?,NULL,'UNKNOWN')",
                  ("2026-11-06T15:00:00+00:00",))
        c.execute("INSERT INTO broker_orders VALUES ('owned','client','NEW','BUY','1',?)", (BASE,))
        c.execute("INSERT INTO broker_orders VALUES ('inherited','legacy','OLD','SELL','1',?)",
                  ("2026-11-04T15:00:00+00:00" if fault == "baseline_crossing" else BASE,))
        if fault == "order_missing":
            c.execute("DELETE FROM broker_orders WHERE broker_order_id='inherited'")
        c.execute("INSERT INTO order_intents VALUES ('intent','client')")
        c.execute("INSERT INTO core_intent_links VALUES ('intent')")
        if fault == "stale":
            c.execute("INSERT INTO fills VALUES "
                      "('later','owned','NEW','BUY','1','10',?,NULL,'UNKNOWN')",
                      ("2026-11-06T17:00:00+00:00",))
        cash = "102.01" if fault in {"mismatch", "no_fee"} else "102.00"
        proof = dict(account=dict(account, cash=cash),
                     recorded_at_utc=AT, complete=True, cash_complete=False, expected_cash=None,
                     baseline_id=baseline_id, generation=1, evidence_lane="BROKER_PAPER",
                     fill_ids=["buy"], order_ids=["owned"],
                     expected_quantities=[["NEW", "1"], ["OLD", "1"]],
                     actual_quantities=[["NEW", "1"], ["OLD", "1"]])
        if fault == "positions":
            proof["actual_quantities"] = [["NEW", "1"]]
        proof_id = put(c, "core_reconciliation_proofs", proof)
        c.execute("INSERT INTO core_quantity_certifications VALUES (1,?)", (proof_id,))
        if fault == "tampered":
            proof["account"]["cash"] = "999"
            c.execute("UPDATE core_reconciliation_proofs SET payload=?", (json.dumps(proof),))
        # An uncertified newer row must never replace the recorded certificate.
        put(c, "core_reconciliation_proofs", dict(proof, generation=900, complete=False))


def test_posted_cash_matches_with_inherited_partial_sale_open_owned_lot_and_unknown_fee(tmp_path):
    path = tmp_path / "cash.db"
    fixture(path)
    before = path.read_bytes()
    result = read_report(path, as_of=AS_OF)["cash_reconciliation"]
    assert result["available"] and result["status"] == "MATCHED_RETAINED_MOVEMENTS"
    assert result["coverage_complete"] is False
    assert result["calculated_cash"] == "102.00"  # 100 + round(12.006) - round(10.004) - .01
    assert result["residual"] == "0.00" and result["positions_match"]
    assert result["posted_fee_cash_flow"] == "-0.01"
    assert result["unknown_fee_fill_count"] == 1
    assert result["unknown_inherited_net_cash_count"] == 2
    assert result["exact_cash_complete"] is False and result["fee_finality"] == "UNKNOWN"
    assert not result["actual_cost_authority"] and not result["learning_label_authority"]
    assert path.read_bytes() == before


def test_cash_difference_is_not_a_balancing_adjustment(tmp_path):
    path = tmp_path / "cash.db"
    fixture(path, "mismatch")
    result = read_report(path, as_of=AS_OF)["cash_reconciliation"]
    assert result["available"] and result["status"] == "MISMATCH"
    assert result["calculated_cash"] == "102.00" and result["residual"] == "0.01"


def test_no_posted_fee_evidence_remains_unknown_even_when_retained_cash_matches(tmp_path):
    path = tmp_path / "cash.db"
    fixture(path, "no_fee")
    result = read_report(path, as_of=AS_OF)["cash_reconciliation"]
    assert result["status"] == "MATCHED_RETAINED_MOVEMENTS"
    assert result["posted_fee_cash_flow"] is None and result["posted_fee_activity_count"] == 0


def test_old_recorded_snapshot_is_explicitly_stale_without_relabeling_history(tmp_path):
    from datetime import timedelta

    path = tmp_path / "cash.db"
    fixture(path)
    result = read_report(path, as_of=AS_OF + timedelta(seconds=121))["cash_reconciliation"]
    assert result["snapshot_stale"] is True
    assert result["status"] == "MATCHED_RETAINED_MOVEMENTS"
    assert "RECORDED_SNAPSHOT_OLDER_THAN_FUNDS_VALIDITY_WINDOW" in result["reasons"]


@pytest.mark.parametrize("fault", ["missing", "tampered", "account", "future_source", "stale",
                                   "duplicate", "prefix", "positions", "tie", "baseline_crossing",
                                   "order_missing"])
def test_missing_or_contradictory_scope_never_silently_matches(tmp_path, fault):
    path = tmp_path / "cash.db"
    fixture(path, fault)
    # The daily fee reader already rejects missing activity documents. Cash's
    # independent reader must classify its own failures without changing evidence.
    from quantbot.reporting import paper_cash
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        result = paper_cash.read_cash_reconciliation(
            lambda sql, params=(): list(c.execute(sql, params)), account_id="paper", as_of=AS_OF)
    assert not result["available"] and result["status"] == "UNAVAILABLE"
    assert result["calculated_cash"] is None and result["reasons"]
