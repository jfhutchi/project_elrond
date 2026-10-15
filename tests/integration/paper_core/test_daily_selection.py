"""Synthetic selection authority and durable experience limits; no broker experience."""

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.integration.paper_core.test_thin_loop import NOW

from quantbot.paper_core.contracts import CoreConfig, ModelArtifact, Observation, ProtectionPlan
from quantbot.paper_core.runtime import initialize_fixture
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore
from quantbot.storage import Database


def prepare(tmp_path, *, inherited=None, predictive=False):
    config = CoreConfig(
        account_id="synthetic-account",
        symbol="XLE",
        symbols=("AAPL", "MSFT", "NVDA"),
        selection_policy="SIGNAL_OR_EXPLORE_V1",
    )
    db = Database.create(tmp_path / "daily.db")
    broker = SyntheticBroker(account_id=config.account_id, now=NOW, inherited=inherited or {})
    store = CoreStore(db, config)
    proof = initialize_fixture(store, broker, now=NOW, entry_enabled=True)
    model = ModelArtifact.fixture_constant(-3) if predictive else ModelArtifact()
    from quantbot.paper_core.activation import initialize_fixture_model

    active = initialize_fixture_model(store, model, at=NOW)
    return db, store, broker, proof, model, active


def cohort(*, at=NOW, unavailable=()):
    from quantbot.paper_core.contracts import ObservationCohort

    observations = tuple(
        Observation(
            symbol=symbol,
            source_start=at - timedelta(minutes=5),
            source_end=at,
            available_at=at,
            recorded_at_utc=at,
            price=Decimal(100),
            raw_data_digest="cohort",
            features=(("return", 0.01),),
            dependencies=("SPY",),
            dependency_available_at=(at,),
            sequence=int(at.timestamp() // 300),
            session_open=NOW - timedelta(minutes=30),
            session_close=NOW + timedelta(hours=6),
        )
        for symbol in ("AAPL", "MSFT", "NVDA")
        if symbol not in dict(unavailable)
    )
    return ObservationCohort(
        source_end=at,
        session_open=NOW - timedelta(minutes=30),
        session_close=NOW + timedelta(hours=6),
        observations=observations,
        unavailable=unavailable,
    )


def test_legacy_config_payload_and_hash_are_unchanged():
    from quantbot.research.manifest import content_id

    config = CoreConfig(account_id="synthetic-account", symbol="XLE")
    assert "symbols" not in config.model_dump()
    assert "selection_policy" not in config.model_dump()
    assert config.entry_symbols == ("XLE",)
    assert content_id(config) == content_id(
        CoreConfig.model_validate_json(config.model_dump_json())
    )


@pytest.mark.parametrize("symbols", [("aapl",), ("AAPL", "AAPL"), ("SPY",), ("AAPL/US",)])
def test_universe_rejects_unsupported_duplicates_and_benchmark(symbols):
    with pytest.raises(ValueError):
        CoreConfig(account_id="paper", symbol="XLE", symbols=symbols)


def test_signal_beats_weak_even_when_only_qualifying_stock_was_used(tmp_path, monkeypatch):
    from quantbot.paper_core.selection import evaluate_cohort

    db, store, broker, proof, model, active = prepare(tmp_path, predictive=True)
    model = model.model_copy(update={"feature_version": "synthetic-causal-v1"})
    active = active.model_copy(update={"model_id": model.id, "model_digest": model.id})

    def predictions(model, rows):
        return [rows[0]["return"] * 1000]

    monkeypatch.setattr("quantbot.paper_core.learning.predict_bps", predictions)
    captured = cohort()
    captured = captured.model_copy(
        update={
            "observations": tuple(
                o.model_copy(update={"features": (("return", value),)})
                for o, value in zip(captured.observations, (0.001, 0.025, -0.004), strict=True)
            )
        }
    )
    try:
        chosen = evaluate_cohort(store.config, captured, model, proof, activation=active)
        assert chosen.symbol == "MSFT" and chosen.selection.kind == "SIGNAL"
        assert chosen.prediction_bps == 25
        diverse = evaluate_cohort(
            store.config, captured, model, proof, activation=active, session_counts=(("MSFT", 1),)
        )
        assert diverse.symbol == "MSFT" and diverse.selection.kind == "SIGNAL"
        assert diverse.prediction_bps == 25
        repeated = captured.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(update={"features": (("return", value),)})
                    for o, value in zip(captured.observations, (0.011, 0.025, 0.02), strict=True)
                )
            }
        )
        fair = evaluate_cohort(
            store.config,
            repeated,
            model,
            proof,
            activation=active,
            session_counts=(("AAPL", 1), ("MSFT", 2), ("NVDA", 1)),
        )
        assert fair.symbol == "NVDA" and fair.prediction_bps == 20
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sixth_unknown", [False, True])
async def test_six_completed_or_unknown_reservations_block_seventh(tmp_path, sixth_unknown):
    from tests.fixtures.core_m2 import fee_events

    from quantbot.brokers import SubmissionUncertainError
    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        chosen = []
        for i in range(7):
            broker.now = NOW + timedelta(minutes=5 * i)
            for symbol in store.config.entry_symbols:
                broker.set_mark(symbol, Decimal(100), as_of=broker.now)
            await synchronize(store, broker, now=broker.now)
            await guardian_cycle(store, broker, now=broker.now)
            if i == 5 and sixth_unknown:

                async def unknown(intent):
                    raise SubmissionUncertainError("explicit synthetic unknown send")

                broker.submit_order = unknown
                with pytest.raises(SubmissionUncertainError):
                    await trader_cycle(
                        store,
                        broker,
                        cohort(at=broker.now),
                        model,
                        store.latest_proof(),
                        now=broker.now,
                    )
                continue
            decision = await trader_cycle(
                store, broker, cohort(at=broker.now), model, store.latest_proof(), now=broker.now
            )
            if i == 6:
                assert decision.action == "WAIT" and "SESSION_RESERVATION_LIMIT" in decision.gates
                break
            chosen.append(decision.symbol)
            assert len(broker.posts) == 2 * i + 1
            broker.set_mark(decision.symbol, Decimal(98), as_of=broker.now)
            await guardian_cycle(store, broker, now=broker.now)
            import_broker_events(
                store,
                tuple(
                    e
                    for e in fee_events(store, broker)
                    if e.cost.fill_id in {f.fill_id for f in broker.fills[-2:]}
                ),
            )
            await synchronize(store, broker, now=broker.now)
        assert (
            chosen == ["AAPL", "MSFT", "NVDA", "AAPL", "MSFT", "NVDA"][: 5 if sixth_unknown else 6]
        )
        assert len(broker.posts) == (10 if sixth_unknown else 12)
    finally:
        db.close()


