"""OFFLINE_PROVIDER_PROTOCOL_FIXTURE: real adapter, no independent cash or fee invention."""

import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from tests.fixtures.core_m3 import Clocks
from tests.integration.paper_core.test_operational_adapter import ProviderFixture

from quantbot.brokers.alpaca import AlpacaPaperBrokerAdapter
from quantbot.brokers.base import BrokerTransportResponse
from quantbot.config import Settings
from quantbot.market_data.alpaca import AlpacaMarketDataClient
from quantbot.market_data.base import TransportResponse
from quantbot.market_data.calendar import AlpacaMarketCalendar
from quantbot.paper_core.__main__ import derived_core
from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
from quantbot.paper_core.authority import (
    RoleHeartbeat,
    RuntimeCommission,
    commission_runtime,
    publish_heartbeat,
)
from quantbot.paper_core.commissioning import acquire_commission_document
from quantbot.paper_core.contracts import CoreConfig
from quantbot.paper_core.guardian import acknowledge_handoffs, guardian_cycle, synchronize
from quantbot.paper_core.outcomes import process_outcomes
from quantbot.paper_core.release import RuntimeConfig, prepare_release
from quantbot.paper_core.roles import dispatch_entries
from quantbot.paper_core.store import CoreStore, table
from quantbot.paper_core.trader import trader_cycle
from quantbot.storage import Database, StorageRepository


class FundsProvider(ProviderFixture):
    def __init__(self, clocks):
        super().__init__(clocks.wall)
        self.clocks = clocks
        self.cash = Decimal(100000)
        self.fill_price = Decimal(100)
        self.orders = []
        self.fills = []
        self.holdings = {}
        self.account_overrides = {}
        self.open_orders = []
        self.delay = 0.01
        self.unstable = False

    async def request(self, method, url, **kwargs):
        self.clocks.advance(self.delay)
        self.now = self.clocks.wall
        status = 200
        if url.endswith("/account"):
            if self.unstable:
                self.cash += 1
            payload = dict(
                id="paper",
                account_number="offline",
                status="ACTIVE",
                cash=str(self.cash),
                equity="100000",
                buying_power=str(self.cash),
                currency="USD",
                trading_blocked=False,
                transfers_blocked=False,
                account_blocked=False,
                trade_suspended_by_user=False,
            )
            payload.update(self.account_overrides)
        elif url.endswith("/positions"):
            payload = [
                dict(
                    symbol=s,
                    qty=str(q),
                    side="long",
                    current_price="100",
                    market_value=str(q * 100),
                    avg_entry_price="100",
                )
                for s, q in self.holdings.items()
                if q
            ]
        elif url.endswith("/orders") and method == "POST":
            body = kwargs["json_body"]
            quantity = Decimal(body["qty"])
            sign = 1 if body["side"] == "buy" else -1
            self.cash -= sign * quantity * self.fill_price
            symbol = body["symbol"]
            self.holdings[symbol] = self.holdings.get(symbol, 0) + sign * quantity
            payload = dict(
                body,
                id=f"order-{len(self.orders)}",
                filled_qty=str(quantity),
                status="filled",
                submitted_at=self.now.isoformat(),
                filled_avg_price=str(self.fill_price),
            )
            self.orders.append(payload)
            self.fills.append(
                dict(
                    id=f"fill-{len(self.fills)}",
                    order_id=payload["id"],
                    activity_type="FILL",
                    symbol=symbol,
                    qty=str(quantity),
                    price=str(self.fill_price),
                    side=body["side"],
                    transaction_time=self.now.isoformat(),
                )
            )
        elif url.endswith("/orders:by_client_order_id"):
            payload = next(
                (
                    o
                    for o in self.orders
                    if o["client_order_id"] == kwargs["params"]["client_order_id"]
                ),
                None,
            )
            status = 200 if payload else 404
            payload = payload or {}
        elif url.endswith("/orders"):
            payload = self.open_orders
        elif "/account/activities" in url:
            payload = self.fills
        elif "/assets/" in url:
            payload = dict(
                symbol=url.rsplit("/", 1)[-1],
                **{"class": "us_equity"},
                status="active",
                tradable=True,
                fractionable=True,
            )
        else:
            response = await super().request(method, url, **kwargs)
            if url.endswith("/bars"):
                payload = json.loads(response.content)
                bars = payload["bars"]["XLE"]
                payload["bars"].update(AAPL=bars, MSFT=bars)
                return response.model_copy(update={"content": json.dumps(payload).encode()})
            return response
        self.calls.append((method, url, kwargs.get("params")))
        return BrokerTransportResponse(
            status_code=status, headers={}, content=json.dumps(payload).encode()
        )


