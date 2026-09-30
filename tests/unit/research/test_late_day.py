from decimal import Decimal

import pytest
from scripts.late_day_research import build_rows, simulate


def day(index: int, **changes: object) -> dict:
    result = dict(
        index=index,
        date=f"2026-03-{index + 2:02}",
        signal_close="101",
        previous_close="100",
        entry="100",
        exit="102",
        eligible=True,
        exclusion="",
    )
    result.update(changes)
    return result


def test_delayed_entry_and_fractional_sizing():
    result = simulate([day(0)], 5)
    trade = result["trades"][0]
    assert Decimal(trade["quantity"]) == Decimal("0.449775")
    assert Decimal(trade["buy_price"]) == Decimal("100.05")
    assert Decimal(trade["sell_price"]) == Decimal("101.949")
    assert Decimal(result["ending_equity"]) == Decimal("500.854122725")


@pytest.mark.parametrize("signal", ["99", "100"])
def test_nonpositive_signal_stays_cash_but_benchmark_trades(signal):
    result = simulate([day(0, signal_close=signal)], 5)
    assert result["trades"] == []
    assert Decimal(result["ending_equity"]) == 500
    assert Decimal(result["benchmark_ending_equity"]) > 500


def test_zero_price_move_loses_round_trip_cost():
    result = simulate([day(0, exit="100")], 5)
    assert Decimal(result["trades"][0]["pnl"]) < 0
    assert Decimal(result["ending_equity"]) < 500


def test_excluded_day_has_no_strategy_or_benchmark_trade():
    result = simulate([day(0, eligible=False, exclusion="corporate_action")], 5)
    assert result["trades"] == []
    assert result["eligible_sessions"] == 0
    assert Decimal(result["benchmark_ending_equity"]) == 500


def test_every_day_accounting_includes_cash_and_unsettled_proceeds():
    result = simulate([day(i) for i in range(12)], 5)
    assert len(result["trades"]) == 12
    assert all(
        Decimal(t["notional"]) <= Decimal(t["equity_before"]) * Decimal(".09")
        for t in result["trades"]
    )
    assert Decimal(result["ending_equity"]) == Decimal("500") + sum(
        (Decimal(t["pnl"]) for t in result["trades"]), Decimal(0)
    )
    assert result["minimum_available_cash"] >= 0
    assert result["daily"][0]["unsettled_proceeds"] > 0
    assert Decimal(result["ending_equity"]) == Decimal(result["benchmark_ending_equity"])


@pytest.mark.parametrize("bad", ["0", "-1", "NaN"])
def test_invalid_price_refuses_measurement(bad):
    with pytest.raises(ValueError):
        simulate([day(0, entry=bad)], 5)


def test_duplicate_or_unordered_sessions_refuse():
    with pytest.raises(ValueError):
        simulate([day(0), day(0)], 5)


def test_only_frozen_cost_cases():
    with pytest.raises(ValueError):
        simulate([day(0)], 0)


def bundle():
    return dict(
        calendar=[
            dict(date="2026-03-02", open="09:30", close="16:00"),
            dict(date="2026-03-03", open="09:30", close="16:00"),
        ],
        raw=[dict(t="2026-03-02T05:00:00Z", c=100), dict(t="2026-03-03T05:00:00Z", c=105)],
        adjusted=[dict(t="2026-03-02T05:00:00Z", c=100), dict(t="2026-03-03T05:00:00Z", c=105)],
        minutes=[
            dict(
                t=f"2026-03-03T20:{m}:00Z",
                o=price,
                c=close,
                h=max(price, close),
                l=min(price, close),
            )
            for m, price, close in [("29", 98, 101), ("31", 102, 99), ("59", 103, 105)]
        ],
        corporate_actions={},
    )


def test_exact_minute_open_and_signal_close_mapping():
    rows = build_rows(bundle())
    assert not rows[0]["eligible"]
    assert rows[1]["signal_close"] == "101"
    assert rows[1]["previous_close"] == "100"
    assert rows[1]["entry"] == "102"
    assert rows[1]["exit"] == "103"


