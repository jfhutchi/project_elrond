"""OFFLINE_PROVIDER_PROTOCOL_FIXTURE; no broker acceptance or OS-role separation claim."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from tests.integration.paper_core.test_paper_funds import (
    FundsProvider,
    close,
    enter,
    prepare,
)

from quantbot.brokers.base import BrokerTransportResponse
from quantbot.paper_core.activation import ActivationAuthority, active_in_session
from quantbot.paper_core.authority import commission_in_session
from quantbot.paper_core.contracts import LearningReceipt, ReconciliationProof, RoundTrip
from quantbot.paper_core.guardian import _pending_owned_activity, guardian_cycle, synchronize
from quantbot.paper_core.learning import predict_bps
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.roles import daily_consideration
from quantbot.paper_core.store import put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StateConflictError, StorageRepository

SESSIONS = tuple(
    datetime(2026, 10, d, 13, 30, tzinfo=UTC)
    for d in (14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30)
)
GROSS = "owned-executed-paper-gross-return-v1"
V3 = "daily-paper-exploration-walk-forward-v3"


class DailyProvider(FundsProvider):
    """Only HTTP-shaped external observations; core selection and fitting stay real."""

    def __init__(self, clocks):
        super().__init__(clocks)
        clocks.execution_domain = "BROKER_PAPER:OFFLINE_PROVIDER_FIXTURE_TIME_V1"
        self.signs = {"AAPL": 1, "MSFT": 1}
        self.exit_sign = 1
        self.activity_lag_seconds = 0

    async def request(self, method, url, **kwargs):
        if method == "POST" and url.endswith("/orders"):
            self.fill_price = Decimal(100)
            if kwargs["json_body"]["side"] == "sell":
                self.fill_price += Decimal(self.exit_sign) / 5
        if any(url.endswith(path) for path in ("/calendar", "/bars", "/account/activities")):
            self.clocks.advance(self.delay)
            self.now = self.clocks.wall
            params = kwargs["params"]
            self.calls.append((method, url, params))
            if url.endswith("/calendar"):
                payload = [
                    dict(date=s.date().isoformat(), open="09:30", close="16:00")
                    for s in SESSIONS
                    if params["start"] <= s.date().isoformat() <= params["end"]
                ]
            elif url.endswith("/account/activities"):
                after = datetime.fromisoformat(params["after"])
                until = datetime.fromisoformat(params["until"])
                rows = [
                    f
                    for f in self.fills
                    if after
                    <= datetime.fromisoformat(f["transaction_time"])
                    <= min(until, self.now - timedelta(seconds=self.activity_lag_seconds))
                ]
                start = 0
                if params.get("page_token"):
                    start = next(
                        i + 1 for i, f in enumerate(rows) if f["id"] == params["page_token"]
                    )
                payload = rows[start : start + int(params["page_size"])]
            else:
                end = self.now.replace(second=0, microsecond=0)
                end -= timedelta(minutes=end.minute % 5)
                payload = dict(
                    bars={
                        s: [
                            dict(
                                t=(end - timedelta(minutes=35 - i * 5)).isoformat(),
                                **dict.fromkeys(
                                    ("o", "h", "l", "c"),
                                    float(
                                        Decimal(100) / (1 + Decimal(self.signs.get(s, 0)) / 500)
                                        if i == 5
                                        else Decimal(100)
                                    ),
                                ),
                                v=100,
                            )
                            for i in range(7)
                        ]
                        for s in params["symbols"].split(",")
                    },
                    next_page_token=None,
                )
            return BrokerTransportResponse(
                status_code=200, headers={}, content=json.dumps(payload).encode()
            )
        return await super().request(method, url, **kwargs)


def advance_to(clocks, at):
    assert at >= clocks.wall
    clocks.advance((at - clocks.wall).total_seconds())


def selection(store):
    with store.database.transaction() as session:
        return active_in_session(store, session)


async def catch_up_provider(store, broker, clocks):
    """Real polls recover the six-hour source cursor after overnight/weekend jumps."""
    target = clocks.wall
    for _ in range(16):
        await synchronize(store, broker, now=clocks.wall)
        with store.database.transaction() as session:
            watermark = session.execute(
                select(table("core_provider_progress").c.watermark)
            ).scalar_one()
        if datetime.fromisoformat(watermark) >= target:
            return
    raise AssertionError("provider cursor did not catch up within fixture poll bound")


@pytest.mark.asyncio
async def test_fractional_exit_activity_is_recovered_before_inventory_certification(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        receipt = await close(store, broker, clocks)
        assert "POST_EXIT_RECONCILIATION_WAIT" not in receipt.reasons, receipt.reasons
        process_outcomes(store, cutoff=clocks.wall)
        assert store.export_loop(first.id)["round_trip"] is not None
        second, reasons = await enter(store, broker)
        assert second.action == "BUY" and not reasons, (second.gates, reasons)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delayed_exit_activity_stays_retryable_until_actual_fill_arrives(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        provider.activity_lag_seconds = 60
        clocks.advance(1800)
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert "POST_EXIT_RECONCILIATION_WAIT" in receipt.reasons
        assert not any(
            h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events")
        )
        assert dict(store.latest_proof().actual_quantities) == {"AAPL": Decimal(5)}
        assert provider.holdings["AAPL"] == 0
        assert store.export_loop(first.id)["round_trip"] is None
        pending_health = next(
            h
            for h in store.documents("core_health_events")
            if "OWNED_FILL_ACTIVITY_PENDING" in h["objections"]
        )
        proof = store.read(
            "core_reconciliation_proofs", pending_health["evidence_ids"][0], ReconciliationProof
        )
        with db.transaction() as session:
            original_id = session.execute(
                select(table("core_provider_progress").c.document_id)
            ).scalar_one()
            source = json.loads(
                session.execute(
                    select(table("core_provider_acquisitions").c.payload).where(
                        table("core_provider_acquisitions").c.id == original_id
                    )
                ).scalar_one()
            )
        started = datetime.fromisoformat(source["until"])
        assert _pending_owned_activity(store, proof, capture_started=started, now=clocks.wall)
        for fault in ("stale", "wrong-account", "wrong-endpoint", "wrong-order"):
            changed = dict(source)
            if fault == "stale":
                changed["until"] = (started - timedelta(seconds=1)).isoformat()
            elif fault == "wrong-account":
                changed["account_id"] = "another-account"
            elif fault == "wrong-endpoint":
                changed["endpoint"] = "https://api.alpaca.markets"
            elif fault == "wrong-order":
                changed["pending_owned_orders"] = [
                    dict(changed["pending_owned_orders"][0], client_order_id="unowned-order")
                ]
            identity = content_id(changed)
            with db.transaction() as session:
                put(session, "core_provider_acquisitions", identity, changed)
                session.execute(
                    table("core_provider_progress").update().values(document_id=identity)
                )
            assert not _pending_owned_activity(
                store, proof, capture_started=started, now=clocks.wall
            ), fault
        with db.transaction() as session:
            session.execute(
                table("core_provider_progress").update().values(document_id=original_id)
            )
        assert _pending_owned_activity(store, proof, capture_started=started, now=clocks.wall)
        clocks.advance(60)
        await synchronize(store, broker, now=clocks.wall)
        process_outcomes(store, cutoff=clocks.wall)
        second, reasons = await enter(store, broker)
        assert second.action == "BUY" and not reasons, (second.gates, reasons)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_integrity", [False, True])
async def test_first_buy_activity_delay_waits_without_certifying_partial_cash(
    tmp_path, prior_integrity
):
    from quantbot.paper_core.funds import entry_inventory_objections
    from quantbot.paper_core.health import active_health, report
    from quantbot.paper_core.store import latest_certification

    class FirstBuyLagProvider(DailyProvider):
        def __init__(self, clocks):
            super().__init__(clocks)
            self.activity_lag_seconds = 60

    db, store, broker, provider, clocks = await prepare(
        tmp_path, provider_factory=FirstBuyLagProvider
    )
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        with db.transaction() as session:
            original_certificate = latest_certification(session)
            assert not StorageRepository(session).list_fills()
        incident_id = None
        if prior_integrity:
            # Explicit preexisting integrity fixture; fresh acquisitions cannot resolve it.
            incident_id = report(
                store,
                role="GUARDIAN",
                state="INTEGRITY_FAILURE",
                scope="ACCOUNT",
                scope_id=store.config.account_id,
                reasons=("PRIOR_KNOWN_CASH_CONTRADICTION",),
                now=clocks.wall,
            )
        with pytest.raises(TimeoutError, match="OWNED_FILL_ACTIVITY_PENDING"):
            await synchronize(store, broker, now=clocks.wall)
        pending = next(
            h
            for h in store.documents("core_health_events")
            if "OWNED_FILL_ACTIVITY_PENDING" in h["objections"]
        )
        proof = store.read(
            "core_reconciliation_proofs", pending["evidence_ids"][0], ReconciliationProof
        )
        assert proof.cash_complete and proof.expected_cash == Decimal(100000)
        assert proof.account.cash == Decimal(99500)
        assert proof.actual_quantities == (("AAPL", Decimal(5)),)
        assert not proof.expected_quantities
        with db.transaction() as session:
            assert latest_certification(session) == original_certificate
            assert not StorageRepository(session).list_fills()
            assert entry_inventory_objections(store, session, proof, now=clocks.wall)
        assert not store.documents("core_cash_activities")
        clocks.advance(60)
        await synchronize(store, broker, now=clocks.wall)
        assert not store.latest_proof().cash_complete
        assert store.latest_proof().expected_cash is None
        await close(store, broker, clocks)
        process_outcomes(store, cutoff=clocks.wall)
        assert store.export_loop(first.id)["round_trip"]["net_pnl"] is None
        second, reasons = await enter(store, broker)
        if prior_integrity:
            with db.transaction() as session:
                assert incident_id in {h.id for h in active_health(session)}
            assert second.action == "WAIT" and "PRIOR_KNOWN_CASH_CONTRADICTION" in second.gates
        else:
            assert second.action == "BUY" and not reasons, (second.gates, reasons)
            assert not any(
                h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events")
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_partial_first_buy_activity_waits_without_restoring_revoked_cash(tmp_path):
    from quantbot.paper_core.funds import entry_inventory_objections
    from quantbot.paper_core.store import latest_certification

    class PartialPublicationProvider(DailyProvider):
        def __init__(self, clocks):
            super().__init__(clocks)
            self.publish_remaining = False

        async def request(self, method, url, **kwargs):
            result = await super().request(method, url, **kwargs)
            if (
                method == "POST"
                and url.endswith("/orders")
                and kwargs["json_body"]["side"] == "buy"
            ):
                fill = self.fills.pop()
                self.fills.extend(
                    (
                        dict(fill, id=fill["id"] + "-a", qty="2"),
                        dict(fill, id=fill["id"] + "-b", qty="3"),
                    )
                )
            if url.endswith("/account/activities") and not self.publish_remaining:
                rows = json.loads(result.content)
                result = result.model_copy(
                    update={
                        "content": json.dumps(
                            [row for row in rows if not row["id"].endswith("-b")]
                        ).encode()
                    }
                )
            return result

    db, store, broker, provider, clocks = await prepare(
        tmp_path, provider_factory=PartialPublicationProvider
    )
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        with db.transaction() as session:
            initial = latest_certification(session)
            assert not StorageRepository(session).list_fills()
        with pytest.raises(TimeoutError, match="OWNED_FILL_ACTIVITY_PENDING"):
            await synchronize(store, broker, now=clocks.wall)
        pending = next(
            h
            for h in store.documents("core_health_events")
            if "OWNED_FILL_ACTIVITY_PENDING" in h["objections"]
        )
        proof = store.read(
            "core_reconciliation_proofs", pending["evidence_ids"][0], ReconciliationProof
        )
        assert proof.expected_quantities == (("AAPL", Decimal(2)),)
        assert proof.actual_quantities == (("AAPL", Decimal(5)),)
        assert not proof.cash_complete and proof.expected_cash is None
        with db.transaction() as session:
            assert latest_certification(session) == initial
            assert sum(f.quantity for f in StorageRepository(session).list_fills()) == 2
            revocations = set(
                session.execute(
                    select(table("core_cash_authority_revocations").c.snapshot_id)
                ).scalars()
            )
            assert "core-certified-" + initial[1] in revocations
            assert entry_inventory_objections(store, session, proof, now=clocks.wall)
        with pytest.raises(StateConflictError, match="CASH_AUTHORITY_REVOKED"):
            store.latest_proof()
        store.runtime_role = "guardian"
        receipt = await guardian_cycle(store, broker, now=clocks.wall)
        assert "INVENTORY_ACQUISITION_RETRYABLE" in receipt.reasons
        assert "INVENTORY_MISMATCH" not in receipt.reasons
        assert not any(
            h["state"] == "INTEGRITY_FAILURE" for h in store.documents("core_health_events")
        )
        assert len(provider.orders) == 1 and not store.documents("core_cash_activities")

        provider.publish_remaining = True
        clocks.advance(1)
        await synchronize(store, broker, now=clocks.wall)
        current = store.latest_proof()
        assert current.expected_quantities == current.actual_quantities == (("AAPL", Decimal(5)),)
        assert not current.cash_complete and current.expected_cash is None
        with db.transaction() as session:
            assert sum(f.quantity for f in StorageRepository(session).list_fills()) == 5
            assert all(f.fee is None for f in StorageRepository(session).list_fills())
            assert revocations <= set(
                session.execute(
                    select(table("core_cash_authority_revocations").c.snapshot_id)
                ).scalars()
            )
        await close(store, broker, clocks)
        process_outcomes(store, cutoff=clocks.wall)
        trip = store.export_loop(first.id)["round_trip"]
        assert len(trip["entry_fill_ids"]) == 2
        assert trip["actual_fees"] is None and trip["net_pnl"] is None
        second, reasons = await enter(store, broker)
        assert second.action == "BUY" and not reasons, (second.gates, reasons)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_delayed_owned_fill_cannot_explain_an_unrelated_symbol_delta(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        provider.activity_lag_seconds = 60
        provider.holdings["XLE"] = Decimal(1)
        with pytest.raises(StateConflictError, match="INVENTORY_MISMATCH"):
            await close(store, broker, clocks)
        assert any(
            h["state"] == "INTEGRITY_FAILURE" and h["scope_id"] == "XLE"
            for h in store.documents("core_health_events")
        )
        with pytest.raises(StateConflictError, match="CASH_AUTHORITY_REVOKED"):
            await enter(store, broker)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [None, "forged", "future", "wrong-target", "wrong-model"])
async def test_collect_history_requires_exact_past_warmup_acknowledgement(tmp_path, fault):
    from quantbot.paper_core.learning import _historical_comparator, _select

    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        await close(store, broker, clocks)
        advance_to(clocks, SESSIONS[0] + timedelta(hours=6, minutes=30, seconds=3))
        await daily_consideration(store, broker, broker.config)
        model, active = selection(store)
        receipt = store.read("core_learning_runs", active.receipt_id, LearningReceipt)
        advance_to(clocks, SESSIONS[1] + timedelta(minutes=35, seconds=10))
        await catch_up_provider(store, broker, clocks)
        second, reasons = await enter(store, broker)
        assert second.action == "BUY" and not reasons
        await close(store, broker, clocks)
        advance_to(clocks, SESSIONS[1] + timedelta(hours=6, minutes=30, seconds=3))
        with db.transaction() as session:
            rows, _, _ = _select(session, receipt.policy, clocks.wall, execution_now=clocks.wall)
        row = next(r for r in rows if r["lot_id"] == second.id)
        if fault:
            acknowledgement = receipt
            if fault == "future":
                future = clocks.wall + timedelta(days=1)
                acknowledgement = receipt.model_copy(
                    update={
                        "available_at": future,
                        "completed_at": future,
                        "recorded_at_utc": future,
                    }
                )
            elif fault == "wrong-target":
                acknowledgement = receipt.model_copy(
                    update={
                        "policy": receipt.policy.model_copy(
                            update={
                                "target_contract": "owned-executed-net-return-v1",
                                "procedure_version": "daily-all-history-walk-forward-v2",
                            }
                        )
                    }
                )
            elif fault == "wrong-model":
                acknowledgement = receipt.model_copy(update={"parent_model_id": "wrong-model"})
            acknowledgement = acknowledgement.model_copy(update={"run_id": "negative-" + fault})
            store.record("core_learning_runs", acknowledgement)
            with db.transaction() as session:
                session.execute(
                    table("core_learning_considerations")
                    .insert()
                    .values(run_id=acknowledgement.run_id, receipt_id=acknowledgement.id)
                )
            # Fault controls retain valid content digests/mappings so target/time/model
            # checks are exercised. The actual active pointer is never changed.
            authority = store.read(
                "core_activation_authorities", active.authority_id, ActivationAuthority
            ).model_copy(
                update={
                    "receipt_id": acknowledgement.id,
                    "target_contract": acknowledgement.policy.target_contract,
                    "procedure_version": acknowledgement.policy.procedure_version,
                }
            )
            store.record("core_activation_authorities", authority)
            event = active.model_copy(
                update={
                    "receipt_id": acknowledgement.id,
                    "authority_id": authority.id,
                    "learning_run_id": acknowledgement.run_id,
                }
            )
            store.record("core_activations", event)
            second = second.model_copy(
                update={
                    "activation_id": event.id,
                    "learning_receipt_id": (
                        "missing-receipt" if fault == "forged" else acknowledgement.id
                    ),
                }
            )
            store.record("core_decisions", second)
            row = {**row, "lot_id": second.id}
        with db.transaction() as session:
            if fault:
                with pytest.raises(ValueError):
                    _historical_comparator(session, row, clocks.execution_domain, GROSS)
            else:
                resolved = _historical_comparator(session, row, clocks.execution_domain, GROSS)
                assert resolved[:3] == (model, None, None)
                assert resolved[3:5] == (receipt.id, receipt.available_at)
    finally:
        db.close()


@pytest.mark.asyncio
async def test_daily_hybrid_collect_fit_activate_signal_then_negative_exploration(tmp_path):
    # Catches broken current-funds continuation, daily-learning acknowledgement,
    # gross-target operational activation, signal priority and negative exploration.
    db, store, broker, provider, clocks = await prepare(tmp_path, provider_factory=DailyProvider)
    try:
        parent, initial = selection(store)
        assert parent.mode == "COLLECT" and parent.target_contract is None
        assert store.config.selection_policy == "SIGNAL_OR_EXPLORE_V1"
        assert broker.config.activation_policy == "DAILY_BOUNDED_V3"
        decisions, trips, warmups = [], [], []
        for day_index, session_open in enumerate(SESSIONS[:10]):
            for offset in range(6):
                advance_to(clocks, session_open + timedelta(minutes=35 + offset * 40, seconds=10))
                if offset == 0 and day_index:
                    await catch_up_provider(store, broker, clocks)
                sign = 1 if offset % 2 == 0 else -1
                provider.signs = dict.fromkeys(("AAPL", "MSFT"), sign)
                provider.exit_sign = sign
                decision, reasons = await enter(store, broker)
                assert decision.action == "BUY", (day_index, offset, decision.gates)
                assert not reasons, reasons
                assert decision.selection.kind == "EXPLORATION"
                assert decision.prediction_bps is None and decision.model_id == parent.id
                assert decision.model_mode == "COLLECT"
                if day_index:
                    assert decision.learning_receipt_id == warmups[-1].id
                decisions.append(decision)
                await close(store, broker, clocks)
                process_outcomes(store, cutoff=clocks.wall)
                exported = store.export_loop(decision.id)["round_trip"]
                assert exported is not None, (day_index, offset, decision.id)
                trip = RoundTrip.model_validate(exported)
                assert trip.status == "COSTS_PENDING" and trip.cost_reason == "FEE_UNAVAILABLE"
                assert trip.actual_fees is None and trip.net_pnl is None
                assert trip.gross_pnl / trip.entry_notional == Decimal(sign) / 500
                trips.append(trip)
            advance_to(clocks, session_open + timedelta(hours=6, minutes=30, seconds=3))
            result = await daily_consideration(store, broker, broker.config)
            _, active = selection(store)
            receipt = store.read("core_learning_runs", active.receipt_id, LearningReceipt)
            assert result[0] == "CONSIDERED_SESSION:" + session_open.date().isoformat(), result
            if day_index < 9:
                assert receipt.status == "NO_CHANGE"
                assert receipt.reason == "TEN_COMPLETED_SESSION_WARMUP"
                assert receipt.trial_charge_id is None
                assert active.model_id == parent.id and active.id != initial.id
                warmups.append(receipt)

        candidate, active = selection(store)
        receipt = store.read("core_learning_runs", active.receipt_id, LearningReceipt)
        assert receipt.status == "AWAITING_ACTIVATION", receipt.reason
        assert receipt.parent_model_id == parent.id != candidate.id == receipt.resulting_model_id
        assert candidate.mode == "PREDICT" and candidate.estimator is not None
        assert not candidate.fixture_authority and candidate.constant_bps is None
        assert candidate.learning_receipt_id == receipt.run_id
        assert receipt.policy.target_contract == candidate.target_contract == GROSS
        assert receipt.policy.procedure_version == V3
        assert len(receipt.final_input_ids) == 60
        assert set(receipt.final_input_ids) == {t.id for t in trips}
        assert [len(f.training_ids) for f in receipt.fold_results] == [30, 36, 42, 48, 54]
        assert [len(f.validation_ids) for f in receipt.fold_results] == [6] * 5
        assert receipt.final_raw_weights == (2.0,) * 60
        authority = store.read(
            "core_activation_authorities", active.authority_id, ActivationAuthority
        )
        with db.transaction() as session:
            commission = commission_in_session(store, session)
            assert all(f.fee is None for f in StorageRepository(session).list_fills())
        assert authority.runtime_commission_id == commission.id
        assert authority.candidate_id == candidate.id and authority.receipt_id == receipt.id
        assert authority.target_contract == GROSS and authority.procedure_version == V3
        assert len(trips) == 60 and {t.gross_pnl > 0 for t in trips} == {True, False}
        assert {d.symbol for d in decisions} == {"AAPL", "MSFT"}
        assert len(provider.orders) == 120 and not store.documents("core_cash_activities")

        advance_to(clocks, SESSIONS[10] + timedelta(minutes=35, seconds=3))
        await catch_up_provider(store, broker, clocks)
        provider.signs = {"AAPL": 1, "MSFT": -1}
        provider.exit_sign = 1
        signal, reasons = await enter(store, broker)
        assert signal.action == "BUY" and not reasons, (signal.gates, reasons)
        assert signal.selection.kind == "SIGNAL" and signal.symbol == "AAPL"
        assert signal.prediction_bps > 10
        mixed_scores = dict(
            zip(
                (o.symbol for o in signal.selection.cohort.observations),
                predict_bps(
                    candidate, tuple(dict(o.features) for o in signal.selection.cohort.observations)
                ),
                strict=True,
            )
        )
        assert mixed_scores["AAPL"] > 10 and mixed_scores["MSFT"] < 0
        await close(store, broker, clocks)
        process_outcomes(store, cutoff=clocks.wall)
        provider.signs = {"AAPL": -1, "MSFT": -1}
        provider.exit_sign = -1
        explore, reasons = await enter(store, broker)
        assert explore.action == "BUY" and not reasons, (explore.gates, reasons)
        assert explore.selection.kind == "EXPLORATION" and explore.symbol == "MSFT"
        assert explore.prediction_bps < 0
        negative_scores = dict(
            zip(
                (o.symbol for o in explore.selection.cohort.observations),
                predict_bps(
                    candidate,
                    tuple(dict(o.features) for o in explore.selection.cohort.observations),
                ),
                strict=True,
            )
        )
        assert all(score < 0 for score in negative_scores.values())
        for decision in (signal, explore):
            assert decision.model_id == candidate.id
            assert decision.learning_receipt_id == receipt.id
            assert decision.activation_id == active.id
            assert decision.model_digest == active.model_digest
        await close(store, broker, clocks)
        process_outcomes(store, cutoff=clocks.wall)
        for trip in trips:
            assert store.read("core_outcome_revisions", trip.id, RoundTrip) == trip
        proof = store.latest_proof()
        assert proof.funds_authority == "BROKER_PAPER_BALANCE_V1"
        assert not proof.cash_complete and proof.expected_cash is None
        assert not store.documents("core_cash_activities")
        outcomes = store.documents("core_outcome_revisions")
        assert len(outcomes) == 62
        assert all(o["actual_fees"] is None and o["net_pnl"] is None for o in outcomes)
        print(
            "DAILY_PAPER_LOOP_OFFLINE_PROVIDER_PROTOCOL_FIXTURE:"
            + json.dumps(
                dict(
                    role_readiness="INDEPENDENT_ROLE_HEARTBEAT_FIXTURE_ATTESTATION_ONLY",
                    initial_activation_id=initial.id,
                    warmup_receipt_ids=[r.id for r in warmups],
                    corpus_decision_ids=[d.id for d in decisions],
                    corpus_outcome_ids=[t.id for t in trips],
                    learning_receipt=receipt.model_dump(mode="json"),
                    activation=active.model_dump(mode="json"),
                    authority=authority.model_dump(mode="json"),
                    candidate=candidate.model_dump(mode="json"),
                    day11_decisions=[d.model_dump(mode="json") for d in (signal, explore)],
                    day11_cohort_predictions_bps=[mixed_scores, negative_scores],
                    final_proof_id=proof.id,
                    final_funds_acquisition_id=proof.acquisition_id,
                    broker_shaped_orders=len(provider.orders),
                    closed_outcomes=62,
                    cash_activities=0,
                    fees=None,
                    net_pnl=None,
                ),
                sort_keys=True,
            )
        )
    finally:
        db.close()