async def prepare(
    tmp_path,
    *,
    hybrid=True,
    provider_factory=FundsProvider,
    entry_policy=None,
    initial_wall=None,
    warmup_seconds=300,
):
    clocks = Clocks()
    clocks.wall = initial_wall or datetime(2026, 10, 14, 14, 0, 3, tzinfo=UTC)
    provider = provider_factory(clocks)
    source = tmp_path / "canonical.db"
    Database.create(source).close()
    db = Database.create(tmp_path / "funds.db")
    config = RuntimeConfig(
        repository=str(Path(__file__).resolve().parents[3]),
        store_path=str(db.path),
        store_identity=db.store_identity,
        expected_account_hash=hashlib.sha256(b"paper").hexdigest(),
        core=CoreConfig(
            account_id="paper",
            symbol="XLE",
            symbols=("AAPL", "MSFT") if hybrid else (),
            selection_policy="SIGNAL_OR_EXPLORE_V1" if hybrid else "MODEL_ONLY_V1",
            evidence_lane="BROKER_PAPER",
            clock_profile="PAPER_TIME_V2",
            release_id="UNBOUND_PREPARED_RELEASE",
        ),
        holdout_source=str(source),
        canonical_datasets=("us-equities-daily",),
        learning_origin=clocks.wall - timedelta(days=1),
        activation_policy="DAILY_BOUNDED_V3" if hybrid else "DAILY_BOUNDED_V2",
    )
    if entry_policy is not None:
        config = config.model_copy(update={"entry_policy": entry_policy})
    release = prepare_release(config)

    class Market:
        async def request(self, *args, **kwargs):
            return TransportResponse.model_validate(
                (await provider.request(*args, **kwargs)).model_dump()
            )

    key = SecretStr("OFFLINE_PROTOCOL_FIXTURE")
    broker = AlpacaCoreBroker(
        AlpacaPaperBrokerAdapter(api_key=key, api_secret=key, transport=provider),
        AlpacaMarketDataClient(api_key=key, api_secret=key, transport=Market()),
        AlpacaMarketCalendar(api_key=key, api_secret=key, transport=Market()),
        config,
        clocks,
        account_id="paper",
    )
    document = await acquire_commission_document(
        release,
        broker,
        dict(
            release_id=release.id,
            approval_reference="OFFLINE_PROVIDER_PROTOCOL_FIXTURE",
            fenced_writer_evidence_ids=["ISOLATED_TEST_DATABASE"],
            legacy_owners=[],
        ),
    )
    store = CoreStore(db, derived_core(release, "paper"), clocks=clocks)
    commission_runtime(
        store,
        release,
        RuntimeCommission.model_validate(document["commission"]),
        baseline=document["baseline"],
    )
    store.runtime_settings = Settings(
        _env_file=None,
        EXPECTED_ACCOUNT_ID="paper",
        ALPACA_PAPER_API_KEY=key,
        ALPACA_PAPER_API_SECRET=key,
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )
    store.runtime_submission_enabled = True
    with db.transaction() as session:
        StorageRepository(session).set_kill_switch(
            False, reason="OFFLINE_PROVIDER_PROTOCOL_FIXTURE", updated_at=clocks.wall
        )
    clocks.advance(warmup_seconds)
    await synchronize(store, broker, now=clocks.wall)
    ready(store)
    return db, store, broker, provider, clocks


