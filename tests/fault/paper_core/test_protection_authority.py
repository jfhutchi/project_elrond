"""A raw broker mismatch must never acquire protection authority on replay."""

from datetime import timedelta

import pytest
from tests.unit.operations.test_position_protection_supervisor import (
    IDENTITY,
    NOW,
    FakeBroker,
    account,
    position,
    seed_local,
    supervisor,
)

from quantbot.storage import Database, StorageRepository


@pytest.mark.asyncio
@pytest.mark.parametrize("certified", [False, True])
async def test_two_pass_quantity_mismatch_stays_unprotected(tmp_path, certified):
    with Database.create(tmp_path / "authority.db") as db:
        if certified:
            seed_local(db, (position("XLE", quantity="1", price="75"),))
        with db.transaction() as session:
            repo = StorageRepository(session)
            repo.save_strategy_deployment(IDENTITY)
            repo.save_account_snapshot(
                "baseline",
                account().account,
                (position("XLE", quantity="1", price="75"),),
                captured_at=NOW - timedelta(minutes=5),
            )
        subject = supervisor(
            db, FakeBroker(account(), (position("XLE", quantity="2", price="75"),))
        )
        first = await subject.evaluate(now=NOW)
        second = await subject.evaluate(now=NOW + timedelta(seconds=15))
        assert first.unprotected_count == 1
        assert second.unprotected_count == 1, "raw observation was promoted to baseline"
        assert second.exit_required_count == 0
        with db.transaction() as session:
            repo = StorageRepository(session)
            assert repo.list_fills() == []
            assert (repo.get_latest_reconciliation() is not None) == certified
            assert len(repo.list_account_snapshots()) == (4 if certified else 3), (
                "raw evidence must be retained"
            )