def test_concurrent_claimers_with_different_receipt_times_reserve_one_boundary(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from quantbot.paper_core.selection import evaluate_cohort, record_cohort
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        first = cohort()
        second = first.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(
                        update={
                            "recorded_at_utc": NOW + timedelta(seconds=1),
                            "available_at": NOW + timedelta(seconds=1),
                        }
                    )
                    for o in first.observations
                )
            }
        )
        decisions = []
        for captured in (first, second):
            record_cohort(store, captured)
            decisions.append(
                evaluate_cohort(store.config, captured, model, proof, activation=active)
            )

        def claim(decision):
            return claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, decisions))
        assert sum(result is not None for result in results) == 1
        assert len(store.links()) == 1
    finally:
        db.close()


def test_only_definitive_unsent_releases_reservation(tmp_path):
    from quantbot.domain import IntentState
    from quantbot.paper_core.selection import evaluate_cohort, record_cohort, reservation_state
    from quantbot.paper_core.send_guard import final_send_objections
    from quantbot.paper_core.trader import claim_entry
    from quantbot.risk import OrderPurpose
    from quantbot.storage import StorageRepository

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        captured = cohort()
        record_cohort(store, captured)
        decision = evaluate_cohort(store.config, captured, model, proof, activation=active)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        with db.transaction() as session:
            repo = StorageRepository(session)
            intent = repo.transition_order_intent(
                entry.intent.intent_id,
                IntentState.ORDER_CREATED,
                expected_current=IntentState.RISK_APPROVED,
            )
            assert reservation_state(store, session, captured)[0] == (("AAPL", 1),)
        reasons = final_send_objections(
            store, intent, OrderPurpose.ENTRY, clock=lambda: NOW + timedelta(minutes=3)
        )
        assert "CORE_ENTRY_DECISION_EXPIRED" in reasons
        with db.transaction() as session:
            # Refusal alone never releases while the durable state is still ORDER_CREATED.
            assert reservation_state(store, session, captured)[0] == (("AAPL", 1),)
            StorageRepository(session).transition_order_intent(
                intent.intent_id, IntentState.ERROR, expected_current=IntentState.ORDER_CREATED
            )
            assert reservation_state(store, session, captured)[0] == ()
    finally:
        db.close()