def ready(store, time_proof=None):
    now = store.clocks.sample()[0]
    store.runtime_role = "guardian"
    receipt = acknowledge_handoffs(store, now=now)
    # Explicit independent-role fixture attestation; no process independence claim.
    heartbeat = RoleHeartbeat(
        role="guardian",
        release_id=store.runtime_release_id,
        config_id=store.config_id,
        store_identity=store.database.store_identity,
        owner=store.config.owner,
        generation=store.config.generation,
        process_epoch=time_proof.process_id if time_proof else "OFFLINE_OTHER_GUARDIAN",
        pid=os.getpid() + 100000,
        recorded_at=now,
        valid_until=now + timedelta(seconds=120),
        last_useful_progress=now,
        objections=(),
        evidence_ids=(receipt.id, time_proof.id) if time_proof else (receipt.id,),
        proof_age_seconds=0,
        guardian_receipt_id=receipt.id,
    )
    publish_heartbeat(store, heartbeat)
    store.runtime_role = "trader"
    return receipt, heartbeat


async def enter(store, broker):
    ready(store)
    observation = (
        await broker.observation_cohort() if store.config.symbols else await broker.observation()
    )
    decision = await trader_cycle(
        store,
        broker,
        observation,
        broker.config.initial_model,
        store.latest_proof(),
        now=broker.now,
    )
    ready(store)
    reasons = await dispatch_entries(store, broker)
    return decision, reasons


async def close(store, broker, clocks):
    clocks.advance(1800)
    store.runtime_role = "guardian"
    receipt = await guardian_cycle(store, broker, now=clocks.wall)
    assert receipt.exit_intent_ids, receipt
    process_outcomes(store, cutoff=clocks.wall)
    clocks.advance(300)
    await synchronize(store, broker, now=clocks.wall)
    return receipt


async def candidate(store, broker, proof=None):
    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.selection import evaluate_cohort, record_cohort, reservation_state
    from quantbot.paper_core.time_evidence import acquire_time_evidence, project_time_evidence

    ready(store)
    cohort = await broker.observation_cohort()
    record_cohort(store, cohort)
    evidence = await acquire_time_evidence(broker, store.clocks)
    store.record("core_time_evidence", evidence)
    with store.database.transaction() as session:
        model, active = active_in_session(store, session)
        daily, prior, _ = reservation_state(store, session, cohort)
    return evaluate_cohort(
        store.config,
        cohort,
        model,
        proof or store.latest_proof(),
        session_counts=daily,
        prior_counts=prior,
        health_reasons={},
        activation=active,
        time_evidence=evidence,
        time_projection=project_time_evidence(evidence, store.clocks),
    )


