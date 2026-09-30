"""Synthetic store fixtures; no broker access or market-performance evidence."""

from datetime import timedelta

import pytest
from sqlalchemy import event

from quantbot.learner import service
from quantbot.storage import Database, StorageRepository
from tests.unit.learner.test_data import inputs_for, session_sequence


def stored(tmp_path):
    database = Database.create(tmp_path / "research.db")
    calendar = session_sequence()
    inputs = inputs_for(calendar, through=89)
    with database.transaction() as session:
        StorageRepository(session).save_bars(
            [r.bar for r in inputs], provider="alpaca:iex:all:1day",
            adjustment_metadata={"provider": "alpaca", "feed": "iex",
                                 "adjustment": "all", "timeframe": "1Day"},
        )
    return database, calendar


def test_store_loader_guards_before_any_bar_select(tmp_path, monkeypatch):
    from quantbot.learner import store

    database, calendar = stored(tmp_path)
    statements = []
    with database.transaction() as session:
        event.listen(session.connection(), "before_cursor_execute",
                     lambda conn, cursor, statement, parameters, context, many:
                     statements.append(statement))

        def refuse(*args):
            raise ValueError("protected fixture")

        monkeypatch.setattr(service, "guard_range", refuse)
        with pytest.raises(ValueError, match="protected fixture"):
            store.load_inputs(session, calendar, ("AAPL", "MSFT"), 0, 70)
    assert not any("FROM bars" in s for s in statements)
    database.close()


def test_resolution_uses_verified_prices_without_a_second_raw_read(tmp_path, monkeypatch):
    from sqlalchemy import update

    from quantbot.learner import store
    from quantbot.learner.repository import LearnerRepository
    from quantbot.research.registry import DataRole
    from quantbot.storage.database import encode_utc
    from quantbot.storage.schema import bars

    database, calendar = stored(tmp_path)
    dates = [s.session_date for s in calendar.sessions]
    spec = store.StoreReplaySpec(
        sessions=calendar, symbols=("AAPL", "MSFT"), train_start=dates[60], train_end=dates[70],
        validation_start=dates[71], validation_end=dates[84],
        holdout_start=dates[85], holdout_end=dates[94],
    )
    dataset = store.build_from_store(database, spec)
    model = service.train(database, dataset.identity)
    predictions = service.replay(database, model.model_id)
    frozen = next(r for r in dataset.rows if r.role is DataRole.VALIDATION
                  and r.features.identity == predictions[0].features.identity)
    original_verify = store.verify_source

    def race(*args):
        inputs = original_verify(*args)
        with database.transaction() as other:
            other.execute(update(bars).where(
                bars.c.symbol == frozen.features.symbol,
                bars.c.timestamp == encode_utc(frozen.features.target_at),
            ).values(close="200", high="201"))
        return inputs

    monkeypatch.setattr(store, "verify_source", race)
    assert store.resolve_from_store(database, model.model_id, calendar) == len(predictions)
    with database.transaction() as session:
        outcome = LearnerRepository(session).get_outcome(predictions[0].prediction_id)
        assert outcome.raw_return == frozen.raw_return
    database.close()


def test_store_loader_bounded_complete_and_honest(tmp_path):
    from quantbot.learner import store

    database, calendar = stored(tmp_path)
    with database.transaction() as session:
        inputs = store.load_inputs(session, calendar, ("AAPL", "MSFT"), 0, 70)
        assert len(inputs) == 71 * 3
        assert max(r.bar.timestamp for r in inputs) == calendar.sessions[70].close_at
        assert all(r.available_at == r.bar.timestamp for r in inputs)
        with pytest.raises(ValueError, match="complete"):
            store.load_inputs(session, calendar, ("AAPL", "MISSING"), 0, 70)
        shifted = calendar.model_copy(update={"sessions": tuple(
            s.model_copy(update={"close_at": s.close_at + timedelta(minutes=1)})
            for s in calendar.sessions)})
        with pytest.raises(ValueError, match="calendar"):
            store.load_inputs(session, shifted, ("AAPL", "MSFT"), 0, 70)
    database.close()


def test_forward_paper_reservation_blocks_training_alias(tmp_path, monkeypatch):
    from sqlalchemy import update

    from quantbot.research.registry import DataRole, HypothesisRegistry
    from quantbot.storage.schema import hypothesis_data_windows
    from tests.unit.research.test_registry import NOW, cleared, make_draft

    database = Database.create(tmp_path / "forward.db")
    draft = make_draft()
    protected = draft.windows_for(DataRole.PROTECTED_EVALUATION)[0]
    with database.transaction() as session:
        HypothesisRegistry(session).register(
            draft, now=NOW, critique=cleared(draft.hypothesis_id, draft.version))
        session.execute(update(hypothesis_data_windows).where(
            hypothesis_data_windows.c.role == DataRole.PROTECTED_EVALUATION.value
        ).values(role=DataRole.FORWARD_PAPER.value))
    monkeypatch.setattr(service, "_utcnow", lambda: NOW)
    with database.transaction() as session, pytest.raises(ValueError, match="forward-paper"):
        service.guard_range(session, "alpaca-us-equities-daily", protected.start, protected.end)
    database.close()
