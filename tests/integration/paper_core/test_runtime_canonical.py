"""Operational underlying-source guard refreshes scalar metadata without targets."""

import json
from datetime import date

from quantbot.paper_core.canonical import import_canonical_metadata, snapshot_canonical_metadata
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.learning import _guard
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database


def test_underlying_canonical_holdout_cannot_be_evaded_by_owned_cohort_name(tmp_path):
    source_path = tmp_path / "source.db"
    with Database.create(source_path) as source:
        with source.engine.begin() as connection:
            # The protected body is never hydrated; SQL extracts holdout metadata only.
            document = {
                "holdout_window": {
                    "dataset": "us-equities-daily",
                    "start": "2026-04-01",
                    "end": "2026-09-30",
                },
                "protected_feature_payload": "MUST_NOT_HYDRATE",
            }
            connection.exec_driver_sql(
                "INSERT INTO learner_datasets VALUES (?,?,?)",
                ("holdout", "hash", json.dumps(document)),
            )
    first = snapshot_canonical_metadata(source_path, ("us-equities-daily",))
    assert "MUST_NOT_HYDRATE" not in json.dumps(first, default=str)
    with Database.create(tmp_path / "core.db") as db:
        store = CoreStore(db, CoreConfig(account_id="fixture", symbol="XLE"))
        with db.transaction() as session:
            import_canonical_metadata(session, first)
            assert (
                _guard(
                    session,
                    "ALPACA_PAPER_OWNED",
                    date(2026, 9, 1),
                    date(2026, 9, 2),
                    now=date(2026, 10, 14),
                )
                == "DECLARED_IMPORTED_CANONICAL_HOLDOUT"
            )
            assert (
                _guard(
                    session,
                    "ALPACA_PAPER_OWNED",
                    date(2026, 10, 14),
                    date(2026, 10, 14),
                    now=date(2026, 10, 14),
                )
                is None
            )
        with Database(source_path) as source:
            with source.engine.begin() as connection:
                document["holdout_window"]["end"] = "2026-10-31"
                connection.exec_driver_sql(
                    "UPDATE learner_datasets SET document_json=?", (json.dumps(document),)
                )
        current = snapshot_canonical_metadata(source_path, ("us-equities-daily",))
        with db.transaction() as session:
            import_canonical_metadata(session, current)
            assert (
                _guard(
                    session,
                    "ALPACA_PAPER_OWNED",
                    date(2026, 10, 14),
                    date(2026, 10, 14),
                    now=date(2026, 10, 14),
                )
                == "DECLARED_IMPORTED_CANONICAL_HOLDOUT"
            )
        assert len(store.documents("core_canonical_sources")) == 2


def test_external_owner_replay_is_inert_without_adopting_exit_ownership(tmp_path):
    from sqlalchemy import select

    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.store import table

    with Database.create(tmp_path / "core.db") as db:
        record = dict(
            event_id="legacy-fill",
            symbol="XLE",
            owner="legacy-owner",
            quantity_delta="-1",
            net_cash="100",
            account_id="fixture",
            source_document="first",
            occurred_at="2026-10-14T14:00:00Z",
        )
        with db.transaction() as session:
            AlpacaCoreBroker.record_external(session, record)
            AlpacaCoreBroker.record_external(session, {**record, "source_document": "overlap"})
            assert len(session.execute(select(table("core_external_owner_events"))).all()) == 1
            assert session.execute(select(table("core_intent_links"))).first() is None


def test_operational_lane_refuses_fixture_broker_and_unowned_exit(tmp_path):
    from datetime import UTC, datetime

    import pytest
    from tests.fixtures.core_m3 import prepare

    from quantbot.domain import OrderIntent, OrderSide, OrderType, TimeInForce
    from quantbot.paper_core.runtime import require_runtime_broker
    from quantbot.paper_core.send_guard import owned_exit_objections

    db, store, broker, *_ = prepare(tmp_path)
    store.config = store.config.model_copy(update={"evidence_lane": "BROKER_PAPER"})
    with pytest.raises(ValueError, match="exact commissioned"):
        require_runtime_broker(store, broker)
    intent = OrderIntent(
        intent_id="unowned",
        client_order_id="unowned-client",
        strategy_id="legacy",
        signal_date=date(2026, 10, 14),
        symbol="XLE",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        quantity=1,
        created_at=datetime(2026, 10, 14, 14, tzinfo=UTC),
    )
    with db.transaction() as session:
        assert owned_exit_objections(store, session, intent) == ("CORE_EXIT_HANDOFF_MISSING",)
    db.close()