def test_negative_prediction_is_retained_and_invalid_activation_cannot_explore(tmp_path):
    from quantbot.learner.five_minute import FEATURE_NAMES
    from quantbot.paper_core.selection import evaluate_cohort

    db, store, broker, proof, model, active = prepare(tmp_path, predictive=True)
    try:
        captured = cohort()
        captured = captured.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(
                        update={
                            "features": tuple((n, 0.0) for n in FEATURE_NAMES),
                            "feature_version": "five-minute-price-alpha-v1",
                        }
                    )
                    for o in captured.observations
                )
            }
        )
        decision = evaluate_cohort(store.config, captured, model, proof, activation=active)
        assert decision.action == "BUY" and decision.selection.kind == "EXPLORATION"
        assert decision.prediction_bps == -3
        invalid = evaluate_cohort(store.config, captured, model, proof)
        assert invalid.action == "WAIT" and "SHADOW_MODEL_NOT_ACTIVATED" in invalid.gates
    finally:
        db.close()


def test_nondefault_health_recovery_does_not_clear_another_symbol(tmp_path):
    from quantbot.paper_core.health import entry_health_objections, recover_health, report

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        for symbol in ("AAPL", "MSFT"):
            report(
                store,
                role="TRADER",
                state="RETRYABLE_FAILURE",
                reasons=("DATA_WAIT",),
                now=NOW,
                scope_id=symbol,
            )
        newer = proof.model_copy(
            update={
                "generation": proof.generation + 1,
                "recorded_at_utc": NOW + timedelta(seconds=1),
            }
        )
        recover_health(store, newer, now=NOW + timedelta(seconds=1), symbol="MSFT")
        with db.transaction() as session:
            assert entry_health_objections(store, session, "AAPL") == ("DATA_WAIT",)
            assert not entry_health_objections(store, session, "MSFT")
    finally:
        db.close()


def test_collect_without_commissioned_activation_cannot_claim_exploration(tmp_path):
    from quantbot.paper_core.selection import evaluate_cohort

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        decision = evaluate_cohort(store.config, cohort(), model, proof)
        assert decision.action == "WAIT" and "CORE_MODEL_UNCOMMISSIONED" in decision.gates
    finally:
        db.close()


@pytest.mark.asyncio
async def test_all_unavailable_members_retain_truthful_durable_wait_evidence(tmp_path):
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        captured = cohort(unavailable=tuple((s, "MISSING") for s in store.config.entry_symbols))
        with pytest.raises(ValueError, match="NO_ELIGIBLE_COHORT_MEMBERS"):
            await trader_cycle(store, broker, captured, model, proof, now=NOW)
        assert not broker.posts
        assert any(
            d.get("selection_cohort", {}).get("unavailable")
            == [["AAPL", "MISSING"], ["MSFT", "MISSING"], ["NVDA", "MISSING"]]
            for d in store.documents("core_provider_acquisitions")
        )
    finally:
        db.close()


