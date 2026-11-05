"""Core-owned protection uses exact guardian evidence, not legacy cycle totals."""

from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.console.state import read_console_state
from quantbot.domain import Position
from quantbot.paper_core.guardian import synchronize
from quantbot.paper_core.protection_bridge import core_position_protection
from quantbot.storage import StorageRepository
from tests.integration.paper_core.test_paper_funds import enter, prepare, ready
from tests.unit.console.test_protection_panel import _seed_protection_cycle


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["healthy", "stale", "mixed"])
async def test_core_guardian_coverage_does_not_depend_on_legacy_loop(
    tmp_path, monkeypatch, scenario
):
    monkeypatch.setenv("EXPECTED_ACCOUNT_ID", "paper")
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision, reasons = await enter(store, broker)
        assert decision.action == "BUY" and not reasons
        await synchronize(store, broker, now=clocks.wall)
        ready(store)
        now = clocks.wall
        _seed_protection_cycle(
            db,
            observed_at=now - timedelta(hours=1),
            account_id="paper",
            outcome="HEALTHY",
            reason="",
            unprotected=0,
            evaluated_count=0,
            position_count=0,
        )
        with db.transaction() as session:
            repo = StorageRepository(session)
            snapshot = repo.get_latest_account_snapshot("paper")
            assert snapshot is not None and len(snapshot.positions) == 1
            assert core_position_protection(
                session, account_id="paper", position=snapshot.positions[0], now=now
            )[:2] == (True, True)
            if scenario == "mixed":
                repo.save_account_snapshot(
                    "mixed-exposure",
                    snapshot.account,
                    (
                        *snapshot.positions,
                        Position(
                            symbol="SPY",
                            quantity=Decimal(1),
                            market_price=Decimal(100),
                            average_entry_price=Decimal(100),
                            market_value=Decimal(100),
                        ),
                    ),
                    captured_at=now + timedelta(microseconds=1),
                )
            if scenario == "stale":
                now += timedelta(seconds=121)
            state = read_console_state(session, now=now)
        notices = " ".join(state.attention)
        if scenario == "healthy":
            assert "position-protection loop last ran" not in notices
            assert "absent from the latest protection cycle" not in notices
            assert "core-owned position(s) lack current verified Guardian protection" not in notices
        elif scenario == "stale":
            assert "1 core-owned position(s) lack current verified Guardian protection" in notices
        else:
            assert "while 1 position(s) remain open" in notices
            assert "1 open position(s) are absent from the latest protection cycle" in notices
    finally:
        db.close()
