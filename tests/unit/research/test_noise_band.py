from scripts.noise_band_research import choose_trade


def session(signal=100, later=100):
    return {
        "open": 100,
        "previous_close": 100,
        "prices": {0: signal, 1: later},
        "fills": {0: 101, 1: 102},
        "final": 103,
    }


def test_band_uses_only_prior_sessions_and_delayed_price():
    history = [session(100, 100) for _ in range(14)]
    selected = choose_trade(session(102, 103), history)
    assert selected["entry"] == 101
    assert selected["exit"] == 103
    assert selected["entry_check"] == 0


def test_normal_move_does_not_trigger():
    history = [session(102, 102) for _ in range(14)]
    assert choose_trade(session(101, 101), history) is None


def test_previous_close_anchors_gap_down_band():
    current = session(101, 101)
    current["previous_close"] = 103
    assert choose_trade(current, [session() for _ in range(14)]) is None


def test_stop_at_next_check_fills_later_and_no_reentry():
    selected = choose_trade(session(102, 99), [session() for _ in range(14)])
    assert selected["exit"] == 102
    assert selected["exit_check"] == 1


def test_fourteen_immediate_sessions_required():
    assert choose_trade(session(105, 105), [session() for _ in range(13)]) is None


def test_boundary_equality_is_not_entry():
    assert choose_trade(session(), [session() for _ in range(14)]) is None


def panel():
    from datetime import datetime, timedelta

    from scripts.noise_band_research import KEEP, ledger

    dates = [(datetime(2026, 3, 2) + timedelta(days=i)).date().isoformat() for i in range(17)]
    content = dict(
        calendar=[dict(date=d, open="09:30", close="16:00") for d in dates],
        raw=[dict(t=d + "T05:00:00Z", c=100) for d in dates],
        adjusted=[dict(t=d + "T05:00:00Z", c=100) for d in dates],
        corporate_actions={},
    )
    minutes = []
    for i, d in enumerate(dates):
        for clock in sorted(KEEP):
            price = 102 if i == 15 and clock != "09:30" else 100
            stamp = datetime.fromisoformat(d + "T" + clock).replace(tzinfo=ledger.NY)
            minutes.append(dict(t=stamp.isoformat(), o=price, c=price, h=price, l=price))
    return {"content": content}, minutes


def test_lookback_does_not_skip_missing_sessions_or_read_future():
    from scripts.noise_band_research import construct_rows

    parent, minutes = panel()
    rows, reference, chosen = construct_rows(parent, minutes)
    assert rows[15]["eligible"]
    assert rows[15]["date"] in chosen
    assert reference[15]["entry"] == "102"
    last_day = rows[16]["date"]
    changed = [
        b | dict(o=500, c=500, h=500, l=500) if b["t"].startswith(last_day) else b for b in minutes
    ]
    assert construct_rows(parent, changed)[2][rows[15]["date"]] == chosen[rows[15]["date"]]
    missing = [
        b for b in minutes if not (b["t"].startswith(rows[10]["date"]) and "10:31" in b["t"])
    ]
    assert construct_rows(parent, missing)[0][15]["exclusion"] == "incomplete_14_session_lookback"


def files(tmp_path):
    import json

    from scripts import noise_band_research as subject

    parent, minutes = panel()
    parent["content"].update(
        rule=subject.ledger.RULE,
        start=subject.ledger.START,
        end=subject.ledger.END,
        feed="sip",
        minutes=minutes,
    )
    parent["content_hash"] = subject.ledger.digest(parent["content"])
    parent_path = tmp_path / "parent.json"
    parent_path.write_text(json.dumps(parent))
    content = dict(rule=subject.RULE, parent_input_hash=parent["content_hash"], minutes=minutes)
    bundle = tmp_path / "bundle.json"
    bundle.write_text(
        json.dumps(dict(content=content, content_hash=subject.ledger.digest(content)))
    )
    audit = tmp_path / "audit.json"
    audit.write_text(
        json.dumps(
            dict(
                input_hash=parent["content_hash"],
                differences={},
                library="exchange_calendars",
                expected_sessions=17,
                actual_sessions=17,
            )
        )
    )
    return parent_path, bundle, audit


def test_bad_audit_refuses_before_creating_database(tmp_path):
    import pytest
    from scripts import noise_band_research as subject

    parent, bundle, audit = files(tmp_path)
    audit.write_text("{}")
    with pytest.raises(ValueError, match="identity differs"):
        subject.measure(parent, bundle, audit, tmp_path / "missing.db", tmp_path / "result.json")
    assert not (tmp_path / "missing.db").exists()


def test_charge_precedes_signal_construction_and_failed_retry_is_blocked(tmp_path, monkeypatch):
    import pytest
    from scripts import noise_band_research as subject

    from quantbot.research.registry import HypothesisRegistry
    from quantbot.storage import Database

    parent, bundle, audit = files(tmp_path)
    database = tmp_path / "research.db"
    with Database.create(database) as db:
        with db.transaction() as s:
            before = HypothesisRegistry(s).cumulative_trials()

    def fail(*args):
        raise RuntimeError("signal engine interrupted")

    monkeypatch.setattr(subject, "construct_rows", fail)
    output = tmp_path / "result.json"
    with pytest.raises(RuntimeError, match="signal engine"):
        subject.measure(parent, bundle, audit, database, output)
    assert output.with_suffix(".manifest.json").exists()
    assert not output.exists()
    with pytest.raises(ValueError, match="no automatic retry"):
        subject.measure(parent, bundle, audit, database, output)
    with Database(database) as db:
        with db.transaction() as s:
            assert HypothesisRegistry(s).cumulative_trials() == before + 1
