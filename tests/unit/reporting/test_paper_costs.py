"""Daily account fees are not invented per-fill fees or a second spread charge."""

import hashlib
import importlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime

import pytest


def module():
    return importlib.import_module("quantbot.reporting.paper_costs")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def fixture():
    fills = []
    trips = []
    rows = []
    sessions = []
    for day in (27, 28):
        ids = []
        for side, price, hour in [("BUY", "10", 14), ("SELL", "10.1", 15)]:
            identity = f"{day}-{side}"
            ids.append(identity)
            fills.append(
                dict(
                    fill_id=identity,
                    broker_order_id=f"order-{identity}",
                    symbol="TEST",
                    side=side,
                    quantity="0.1",
                    price=price,
                    occurred_at=f"2026-10-{day}T{hour}:00:00Z",
                    fee=None,
                    fee_source="UNKNOWN",
                )
            )
            rows.append(
                dict(
                    id=identity,
                    activity_type="FILL",
                    order_id=f"order-{identity}",
                    symbol="TEST",
                    side=side.lower(),
                    qty="0.1",
                    price=price,
                    transaction_time=f"2026-10-{day}T{hour}:00:00Z",
                )
            )
        trips.append(
            dict(
                lot_id=f"lot-{day}",
                entry_fill_ids=[ids[0]],
                exit_fill_ids=[ids[1]],
                gross_pnl="0.01",
                actual_fees=None,
                net_pnl=None,
                source_end=f"2026-10-{day}T15:00:00Z",
                evidence_lane="BROKER_PAPER",
            )
        )
        sessions.append(
            dict(
                session_date=f"2026-10-{day}",
                session_open=f"2026-10-{day}T13:30:00Z",
                session_close=f"2026-10-{day}T20:00:00Z",
                equity="495.18",
            )
        )
    for subtype in ("REG", "CAT"):
        rows.append(
            dict(
                id=f"fee-27-{subtype}",
                activity_type="FEE",
                activity_sub_type=subtype,
                date="2026-10-27",
                created_at="2026-10-28T00:15:00Z",
                net_amount="-0.01",
                currency="USD",
                status="executed",
            )
        )
    doc = dict(
        account_id="paper",
        endpoint="https://paper-api.alpaca.markets",
        after="2026-10-27T00:00:00Z",
        until="2026-10-28T22:00:00Z",
        rows=rows,
    )
    return dict(
        trips=trips,
        fills=fills,
        documents=[dict(id=digest(doc), document=doc)],
        sessions=sessions,
        account_id="paper",
        as_of=datetime(2026, 10, 28, 22, tzinfo=UTC),
    )


def report(data):
    return module().build_report(**data)


def test_posted_daily_fees_and_unposted_day_have_separate_truths():
    data = fixture()
    result = report(data)
    prior, today = result["days"]
    assert prior["observed_fee_cash_flow"] == "-0.02"
    assert prior["net_after_observed_fees"] == "-0.01"
    assert today["observed_fee_cash_flow"] is None
    assert today["net_after_observed_fees"] is None
    assert today["fee_scenario"]["modeled_net"] == "-0.01"
    assert today["fee_scenario"]["reference_session"] == "2026-10-27"
    assert today["all_in_verified_net"] is None
    assert today["entry_notional"] == "1.0"
    assert today["opening_equity"] == "495.18"
    assert all(t["net_pnl"] is None for t in data["trips"])


def test_overlapping_documents_do_not_double_count_and_partial_bill_subtracts_only_remainder():
    data = fixture()
    doc = deepcopy(data["documents"][0]["document"])
    doc["rows"].append(
        dict(
            id="fee-28-CAT",
            activity_type="FEE",
            activity_sub_type="CAT",
            date="2026-10-28",
            created_at="2026-10-28T21:00:00Z",
            net_amount="-0.01",
            currency="USD",
            status="executed",
        )
    )
    data["documents"].append(dict(id=digest(doc), document=doc))
    today = report(data)["days"][1]
    assert today["observed_fee_cash_flow"] == "-0.01"
    assert today["net_after_observed_fees"] == "0.00"
    assert today["fee_scenario"]["modeled_additional_fee"] == "0.01"
    assert today["fee_scenario"]["modeled_net"] == "-0.01"