@pytest.mark.asyncio
async def test_real_adapter_repeats_entries_without_fabricating_cash_or_fees(tmp_path):
    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decisions = []
        for index in range(6):
            decision, reasons = await enter(store, broker)
            assert decision.action == "BUY", decision.gates
            assert not reasons, reasons
            assert len(provider.orders) == index * 2 + 1
            decisions.append(decision)
            await close(store, broker, clocks)
        assert [d.symbol for d in decisions[:2]] == ["AAPL", "MSFT"]
        assert len(provider.orders) == 12
        decision, reasons = await enter(store, broker)
        assert len(provider.orders) == 12  # Seventh boundary cannot reserve or send.
        assert len([r for r in store.links() if r["role"] == "ENTRY"]) == 6
        proof = store.latest_proof()
        assert proof.expected_cash is None and not proof.cash_complete
        assert store.documents("core_cash_activities") == []
        with db.transaction() as session:
            assert all(f.fee is None for f in StorageRepository(session).list_fills())
            quantity_ids = set(
                session.execute(select(table("core_quantity_certifications").c.proof_id)).scalars()
            )
            assert proof.id in quantity_ids
            assert quantity_ids.isdisjoint(
                session.execute(select(table("core_inventory_certifications").c.proof_id)).scalars()
            )
            reconciled_ids = set(
                session.execute(select(table("reconciliation_runs").c.reconciliation_id)).scalars()
            )
            assert not any("core-" + identity in reconciled_ids for identity in quantity_ids)
        outcomes = store.documents("core_outcome_revisions")
        assert outcomes and all(o["actual_fees"] is None and o["net_pnl"] is None for o in outcomes)
        print(
            "OFFLINE_PROVIDER_PROTOCOL_FIXTURE:"
            + json.dumps(
                dict(
                    proof_id=proof.id,
                    acquisition_id=proof.acquisition_id,
                    decision_ids=[d.id for d in decisions],
                    symbols=[d.symbol for d in decisions],
                    broker_shaped_orders=len(provider.orders),
                    entry_reservations=6,
                    cash_activities=0,
                    actual_fees=None,
                    net_pnl=None,
                    full_cash_complete=proof.cash_complete,
                    quantity_certified=True,
                )
            )
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_direct_uncertified_proof_and_unmarked_incomplete_cash_cannot_claim(tmp_path):
    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        current = store.latest_proof()
        direct = store.proof(
            current.account,
            current.actual_quantities,
            now=clocks.wall,
            positions=current.positions,
            acquisition_id=current.acquisition_id,
        )
        marked = direct.model_copy(update={"funds_authority": "BROKER_PAPER_BALANCE_V1"})
        decision = await candidate(store, broker, marked)
        assert decision.action == "BUY"
        assert (
            claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
            is None
        )
        unmarked = await candidate(store, broker, direct)
        assert unmarked.action == "WAIT" and "INVENTORY_UNCERTIFIED" in unmarked.gates
        assert len(provider.orders) == 2
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_original_source", [False, True])
async def test_quantity_successor_keeps_original_source_binding(tmp_path, remove_original_source):
    from sqlalchemy import delete

    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        original = store.latest_proof()
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        await synchronize(store, broker, now=clocks.wall)
        assert store.latest_proof().id != original.id
        ready(store)
        if remove_original_source:
            with db.transaction() as session:
                session.execute(
                    delete(table("core_provider_acquisitions")).where(
                        table("core_provider_acquisitions").c.id == original.acquisition_id
                    )
                )
        reasons = await dispatch_entries(store, broker)
        if remove_original_source:
            assert "CORE_CASH_AUTHORITY_UNAVAILABLE" in reasons
            assert len(provider.orders) == 2
        else:
            assert not reasons
            assert len(provider.orders) == 3
    finally:
        db.close()


@pytest.mark.asyncio
async def test_balance_receipt_age_is_local_and_bounded_across_processes(tmp_path):
    from quantbot.paper_core.funds import entry_inventory_objections

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        proof = store.latest_proof()
        clocks.process_id = "OTHER_PROCESS_WITH_DIFFERENT_MONOTONIC_ORIGIN"
        clocks.mono = 0
        with db.transaction() as session:
            assert not entry_inventory_objections(
                store, session, proof, now=proof.recorded_at_utc + timedelta(seconds=120)
            )
            assert entry_inventory_objections(
                store, session, proof, now=proof.recorded_at_utc + timedelta(seconds=120.01)
            )
            assert entry_inventory_objections(
                store, session, proof, now=proof.recorded_at_utc - timedelta(seconds=0.01)
            )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_legacy_strict_mode_stays_blocked_after_unknown_fee_exit(tmp_path):
    from quantbot.paper_core.funds import entry_inventory_objections

    db, store, broker, provider, clocks = await prepare(tmp_path, hybrid=False)
    try:
        first, reasons = await enter(store, broker)
        assert first.action == "BUY" and not reasons
        await close(store, broker, clocks)
        second, reasons = await enter(store, broker)
        assert second.action == "WAIT" and "INVENTORY_UNCERTIFIED" in second.gates
        assert len(provider.orders) == 2
        with db.transaction() as session:
            assert entry_inventory_objections(store, session, store.latest_proof(), now=clocks.wall)
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["slow", "unstable", "known_cash", "quantity", "basis"])
async def test_acquisition_and_inventory_contradictions_never_grant_funds(tmp_path, fault):
    from quantbot.storage import StateConflictError

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        original = store.latest_proof().id
        if fault == "slow":
            provider.delay = 1
        elif fault == "unstable":
            provider.unstable = True
        elif fault == "known_cash":
            provider.cash -= 1
        elif fault == "quantity":
            provider.holdings["AAPL"] = Decimal(1)
        else:
            await enter(store, broker)
            await synchronize(store, broker, now=clocks.wall)
            original = store.latest_proof().id
            request = provider.request

            async def changed_basis(method, url, **kwargs):
                result = await request(method, url, **kwargs)
                if url.endswith("/positions"):
                    payload = json.loads(result.content)
                    payload[0]["avg_entry_price"] = "99"
                    result = result.model_copy(update={"content": json.dumps(payload).encode()})
                return result

            provider.request = changed_basis
        with pytest.raises((TimeoutError, StateConflictError)):
            await synchronize(store, broker, now=clocks.wall)
        assert store.latest_proof().id == original
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "cash",
        "buying_power",
        "equity",
        "restricted",
        "open_order",
        "manual",
        "quarantine",
        "source_race",
        "ledger_race",
        "unknown_send",
    ],
)
async def test_actual_coordinator_rechecks_funds_and_locked_source_before_post(
    tmp_path, monkeypatch, fault
):
    from sqlalchemy import delete

    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.runtime import CoreExecutionCoordinator
    from quantbot.paper_core.trader import claim_entry

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        decision = await candidate(store, broker)
        entry = claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
        assert entry is not None
        ready(store)
        if fault in {"cash", "buying_power", "equity"}:
            provider.account_overrides[fault] = "1"
        elif fault == "restricted":
            provider.account_overrides["trading_blocked"] = True
        elif fault == "open_order":
            provider.open_orders = [
                dict(
                    provider.orders[0],
                    id="unowned",
                    client_order_id="unowned",
                    status="new",
                    filled_qty="0",
                )
            ]
        elif fault == "manual":
            with db.transaction() as session:
                StorageRepository(session).set_kill_switch(
                    True, reason="OFFLINE_MANUAL_STOP", updated_at=clocks.wall
                )
        elif fault == "quarantine":
            with db.transaction() as session:
                StorageRepository(session).create_incident(
                    "disputed",
                    kind="CORE_EVIDENCE_CONFLICT",
                    severity="ERROR",
                    message="OFFLINE_DISPUTED_CASH",
                    detail={"account_scope": True},
                    occurred_at=clocks.wall,
                )
        elif fault == "unknown_send":
            from quantbot.domain import IntentState

            with db.transaction() as session:
                repo = StorageRepository(session)
                other = entry.intent.model_copy(
                    update={"intent_id": "other-unknown", "client_order_id": "other-unknown-client"}
                )
                repo.create_order_intent(other)
                for state in (
                    IntentState.ORDER_CREATED,
                    IntentState.SUBMITTING,
                    IntentState.SUBMISSION_UNKNOWN,
                ):
                    other = repo.transition_order_intent(
                        other.intent_id, state, expected_current=other.state
                    )
        else:
            gate = CoreExecutionCoordinator._gate
            observed = []

            def changed_after_gate(self, *args, **kwargs):
                result = gate(self, *args, **kwargs)
                observed.append(result.allowed)
                if len(observed) == 2:
                    assert result.allowed
                    with db.transaction() as session:
                        if fault == "source_race":
                            session.execute(
                                delete(table("core_provider_acquisitions")).where(
                                    table("core_provider_acquisitions").c.id
                                    == store.latest_proof().acquisition_id
                                )
                            )
                        else:
                            AlpacaCoreBroker.record_external(
                                session,
                                dict(
                                    event_id="new-account-event",
                                    account_id="paper",
                                    symbol="",
                                    owner="ACCOUNT_NONTRADE",
                                    quantity_delta="0",
                                    net_cash="1",
                                    order_id=None,
                                    occurred_at=clocks.wall.isoformat(),
                                    activity_type="CSD",
                                    source_document="OFFLINE_SOURCE_CHANGE",
                                ),
                            )
                return result

            monkeypatch.setattr(CoreExecutionCoordinator, "_gate", changed_after_gate)
        reasons = await dispatch_entries(store, broker)
        assert reasons
        assert len(provider.orders) == 2
        if fault in {"source_race", "ledger_race"}:
            assert observed == [True, True]
            assert "CORE_CASH_AUTHORITY_UNAVAILABLE" in reasons
    finally:
        db.close()