@pytest.mark.parametrize(
    "reason",
    [
        "missing_required_minute",
        "corporate_action",
        "adjustment_factor_change",
        "short_or_nonstandard_session",
    ],
)
def test_data_exclusions(reason):
    data = bundle()
    if reason == "missing_required_minute":
        data["minutes"].pop()
    elif reason == "corporate_action":
        data["corporate_actions"] = {"cash_dividends": [{"ex_date": "2026-03-03"}]}
    elif reason == "adjustment_factor_change":
        data["adjusted"][0]["c"] = 99
    else:
        data["calendar"][1]["close"] = "13:00"
    assert build_rows(data)[1]["exclusion"] == reason


def test_future_calendar_is_refused():
    data = bundle()
    data["calendar"][1]["date"] = "2026-09-14"
    with pytest.raises(ValueError, match="frozen range"):
        build_rows(data)


def test_duplicate_minute_is_refused():
    data = bundle()
    data["minutes"].append(data["minutes"][0])
    with pytest.raises(ValueError, match="duplicate"):
        build_rows(data)


def test_cent_rounding_is_not_a_corporate_action():
    data = bundle()
    data["raw"][0]["c"] = 201.36
    data["adjusted"][0]["c"] = 171.39
    data["raw"][1]["c"] = 198.82
    data["adjusted"][1]["c"] = 169.23
    assert build_rows(data)[1]["eligible"]


def test_missing_calendar_session_refuses_prior_close_substitution():
    data = bundle()
    data["calendar"].pop(0)
    with pytest.raises(ValueError, match="daily bar missing from calendar"):
        build_rows(data)


def test_naive_minute_timestamp_refuses_host_timezone_guess():
    data = bundle()
    data["minutes"][0]["t"] = data["minutes"][0]["t"].replace("Z", "")
    with pytest.raises(ValueError, match="explicit timezone"):
        build_rows(data)


def measurement_files(tmp_path):
    import json

    from scripts import late_day_research as subject

    content = bundle() | dict(rule=subject.RULE, start=subject.START, end=subject.END, feed="sip")
    content_hash = subject.digest(content)
    source = tmp_path / "inputs.json"
    source.write_text(json.dumps(dict(content=content, content_hash=content_hash)))
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            dict(
                input_hash=content_hash,
                differences={},
                expected_sessions=2,
                actual_sessions=2,
                library="exchange_calendars",
            )
        )
    )
    return source, audit


def test_mismatched_calendar_audit_refuses_before_database_access(tmp_path):
    from scripts import late_day_research as subject

    source, audit = measurement_files(tmp_path)
    audit.write_text("{}")
    with pytest.raises(ValueError, match="calendar audit"):
        subject.measure(source, tmp_path / "nonexistent.db", tmp_path / "result.json", audit)
    assert not (tmp_path / "nonexistent.db").exists()


def test_interrupted_measurement_retains_trial_and_refuses_retry(tmp_path, monkeypatch):
    from scripts import late_day_research as subject

    from quantbot.research.registry import HypothesisRegistry
    from quantbot.storage import Database

    source, audit = measurement_files(tmp_path)
    database = tmp_path / "research.db"
    with Database.create(database) as db:
        with db.transaction() as s:
            before = HypothesisRegistry(s).cumulative_trials()
    calls = []

    def fail(*args):
        calls.append(True)
        raise RuntimeError("intentional engine interruption")

    monkeypatch.setattr(subject, "simulate", fail)
    output = tmp_path / "result.json"
    with pytest.raises(RuntimeError, match="intentional"):
        subject.measure(source, database, output, audit)
    assert output.with_suffix(".manifest.json").exists()
    assert not output.exists()
    with pytest.raises(ValueError, match="no automatic retry"):
        subject.measure(source, database, output, audit)
    assert len(calls) == 1
    with Database(database) as db:
        with db.transaction() as s:
            assert HypothesisRegistry(s).cumulative_trials() == before + 1