def test_future_fee_is_not_available_and_next_report_incorporates_it():
    data = fixture()
    doc = data["documents"][0]["document"]
    doc["rows"].append(
        dict(
            id="future-fee",
            activity_type="FEE",
            date="2026-10-28",
            created_at="2026-10-29T00:15:00Z",
            net_amount="-0.02",
            currency="USD",
            status="executed",
        )
    )
    data["documents"][0]["id"] = digest(doc)
    assert report(data)["days"][1]["observed_fee_cash_flow"] is None
    data["as_of"] = datetime(2026, 10, 29, 1, tzinfo=UTC)
    assert report(data)["days"][1]["observed_fee_cash_flow"] == "-0.02"


@pytest.mark.parametrize("fault", ["foreign_fill", "incomplete_window", "different_account"])
def test_account_fees_cannot_be_called_owned_net_without_complete_matching_scope(fault):
    data = fixture()
    doc = data["documents"][0]["document"]
    if fault == "foreign_fill":
        foreign = deepcopy(doc["rows"][0])
        foreign["id"] = "foreign"
        doc["rows"].append(foreign)
    elif fault == "incomplete_window":
        doc["after"] = "2026-10-27T14:30:00Z"
    else:
        doc["account_id"] = "other"
    data["documents"][0]["id"] = digest(doc)
    prior = report(data)["days"][0]
    assert prior["net_after_observed_fees"] is None
    assert prior["fee_scenario"] is None
    assert prior["scope_verified"] is False


def test_conflicting_fee_id_and_tampered_source_fail_closed():
    data = fixture()
    doc = deepcopy(data["documents"][0]["document"])
    doc["rows"][-1]["net_amount"] = "-0.03"
    data["documents"].append(dict(id=digest(doc), document=doc))
    with pytest.raises(ValueError, match="CONFLICTING_ACTIVITY"):
        report(data)
    data = fixture()
    data["documents"][0]["document"]["rows"][-1]["net_amount"] = "-0.03"
    with pytest.raises(ValueError, match="SOURCE_DIGEST"):
        report(data)


def test_later_partial_document_with_foreign_fill_invalidates_earlier_complete_scope():
    data = fixture()
    later = deepcopy(data["documents"][0]["document"])
    later["after"] = "2026-10-27T14:30:00Z"
    later["rows"] = [deepcopy(later["rows"][0])]
    later["rows"][0]["id"] = "later-foreign-fill"
    data["documents"].append(dict(id=digest(later), document=later))
    assert report(data)["days"][0]["scope_verified"] is False


def test_fee_absence_has_capture_timestamp_and_lag_not_implied_live_freshness():
    data = fixture()
    data["as_of"] = datetime(2026, 10, 28, 23, tzinfo=UTC)
    today = report(data)["days"][1]
    assert today["activity_capture_until"] == "2026-10-28T22:00:00+00:00"
    assert today["activity_capture_lag_seconds"] == 3600


def test_cost_panel_identifies_observed_but_unverified_fee_activities():
    from types import SimpleNamespace

    from quantbot.console.app import TEMPLATES

    data = fixture()
    document = data["documents"][0]["document"]
    for row in document["rows"]:
        if row.get("activity_type") == "FEE":
            row["status"] = "pending"
    data["documents"][0]["id"] = digest(document)
    costs = report(data)
    assert costs["days"][0]["invalid_fee_activity_ids"]
    html = TEMPLATES.get_template("_paper_costs.html").render(
        state=SimpleNamespace(paper_costs={"available": True, **costs})
    )
    first_day = html.split("<td>2026-10-27</td>", 1)[1].split("</tr>", 1)[0]
    assert "Fee activity observed, but its status or details are unverified" in first_day
    assert "No fee activity observed by this capture" not in first_day


def test_refund_is_a_signed_observed_credit_and_not_a_modeled_cost():
    data = fixture()
    doc = data["documents"][0]["document"]
    doc["rows"].append(
        dict(
            id="refund",
            activity_type="FEE",
            date="2026-10-28",
            created_at="2026-10-28T21:00:00Z",
            net_amount="0.01",
            currency="USD",
            status="executed",
        )
    )
    data["documents"][0]["id"] = digest(doc)
    today = report(data)["days"][1]
    assert today["observed_fee_cash_flow"] == "0.01"
    assert today["net_after_observed_fees"] == "0.02"
    assert today["fee_scenario"] is None