@pytest.mark.asyncio
async def test_protection_and_outcomes_continue_without_entry_funds(tmp_path):
    from quantbot.paper_core.funds import entry_inventory_objections
    from quantbot.paper_core.health import independent_cash_available

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        decision, reasons = await enter(store, broker)
        assert not reasons
        provider.open_orders = [
            dict(
                provider.orders[0],
                id="unowned",
                client_order_id="unowned",
                symbol="OTHER",
                status="new",
                filled_qty="0",
            )
        ]
        await synchronize(store, broker, now=clocks.wall)
        with db.transaction() as session:
            assert entry_inventory_objections(store, session, store.latest_proof(), now=clocks.wall)
            assert not independent_cash_available(store, session)
        await close(store, broker, clocks)
        assert len(provider.orders) == 2
        assert store.documents("core_round_trips")[0]["decision_id"] == decision.id
        assert store.documents("core_cash_activities") == []
    finally:
        db.close()


@pytest.mark.asyncio
async def test_operational_recovery_requires_certified_funds_and_preserves_integrity(tmp_path):
    from quantbot.paper_core.health import active_health, recover_health, report

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        report(
            store,
            role="TRADER",
            state="RETRYABLE_FAILURE",
            reasons=("OFFLINE_RETRY",),
            scope_id="MSFT",
            now=clocks.wall,
        )
        original = store.latest_proof()
        uncertified = original.model_copy(update={"generation": original.generation + 1})
        recover_health(store, uncertified, now=clocks.wall, symbol="MSFT")
        with db.transaction() as session:
            assert any(h.state == "RETRYABLE_FAILURE" for h in active_health(session))
        await synchronize(store, broker, now=clocks.wall)
        proof = store.latest_proof()
        assert not proof.cash_complete
        recover_health(store, proof, now=clocks.wall, symbol="MSFT")
        with db.transaction() as session:
            assert not active_health(session)
        report(
            store,
            role="TRADER",
            state="INTEGRITY_FAILURE",
            reasons=("DISPUTED_CASH",),
            scope="ACCOUNT",
            scope_id="paper",
            now=clocks.wall,
        )
        await synchronize(store, broker, now=clocks.wall)
        recover_health(store, store.latest_proof(), now=clocks.wall, symbol="MSFT")
        with db.transaction() as session:
            assert any(h.state == "INTEGRITY_FAILURE" for h in active_health(session))
    finally:
        db.close()