def test_missing_and_held_members_allow_collect_exploration(tmp_path):
    from quantbot.paper_core.selection import evaluate_cohort

    db, store, broker, proof, model, active = prepare(tmp_path, inherited={"AAPL": Decimal(1)})
    try:
        chosen = evaluate_cohort(
            store.config,
            cohort(unavailable=(("MSFT", "MISSING"),)),
            model,
            proof,
            activation=active,
        )
        assert chosen.symbol == "NVDA" and chosen.action == "BUY"
        assert chosen.selection.kind == "EXPLORATION" and chosen.prediction_bps is None
        assert ("MSFT", "MISSING") in chosen.selection.cohort.unavailable
    finally:
        db.close()


def test_forged_fallback_and_incomplete_cohort_cannot_claim(tmp_path):
    from quantbot.paper_core.selection import evaluate_cohort, record_cohort
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        captured = cohort()
        record_cohort(store, captured)
        chosen = evaluate_cohort(store.config, captured, model, proof, activation=active)
        forged = chosen.model_copy(
            update={"selection": chosen.selection.model_copy(update={"kind": "SIGNAL"})}
        )
        with pytest.raises(ValueError, match="selection|decision"):
            claim_entry(store, forged, ProtectionPlan.for_decision(forged, store.config))
        incomplete = captured.model_copy(update={"observations": captured.observations[:1]})
        with pytest.raises(ValueError, match="cohort"):
            evaluate_cohort(store.config, incomplete, model, proof, activation=active)
        assert not store.links()
    finally:
        db.close()


def test_substituting_unavailable_reason_cannot_hide_captured_member(tmp_path):
    from quantbot.paper_core.selection import evaluate_cohort, record_cohort
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        captured = cohort()
        record_cohort(store, captured)
        hidden = captured.model_copy(
            update={
                "observations": captured.observations[1:],
                "unavailable": (("AAPL", "FAKE_MISSING"),),
            }
        )
        forged = evaluate_cohort(store.config, hidden, model, proof, activation=active)
        with pytest.raises(ValueError, match="captured cohort"):
            claim_entry(store, forged, ProtectionPlan.for_decision(forged, store.config))
        assert not store.links()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_nondefault_stop_and_restart_rotation_and_boundary_dedup(tmp_path):
    from tests.fixtures.core_m2 import fee_events

    from quantbot.paper_core.guardian import guardian_cycle, synchronize
    from quantbot.paper_core.outcomes import import_broker_events
    from quantbot.paper_core.trader import trader_cycle

    db, store, broker, proof, model, active = prepare(tmp_path)
    try:
        captured = cohort()
        first = await trader_cycle(store, broker, captured, model, proof, now=NOW)
        assert first.symbol == "AAPL" and len(broker.posts) == 1
        broker.set_mark("AAPL", Decimal(98), as_of=NOW)
        assert (await guardian_cycle(store, broker, now=NOW)).exit_intent_ids
        import_broker_events(store, fee_events(store, broker))
        await synchronize(store, broker, now=NOW)
        config = store.config
        db.close()
        db = Database(tmp_path / "daily.db")
        store = CoreStore(db, config)
        changed = captured.model_copy(
            update={
                "observations": tuple(
                    o.model_copy(
                        update={
                            "recorded_at_utc": NOW + timedelta(seconds=1),
                            "available_at": NOW + timedelta(seconds=1),
                        }
                    )
                    for o in captured.observations
                )
            }
        )
        await trader_cycle(store, broker, changed, model, store.latest_proof(), now=NOW)
        assert len(broker.posts) == 2
        broker.now = NOW + timedelta(minutes=5)
        await synchronize(store, broker, now=broker.now)
        await guardian_cycle(store, broker, now=broker.now)
        later = await trader_cycle(
            store, broker, cohort(at=broker.now), model, store.latest_proof(), now=broker.now
        )
        assert later.symbol == "MSFT" and len(broker.posts) == 3
    finally:
        db.close()