def test_gross_is_fill_economics_without_an_added_spread_or_slippage_charge():
    data = fixture()
    result = report(data)
    assert result["days"][0]["gross_realized"] == "0.01"
    assert result["days"][0]["net_after_observed_fees"] == "-0.01"
    assert (
        "spread" not in result["days"][0]["fee_scenario"]
        if result["days"][0]["fee_scenario"]
        else True
    )
    data["trips"][0]["gross_pnl"] = "1.87"
    with pytest.raises(ValueError, match="GROSS_FILL_MISMATCH"):
        report(data)


def database_fixture(path, data):
    with sqlite3.connect(path) as c:
        for name in (
            "core_identities",
            "core_round_trips",
            "core_external_owner_events",
            "core_provider_acquisitions",
            "core_session_openings",
            "core_time_evidence",
        ):
            c.execute(f"CREATE TABLE {name}(id TEXT PRIMARY KEY,payload TEXT)")
        c.execute("CREATE TABLE core_provider_progress(document_id TEXT)")
        c.execute("CREATE TABLE core_session_opening_index(document_id TEXT)")
        c.execute(
            "CREATE TABLE fills(fill_id TEXT PRIMARY KEY,broker_order_id TEXT,symbol TEXT,"
            "side TEXT,quantity TEXT,price TEXT,occurred_at TEXT,fee TEXT,fee_source TEXT)"
        )
        c.execute(
            "INSERT INTO core_identities VALUES (?,?)",
            ("identity", json.dumps(dict(account_id="paper", evidence_lane="BROKER_PAPER"))),
        )
        for t in data["trips"]:
            c.execute("INSERT INTO core_round_trips VALUES (?,?)", (t["lot_id"], json.dumps(t)))
        for f in data["fills"]:
            c.execute(
                "INSERT INTO fills VALUES (?,?,?,?,?,?,?,?,?)",
                tuple(
                    f[k]
                    for k in (
                        "fill_id",
                        "broker_order_id",
                        "symbol",
                        "side",
                        "quantity",
                        "price",
                        "occurred_at",
                        "fee",
                        "fee_source",
                    )
                ),
            )
        for d in data["documents"]:
            c.execute(
                "INSERT INTO core_provider_acquisitions VALUES (?,?)",
                (d["id"], json.dumps(d["document"])),
            )
        c.execute("INSERT INTO core_provider_progress VALUES (?)", (data["documents"][0]["id"],))
        for s in data["sessions"]:
            timing = dict(
                objections=[],
                samples=[
                    dict(
                        provider=dict(
                            provider="alpaca",
                            account_id="paper",
                            timestamp=s["session_open"],
                            session_id=s["session_date"],
                            session_open=s["session_open"],
                            session_close=s["session_close"],
                        )
                    ),
                    dict(
                        provider=dict(
                            provider="alpaca",
                            account_id="paper",
                            timestamp=s["session_open"].replace("13:30:00", "13:30:01"),
                            session_id=s["session_date"],
                            session_open=s["session_open"],
                            session_close=s["session_close"],
                        )
                    ),
                ],
            )
            timing_id = digest(timing)
            opening = dict(s, time_evidence_id=timing_id, account_id="paper")
            opening_id = digest(opening)
            c.execute("INSERT INTO core_session_opening_index VALUES (?)", (opening_id,))
            c.execute(
                "INSERT INTO core_session_openings VALUES (?,?)",
                (opening_id, json.dumps(opening)),
            )
            c.execute(
                "INSERT INTO core_time_evidence VALUES (?,?)",
                (timing_id, json.dumps(timing)),
            )


