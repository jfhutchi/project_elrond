"""The sampled outcome cutoff cannot consume later durable notifications."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select, update
from tests.fixtures.core_m2 import NOW, fee_events, prepare

from quantbot.paper_core import outcomes
from quantbot.paper_core.contracts import LearningPolicy, ModelArtifact, RoundTrip
from quantbot.paper_core.guardian import guardian_cycle, synchronize
from quantbot.paper_core.learning import _select
from quantbot.paper_core.store import CoreStore, table
from quantbot.paper_core.trader import trader_cycle
from quantbot.storage import Database


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "legacy_missing_watermark",
    [
        "normal",
        "missing_watermark",
        "missing_work",
        "missing_work_then_older_notification",
        "preacknowledged",
        "preacknowledged_then_older_notification",
    ],
)
async def test_cutoff_before_late_fee_notification_before_lock_stays_pending(
    tmp_path, legacy_missing_watermark, monkeypatch
):
    db, store, broker, proof, observation, _ = prepare(tmp_path)
    model = ModelArtifact(feature_version="five-minute-price-alpha-v1")
    observation = observation.model_copy(update={"feature_version": model.feature_version})
    initial_notifications = []
    replay_checked = False
    if legacy_missing_watermark == "normal":
        from quantbot.paper_core import trader, work

        original_dirty, original_claim = work.dirty, trader.claim_entry

        def observed_dirty(*args, **kwargs):
            if kwargs.get("_initial_claim"):
                initial_notifications.append(True)
            return original_dirty(*args, **kwargs)

        def claim_then_replay(*args, **kwargs):
            nonlocal replay_checked
            claimed = original_claim(*args, **kwargs)
            assert claimed is not None
            # Same inputs, before any send/economic change: the existing ENTRY
            # observation exits before new creation and cannot initialize coverage.
            assert original_claim(*args, **kwargs) is None
            assert initial_notifications == [True]
            replay_checked = True
            return claimed

        monkeypatch.setattr(work, "dirty", observed_dirty)
        monkeypatch.setattr(trader, "claim_entry", claim_then_replay)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=NOW)
        if legacy_missing_watermark == "normal":
            assert replay_checked and initial_notifications == [True]
        broker.now = NOW + timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        zero = fee_events(store, broker)
        outcomes.import_broker_events(store, zero)
        await synchronize(store, broker, now=broker.now)
        outcomes.process_outcomes(store, cutoff=broker.now)
        original = RoundTrip.model_validate(store.documents("core_round_trips")[0])
        assert original.status == "COMPLETE" and original.actual_fees == 0
        cutoff = broker.now
        broker.now += timedelta(seconds=1)
        outcomes.import_broker_events(
            store,
            fee_events(
                store,
                broker,
                amount="0.10",
                revision=2,
                supersedes={e.cost.fill_id: e.cost.id for e in zero},
            ),
        )
        if legacy_missing_watermark != "normal":
            with db.transaction() as session:
                session.execute(
                    delete(table("core_document_index")).where(
                        table("core_document_index").c.kind == "core_outcome_notifications",
                        table("core_document_index").c.id == decision.id,
                    )
                )

        if legacy_missing_watermark.startswith("missing_work"):
            with db.transaction() as session:
                session.execute(
                    delete(table("core_outcome_work")).where(
                        table("core_outcome_work").c.lot_id == decision.id
                    )
                )
        elif legacy_missing_watermark.startswith("preacknowledged"):
            with db.transaction() as session:
                session.execute(
                    update(table("core_outcome_work"))
                    .where(table("core_outcome_work").c.lot_id == decision.id)
                    .values(processed_version=table("core_outcome_work").c.version)
                )

        if legacy_missing_watermark.endswith("then_older_notification"):
            from quantbot.paper_core.work import dirty

            with db.transaction() as session:
                # Arrival order supplies no coverage: this notification is older
                # than the already retained late commission evidence.
                dirty(session, (decision.id,), available_at=cutoff)

        def state():
            with db.transaction() as session:
                row = (
                    session.execute(
                        select(table("core_outcome_work")).where(
                            table("core_outcome_work").c.lot_id == decision.id
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                return dict(row) if row else None

        policy = LearningPolicy(
            parent_model=model,
            dataset=original.dataset,
            policy_id=original.policy_id,
            evidence_lane=original.evidence_lane,
        )
        learning_at = NOW.replace(hour=21)
        with db.transaction() as session:
            selected, excluded, _ = _select(session, policy, learning_at, execution_now=learning_at)
            assert not selected
            assert (original.id, "OUTCOME_RECONCILIATION_PENDING") in excluded
        before = state()
        stale = outcomes.process_outcomes(store, cutoff=cutoff)
        pending = state()
        assert not stale.outcomes
        assert pending["version"] > pending["processed_version"]
        assert RoundTrip.model_validate(store.documents("core_round_trips")[0]).id == original.id
        with db.transaction() as session:
            selected, excluded, _ = _select(session, policy, learning_at, execution_now=learning_at)
            assert not selected
            assert (original.id, "OUTCOME_RECONCILIATION_PENDING") in excluded
        config, path = store.config, db.path
        db.close()
        db = Database(path)
        store = CoreStore(db, config)
        if legacy_missing_watermark != "normal":
            broker.now += timedelta(seconds=1)  # Covers the conservative cold UTC-second bound.
        fresh = outcomes.process_outcomes(store, cutoff=broker.now)
        assert len(fresh.outcomes) == 1 and fresh.outcomes[0].actual_fees == Decimal("0.20")
        corrected = fresh.outcomes[0]
        assert corrected.supersedes == original.id
        after_current = state()
        assert after_current["version"] == after_current["processed_version"]
        with db.transaction() as session:
            selected, excluded, _ = _select(session, policy, learning_at, execution_now=learning_at)
            assert {row["outcome_id"] for row in selected} == {corrected.id}
        from quantbot.paper_core.work import dirty

        with db.transaction() as session:
            dirty(session, (decision.id,))
        historical = outcomes.process_outcomes(store, cutoff=cutoff)
        assert not historical.outcomes and state()["version"] > state()["processed_version"]
        assert RoundTrip.model_validate(store.documents("core_round_trips")[0]).id == corrected.id
        print(
            "RESIDUAL_CUTOFF_RECOVERY:"
            + json.dumps(
                dict(
                    legacy_missing_watermark=legacy_missing_watermark,
                    lot=decision.id,
                    before=before,
                    after_old_cutoff=pending,
                    after_current_cutoff=after_current,
                    original=original.id,
                    corrected=corrected.id,
                    corrected_fees=str(corrected.actual_fees),
                    selected=[r["outcome_id"] for r in selected],
                    fresh_claim_and_replay_verified=replay_checked,
                    restart=True,
                    historical_no_rollback=True,
                )
            )
        )
    finally:
        db.close()


def test_archived_learning_corpus_without_runtime_entry_notifications_retains_eligibility(tmp_path):
    from tests.fault.paper_core.test_learning_temporal import replay_inputs

    db, store, policy, cutoff = replay_inputs(tmp_path)
    try:
        with db.transaction() as session:
            assert not session.execute(select(table("core_intent_links"))).first()
            selected, excluded, _ = _select(
                session, policy, cutoff, execution_now=cutoff + timedelta(days=1)
            )
            assert selected
            assert not any(reason == "OUTCOME_RECONCILIATION_PENDING" for _, reason in excluded)
            print(
                "RESIDUAL_ARCHIVED_CORPUS:"
                + json.dumps(dict(selected=len(selected), owned_entries=0, pending=False))
            )
    finally:
        db.close()