def test_isolated_paper_commission_requires_explicit_source_then_keeps_stop(tmp_path):
    import asyncio
    import hashlib
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    import pytest

    from quantbot.domain import Account
    from quantbot.paper_core.__main__ import derived_core
    from quantbot.paper_core.activation import initialize_operational_model
    from quantbot.paper_core.authority import (
        RuntimeCommission,
        commission_in_session,
        commission_runtime,
    )
    from quantbot.paper_core.commissioning import acquire_commission_document
    from quantbot.paper_core.release import RuntimeConfig, prepare_release
    from quantbot.paper_core.simulation import SyntheticBroker
    from quantbot.storage import StateConflictError, StorageRepository

    source = tmp_path / "canonical.db"
    Database.create(source).close()
    with Database.create(tmp_path / "paper-fixture.db") as db:
        config = RuntimeConfig(
            repository=str(Path(__file__).resolve().parents[3]),
            store_path=str(db.path),
            store_identity=db.store_identity,
            expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
            core=CoreConfig(
                account_id="paper",
                symbol="XLE",
                evidence_lane="BROKER_PAPER",
                release_id="UNBOUND_PREPARED_RELEASE",
                clock_profile="PAPER_TIME_V2",
            ),
            holdout_source=str(source),
            canonical_datasets=("us-equities-daily",),
            learning_origin=datetime.now(UTC) - timedelta(days=1),
            activation_policy="DAILY_BOUNDED_V2",
        )
        release = prepare_release(config)
        broker = SyntheticBroker(
            account_id="paper",
            now=datetime.now(UTC),
            initial_account=Account(
                account_id="paper", cash=100000, equity=100000, buying_power=100000, currency="USD"
            ),
        )
        broker.endpoint = "https://paper-api.alpaca.markets"
        request = {
            "release_id": release.id,
            "approval_reference": "EXPLICIT_ISOLATED_MOCK_PROVIDER_FIXTURE",
            "fenced_writer_evidence_ids": ["ISOLATED_NO_OTHER_WRITERS"],
            "legacy_owners": [],
        }
        document = asyncio.run(acquire_commission_document(release, broker, request))
        assert not broker.posts
        store = CoreStore(db, derived_core(release, "paper"))
        commission = RuntimeCommission.model_validate(document["commission"])
        malformed = {**document["baseline"], "source": "EXPLICIT_SYNTHETIC_INITIAL_STATE"}
        with pytest.raises(StateConflictError, match="BASELINE"):
            commission_runtime(store, release, commission, baseline=malformed)
        commission_runtime(store, release, commission, baseline=document["baseline"])
        event = initialize_operational_model(store, config.initial_model, at=commission.approved_at)
        with db.transaction() as session:
            assert commission_in_session(store, session).id == commission.id
            assert StorageRepository(session).get_kill_switch_state().engaged
        assert event.model_id == config.initial_model.id and not broker.posts
        print(
            "M6_COMMISSION_OFFLINE_FIXTURE:"
            + json.dumps(
                {
                    "release_id": release.id,
                    "commission_id": commission.id,
                    "model_id": event.model_id,
                    "stop_preserved": True,
                    "provider_probe": "MOCK_ONLY_NOT_ACTUAL_BROKER",
                    "posts": 0,
                }
            )
        )


def test_daily_warmup_considers_new_close_and_retries_calendar_failure(tmp_path):
    import asyncio
    import hashlib
    from datetime import timedelta
    from pathlib import Path

    import pytest
    from tests.fixtures.core_m3 import prepare

    from quantbot.paper_core.activation import initialize_fixture_model
    from quantbot.paper_core.contracts import LearningCalendar, ModelArtifact
    from quantbot.paper_core.release import RuntimeConfig
    from quantbot.paper_core.roles import daily_consideration
    from quantbot.strategy import XNYSSession, XNYSSessionSequence

    db, store, broker, *rest = prepare(tmp_path)
    clocks = rest[-1]
    clocks.execution_domain = "SYNTHETIC_TEST:DAILY_SCHEDULING"
    model = ModelArtifact(feature_version="five-minute-price-alpha-v1")
    initialize_fixture_model(store, model, at=clocks.wall)
    origin = clocks.wall - timedelta(days=3)
    config = RuntimeConfig(
        repository=str(Path(__file__).resolve().parents[3]),
        store_path=str(db.path),
        store_identity=db.store_identity,
        expected_account_hash=hashlib.sha256(broker.account_id.encode()).hexdigest(),
        core=store.config.model_copy(update={"release_id": "UNBOUND_PREPARED_RELEASE"}),
        holdout_source=str(tmp_path / "source.db"),
        learning_origin=origin,
        learning_dataset="EXPLICIT_SYNTHETIC",
    )
    sequence = LearningCalendar(
        sequence=XNYSSessionSequence(
            calendar="XNYS",
            sessions=tuple(
                XNYSSession(
                    session_date=(clocks.wall - timedelta(days=d)).date(),
                    open_at=clocks.wall - timedelta(days=d, hours=1),
                    close_at=clocks.wall - timedelta(days=d) + timedelta(seconds=3),
                )
                for d in (2, 1, 0)
            ),
        )
    )
    calls = 0

    async def calendar():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("EXPLICIT_CALENDAR_RETRY")
        return sequence

    broker.learning_calendar = calendar
    with pytest.raises(TimeoutError):
        asyncio.run(daily_consideration(store, broker, config))
    before = asyncio.run(daily_consideration(store, broker, config))
    assert before[0].endswith((clocks.wall - timedelta(days=1)).date().isoformat())
    clocks.advance(4)
    after = asyncio.run(daily_consideration(store, broker, config))
    assert after[0].endswith(clocks.wall.date().isoformat())
    assert len(store.documents("core_learning_runs")) == 2
    assert all(
        r["status"] == "NO_CHANGE" and r["trial_charge_id"] is None
        for r in store.documents("core_learning_runs")
    )
    assert asyncio.run(daily_consideration(store, broker, config)) == after
    assert len(store.documents("core_learning_runs")) == 2
    db.close()