@pytest.mark.asyncio
async def test_explicit_resume_uses_current_funds_without_resolving_cash_history(tmp_path):
    from sqlalchemy import delete

    from quantbot.paper_core.activation import active_in_session
    from quantbot.paper_core.authority import commission_in_session, fence_entries, resume_entries
    from quantbot.paper_core.funds import PaperFundsEvidence
    from quantbot.paper_core.store import put
    from quantbot.paper_core.time_evidence import acquire_time_evidence
    from quantbot.storage import StateConflictError

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        fence = fence_entries(store, reason="OFFLINE_EXPLICIT_FENCE", evidence_ids=("FIXTURE",))
        process_id = clocks.process_id
        clocks.process_id = "OFFLINE_OTHER_GUARDIAN"
        time_proof = await acquire_time_evidence(broker, clocks)
        clocks.process_id = process_id
        store.record("core_time_evidence", time_proof)
        receipt, heartbeat = ready(store, time_proof)
        proof = store.latest_proof()
        funds = store.read("core_provider_acquisitions", proof.acquisition_id, PaperFundsEvidence)
        with db.transaction() as session:
            commission = commission_in_session(store, session)
            active = active_in_session(store, session)[1]
            session.execute(
                delete(table("core_provider_acquisitions")).where(
                    table("core_provider_acquisitions").c.id == funds.id
                )
            )
        request = dict(
            commission_id=commission.id,
            expected_fence_id=fence,
            runtime_release_id=store.runtime_release_id,
            expected_activation_id=active.id,
            guardian_receipt_id=receipt.id,
            guardian_heartbeat_id=heartbeat.id,
            proof_id=proof.id,
            time_evidence_id=time_proof.id,
            approved_at=clocks.wall.isoformat(),
            approval_reference="OFFLINE_EXPLICIT_RESUME",
        )
        with pytest.raises(StateConflictError, match="FRESH_PROOF"):
            resume_entries(store, request)
        with db.transaction() as session:
            put(session, "core_provider_acquisitions", funds.id, funds)
        assert resume_entries(store, request)
        assert store.latest_proof().expected_cash is None
        assert store.documents("core_cash_activities") == []
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "digest",
        "source",
        "endpoint",
        "account_id",
        "store_identity",
        "config_id",
        "commission_id",
        "evidence_lane",
        "stale",
        "slow",
        "unstable_account",
        "unstable_positions",
        "restricted",
        "open_order",
        "baseline",
    ],
)
async def test_source_receipt_is_bound_at_claim_and_locked_send(tmp_path, fault):
    from sqlalchemy import delete, update

    from quantbot.paper_core.contracts import ProtectionPlan
    from quantbot.paper_core.funds import PaperFundsEvidence, entry_inventory_objections
    from quantbot.paper_core.send_guard import compatible_successor, final_send_objections
    from quantbot.paper_core.store import certify_inventory, put
    from quantbot.paper_core.trader import claim_entry
    from quantbot.research.manifest import canonical_result_json, content_id
    from quantbot.risk import OrderPurpose

    db, store, broker, provider, clocks = await prepare(tmp_path)
    try:
        await enter(store, broker)
        await close(store, broker, clocks)
        current = store.latest_proof()
        assert not current.cash_complete
        receipt = json.loads(
            canonical_result_json(
                store.read("core_provider_acquisitions", current.acquisition_id, PaperFundsEvidence)
            )
        )
        if fault in {
            "source",
            "endpoint",
            "account_id",
            "store_identity",
            "config_id",
            "commission_id",
            "evidence_lane",
        }:
            receipt[fault] = "EXPLICIT_SYNTHETIC_TEST" if fault == "source" else "wrong"
        elif fault == "stale":
            receipt["capture_started"] = (clocks.wall - timedelta(seconds=122)).isoformat()
            receipt["recorded_at_utc"] = (clocks.wall - timedelta(seconds=121)).isoformat()
        elif fault == "slow":
            receipt["elapsed_seconds"] = 5.01
        elif fault == "unstable_account":
            receipt["second_account"]["account"]["cash"] = "1"
        elif fault == "unstable_positions":
            receipt["second_positions"] = [
                dict(
                    symbol="AAPL",
                    quantity="1",
                    market_price="100",
                    market_value="100",
                    average_entry_price="100",
                )
            ]
        elif fault == "restricted":
            receipt["first_account"]["order_entry_allowed"] = False
            receipt["second_account"] = receipt["first_account"]
        elif fault == "open_order":
            receipt["first_open_orders"] = [
                (
                    await broker.get_order_by_client_id(provider.orders[0]["client_order_id"])
                ).model_dump(mode="json")
            ]
        identity = content_id(receipt)
        with db.transaction() as session:
            put(session, "core_provider_acquisitions", identity, receipt)
        forged = current.model_copy(
            update={"generation": current.generation + 1, "acquisition_id": identity}
        )
        certify_inventory(store, forged)
        decision = await candidate(store, broker)
        assert decision.action == "BUY", decision.gates
        if fault in {"missing", "digest", "baseline"}:
            # Claim valid authority first so send's same-proof path is exercised after corruption.
            entry = claim_entry(
                store, decision, ProtectionPlan.for_decision(decision, store.config)
            )
            with db.transaction() as session:
                assert entry is not None, (
                    entry_inventory_objections(store, session, forged, now=clocks.wall),
                    store.documents("core_health_events"),
                )
                from quantbot.domain import IntentState

                intent = StorageRepository(session).transition_order_intent(
                    entry.intent.intent_id,
                    IntentState.ORDER_CREATED,
                    expected_current=IntentState.RISK_APPROVED,
                )
            ready(store)
            assert not final_send_objections(
                store, intent, OrderPurpose.ENTRY, clock=lambda: clocks.wall
            )
            with db.transaction() as session:
                if fault == "missing":
                    session.execute(
                        delete(table("core_provider_acquisitions")).where(
                            table("core_provider_acquisitions").c.id == identity
                        )
                    )
                elif fault == "digest":
                    receipt["process_id"] = "changed-content-with-old-id"
                    session.execute(
                        update(table("core_provider_acquisitions"))
                        .where(table("core_provider_acquisitions").c.id == identity)
                        .values(payload=canonical_result_json(receipt))
                    )
                else:
                    session.execute(update(table("core_fixture_baselines")).values(id="changed"))
            assert final_send_objections(
                store, intent, OrderPurpose.ENTRY, clock=lambda: clocks.wall
            )
        else:
            assert (
                claim_entry(store, decision, ProtectionPlan.for_decision(decision, store.config))
                is None
            )
        with db.transaction() as session:
            assert entry_inventory_objections(store, session, forged, now=clocks.wall)
            assert not compatible_successor(store, session, decision, forged, None, now=clocks.wall)
        assert len(provider.orders) == 2
    finally:
        db.close()