def test_query_only_reader_is_repeatable_and_late_posting_never_rewrites_native_outcomes(tmp_path):
    data = fixture()
    path = tmp_path / "paper.db"
    database_fixture(path, data)
    before = path.read_bytes()
    first = module().read_report(path, as_of=data["as_of"])
    assert module().read_report(path, as_of=data["as_of"]) == first
    assert path.read_bytes() == before
    later = deepcopy(data["documents"][0]["document"])
    later["until"] = "2026-10-29T01:00:00Z"
    later["rows"].append(
        dict(
            id="late-fee",
            activity_type="FEE",
            date="2026-10-28",
            created_at="2026-10-29T00:15:00Z",
            net_amount="-0.02",
            currency="USD",
            status="executed",
        )
    )
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO core_provider_acquisitions VALUES (?,?)",
            (digest(later), json.dumps(later)),
        )
        c.execute("UPDATE core_provider_progress SET document_id=?", (digest(later),))
        native_before = c.execute("SELECT * FROM core_round_trips ORDER BY id").fetchall()
    updated = module().read_report(path, as_of=datetime(2026, 10, 29, 1, tzinfo=UTC))
    assert updated["days"][1]["net_after_observed_fees"] == "-0.01"
    assert updated["days"][1]["fee_scenario"] is None
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT * FROM core_round_trips ORDER BY id").fetchall() == native_before


@pytest.mark.parametrize("budget", ["rows", "bytes", "time"])
def test_cost_read_budget_refuses_incomplete_history_without_changing_evidence(tmp_path, budget):
    data = fixture()
    path = tmp_path / "paper.db"
    database_fixture(path, data)
    with sqlite3.connect(path) as c:
        for index in range(100):
            c.execute(
                "INSERT INTO core_external_owner_events VALUES (?,?)",
                (f"unrelated-{index}", json.dumps({"activity_type": "OTHER", "id": index})),
            )
    before = path.read_bytes()
    limits = {
        "rows": {"max_rows": 25},
        "bytes": {"max_bytes": 100},
        "time": {"max_read_seconds": 0},
    }[budget]
    with pytest.raises(ValueError, match="PAPER_COST_READ_BUDGET_EXCEEDED"):
        module().read_report(path, as_of=data["as_of"], **limits)
    assert path.read_bytes() == before


def test_cost_deadline_expiring_during_history_read_never_returns_partial_days(
    tmp_path, monkeypatch
):
    import itertools

    data = fixture()
    path = tmp_path / "paper.db"
    database_fixture(path, data)
    with sqlite3.connect(path) as c:
        for index in range(100):
            c.execute(
                "INSERT INTO core_external_owner_events VALUES (?,?)",
                (f"other-{index}", json.dumps({"activity_type": "OTHER", "id": index})),
            )
    ticks = itertools.count()
    monkeypatch.setattr(module().time, "monotonic", lambda: next(ticks) / 1000)
    with pytest.raises(ValueError, match="PAPER_COST_READ_BUDGET_EXCEEDED"):
        module().read_report(path, as_of=data["as_of"], max_read_seconds=0.03)
    assert next(ticks) > 8  # Budget expired after the reader had begun loading rows.


def test_cli_refuses_existing_output_and_nonpaper_database(tmp_path, monkeypatch):
    data = fixture()
    path = tmp_path / "paper.db"
    database_fixture(path, data)
    output = tmp_path / "report.json"
    output.write_text("original")
    monkeypatch.setattr(
        "sys.argv", ["paper_costs", "--database", str(path), "--output", str(output)]
    )
    with pytest.raises(FileExistsError):
        module().main()
    assert output.read_text() == "original"
    with sqlite3.connect(path) as c:
        c.execute(
            "UPDATE core_identities SET payload=?",
            (json.dumps(dict(account_id="paper", evidence_lane="LIVE")),),
        )
    with pytest.raises(ValueError, match="PAPER_ONLY"):
        module().read_report(path, as_of=data["as_of"])


@pytest.mark.parametrize("table_name", ["core_time_evidence", "core_session_openings"])
def test_reader_rejects_tampered_calendar_source(tmp_path, table_name):
    data = fixture()
    path = tmp_path / "paper.db"
    database_fixture(path, data)
    with sqlite3.connect(path) as c:
        identity, payload = c.execute(f"SELECT id,payload FROM {table_name} LIMIT 1").fetchone()
        document = json.loads(payload)
        if table_name == "core_time_evidence":
            document["samples"][-1]["provider"]["session_close"] = "2026-10-27T14:00:00Z"
        else:
            document["equity"] = "999"
        c.execute(f"UPDATE {table_name} SET payload=? WHERE id=?", (json.dumps(document), identity))
    with pytest.raises(ValueError, match="SOURCE_DIGEST"):
        module().read_report(path, as_of=data["as_of"])
