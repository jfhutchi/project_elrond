from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.operations.daily_performance import (
    CloseEvidence,
    CloseLedger,
    evaluate_day,
    summarize,
)


def evidence(*, end="1010", flow="0", known=True, reset=False, captured=None):
    return CloseEvidence(
        session_date=date(2026, 11, 27),
        close_at=datetime(2026, 11, 27, 18, tzinfo=UTC),
        captured_at=captured or datetime(2026, 11, 27, 18, 1, tzinfo=UTC),
        previous_close_equity=Decimal("1000"), close_equity=Decimal(end),
        previous_session_date=date(2026, 11, 25),
        expected_previous_session_date=date(2026, 11, 25),
        net_external_flow=Decimal(flow), flows_verified=known,
        same_account_epoch=not reset, reconciled=True,
    )


def test_deposit_is_not_profit_and_early_close_is_authoritative():
    day = evaluate_day(evidence(end="1110", flow="100"))
    assert day.status == "FINAL"
    assert day.net_pnl == Decimal("10")


def test_reset_or_unknown_flows_are_excluded():
    assert evaluate_day(evidence(reset=True)).status == "INVALID"
    assert evaluate_day(evidence(known=False)).net_pnl is None


def test_missing_baseline_is_not_zero():
    from dataclasses import replace
    assert evaluate_day(replace(evidence(), previous_close_equity=None)).status == "MISSING"


def test_preclose_and_late_snapshots_cannot_finalize():
    pending = evidence(captured=datetime(2026, 11, 27, 17, 59, tzinfo=UTC))
    late = evidence(captured=datetime(2026, 11, 27, 19, tzinfo=UTC))
    assert evaluate_day(pending).status == "PENDING"
    assert evaluate_day(late).status == "INVALID"


def test_percentage_includes_flat_and_excludes_invalid():
    from dataclasses import replace
    days = [replace(evaluate_day(evidence(end=x)), session_date=date(2026, 11, i))
            for i, x in enumerate(("1010", "990", "1000"), start=23)]
    summary = summarize([*days, evaluate_day(evidence(reset=True))])
    assert summary.valid_days == 3
    assert (summary.profitable_days, summary.losing_days, summary.flat_days) == (1, 1, 1)
    assert summary.net_pnl == 0
    assert summary.profitable_day_percent == Decimal(100) / Decimal(3)


def test_empty_sample_has_no_percentage():
    assert summarize([]).profitable_day_percent is None


def test_missing_intermediate_session_not_counted_as_daily_profit():
    from dataclasses import replace
    old_baseline = replace(evidence(), previous_session_date=date(2026, 11, 24))
    assert evaluate_day(old_baseline).status == "MISSING"


def test_immutable_durable_close_and_retry(tmp_path: Path):
    ledger = CloseLedger(tmp_path / "closes.db", create=True)
    ledger.finalize(evidence())
    ledger.finalize(evidence())
    with pytest.raises(ValueError, match="conflicting"):
        ledger.finalize(evidence(end="1020"))
    assert CloseLedger(tmp_path / "closes.db").read_days()[0].net_pnl == Decimal("10")


def test_invalid_close_never_persisted(tmp_path: Path):
    ledger = CloseLedger(tmp_path / "closes.db", create=True)
    with pytest.raises(ValueError, match="only valid"):
        ledger.finalize(evidence(reset=True))
    assert ledger.read_days() == []


def test_unrelated_database_is_untouched(tmp_path: Path):
    import sqlite3
    path = tmp_path / "operational.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE protection(value TEXT)")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="unrelated"):
        CloseLedger(path, create=True)
    assert path.read_bytes() == before
