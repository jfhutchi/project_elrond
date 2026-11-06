"""Fixture initialization only. Operational launcher and model activation are later milestones."""

from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from quantbot.brokers import BrokerAdapter
from quantbot.brokers.base import BrokerAccountSnapshot
from quantbot.config import Settings
from quantbot.domain import BrokerOrder, OrderIntent, Position
from quantbot.execution import ExecutionCoordinator
from quantbot.execution.coordinator import SubmissionPolicy, SubmissionResult
from quantbot.paper_core.contracts import GuardianReceipt, ReconciliationProof
from quantbot.paper_core.entry_policy import (
    AssetEligibility,
    decision_entry_authority,
    entry_risk_allowed,
)
from quantbot.paper_core.funds import (
    entry_inventory_objections,
    paper_balance_policy,
    same_spending_authority,
)
from quantbot.paper_core.release import prepare_release, read_status, verify_release
from quantbot.paper_core.send_guard import final_send_in_session, record_pre_send_refusal
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, put, table
from quantbot.research.manifest import content_id
from quantbot.risk import DrawdownState, OrderGateDecision, OrderPurpose, PortfolioVetoDecision
from quantbot.storage import StorageRepository

__all__ = ["prepare_release", "read_status", "verify_release"]


def require_synthetic(store: CoreStore, broker: SyntheticBroker) -> None:
    from quantbot.paper_core.local_simulation import LocalSimulationBroker

    matches_lane = (
        isinstance(broker, LocalSimulationBroker)
        if store.config.evidence_lane == "LOCAL_SIM_REAL_DATA"
        else isinstance(broker, SyntheticBroker) and not isinstance(broker, LocalSimulationBroker)
    )
    if not matches_lane or broker.account_id != store.config.account_id:
        raise ValueError("M1 permits only an explicit matching synthetic broker")


def require_runtime_broker(store: CoreStore, broker: object) -> None:
    from quantbot.paper_core.alpaca_adapter import AlpacaCoreBroker
    from quantbot.paper_core.authority import commission_in_session

    if store.config.evidence_lane != "BROKER_PAPER":
        if not isinstance(broker, SyntheticBroker):
            raise ValueError("fixture broker required")
        require_synthetic(store, broker)
        return
    if type(broker) is not AlpacaCoreBroker:
        raise ValueError("exact commissioned PAPER adapter required")
    with store.database.transaction() as session:
        commission = commission_in_session(store, session)
    if broker.account_id != commission.account_id or broker.endpoint != commission.endpoint:
        raise ValueError("commissioned broker identity mismatch")


def coordinator(store: CoreStore, broker: object) -> ExecutionCoordinator:
    require_runtime_broker(store, broker)
    if isinstance(broker, SyntheticBroker):
        legacy = fixture_coordinator(store, broker)
        if store.runtime_role is None:
            return legacy
        return CoreExecutionCoordinator(store, cast(BrokerAdapter, broker), legacy._settings)
    settings = getattr(store, "runtime_settings", None)
    if not isinstance(settings, Settings):
        raise ValueError("existing operational Settings authority required")
    return CoreExecutionCoordinator(
        store,
        cast(BrokerAdapter, broker),
        settings,
    )


class CoreExecutionCoordinator(ExecutionCoordinator):
    """Only optional general entry policies change; parent outbox/send/recovery is retained."""

    def __init__(self, store: CoreStore, broker: BrokerAdapter, settings: Settings) -> None:
        self.store = store
        self._entry_asset: AssetEligibility | None = None
        from quantbot.paper_core.portfolio import CurrentPortfolioSnapshot

        self._entry_snapshot: CurrentPortfolioSnapshot | None = None
        self._entry_acquired_at = store.clocks.sample()[0]
        self._entry_quote_id: str | None = None
        self._entry_quote: (
            tuple[Decimal, datetime, Literal["IEX_ASK_V1", "EXPLICIT_OFFLINE_MARK"]] | None
        ) = None
        super().__init__(
            store.database,
            broker,
            settings,
            submission_policy=SubmissionPolicy(max_not_found_retries=0),
            transactional_send_check=partial(
                final_send_in_session,
                store,
                clock=lambda: store.clocks.sample()[0],
                asset_eligibility=lambda: self._entry_asset,
                market_snapshot=lambda: self._entry_snapshot,
            ),
            transactional_send_refusal=self._record_send_refusal,
        )

    async def _begin_entry_acquisition(self, intent: OrderIntent, purpose: OrderPurpose) -> None:

        self._entry_acquired_at = self.store.clocks.sample()[0]
        self._entry_snapshot = None
        self._entry_quote = None
        self._entry_quote_id = None
        if purpose is OrderPurpose.ENTRY and self.store.parallel_entries:
            source = getattr(self._broker, "entry_price_evidence", None)
            if source is not None:
                quote = await source(intent.symbol)
                self._entry_quote = (quote.ask, quote.source_at, "IEX_ASK_V1")
                self._entry_quote_id = quote.id
            elif self.store.config.evidence_lane != "BROKER_PAPER":
                mark = cast(Any, self._broker).mark(intent.symbol)
                if mark is not None:
                    self._entry_quote = (mark[0], mark[1], "EXPLICIT_OFFLINE_MARK")

    async def submit_durable(
        self,
        intent: OrderIntent,
        *,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None = None,
    ) -> SubmissionResult:
        # Neither a slow initial fetch nor final fetch may receive a fresh label afterward.
        # Possibly-sent recovery never gains a new identity or a second POST here.
        from quantbot.domain import IntentState

        current = self._stored_intent(intent)
        if current.state in {
            IntentState.RISK_APPROVED,
            IntentState.ORDER_CREATED,
        }:
            if purpose is OrderPurpose.ENTRY:
                from quantbot.execution import OrderSubmissionBlocked
                from quantbot.paper_core.send_guard import REFUSAL, retire_stale_entries

                retired = retire_stale_entries(
                    self.store,
                    clock=lambda: self.store.clocks.sample()[0],
                    intent_id=current.intent_id,
                )
                if retired:
                    with self.store.database.transaction() as session:
                        events = StorageRepository(session).list_order_events(
                            intent_id=current.intent_id
                        )
                    reasons: list[str] = []
                    for event in events:
                        retained = event.detail.get("reasons")
                        if event.event_type == REFUSAL and isinstance(retained, (tuple, list)):
                            reasons.extend(reason for reason in retained if isinstance(reason, str))
                    raise OrderSubmissionBlocked(
                        OrderGateDecision(
                            purpose=purpose,
                            allowed=False,
                            reasons=(*dict.fromkeys(reasons), "CORE_ENTRY_PREACQUISITION_REFUSED"),
                            broker_label=self._settings.BROKER_PROVIDER.value,
                            environment_label=self._settings.BROKER_ENVIRONMENT.value,
                        )
                    )
            if purpose is OrderPurpose.ENTRY and self.store.config.evidence_lane == "BROKER_PAPER":
                # PAPER claims already retain their risk decision and Guardian handoff.
                # ORDER_CREATED remains unsent: the parent acquires every volatile
                # constraint once, runs _gate, then checks authority and claims
                # SUBMITTING under the same writer fence before any POST.
                if current.state is IntentState.RISK_APPROVED:
                    intent = self._transition(
                        current.intent_id, IntentState.ORDER_CREATED, IntentState.RISK_APPROVED
                    )
            else:
                await self._begin_entry_acquisition(intent, purpose)
        from quantbot.paper_core.account_control import LocalRequestBudgetRefusal, request_priority

        try:
            with request_priority(
                "OWNED_EXIT" if purpose is OrderPurpose.STRATEGY_EXIT else "ENTRY"
            ):
                return await super().submit_durable(
                    intent,
                    purpose=purpose,
                    market_eligible=market_eligible,
                    drawdown=drawdown,
                    risk_approved=risk_approved,
                    vetoes=vetoes,
                )
        except LocalRequestBudgetRefusal as error:
            # This exact local transport exception is raised before any awaited POST I/O.
            # A real HTTP rejection/timeout/404 is never promoted to no-send evidence.
            if error.method == "POST" and error.client_order_id == intent.client_order_id:
                with self.store.database.transaction() as session:
                    repo = StorageRepository(session)
                    current = repo.get_order_intent(intent.intent_id)
                    if (
                        current is not None
                        and current.state is IntentState.REJECTED
                        and (
                            repo.get_broker_order_by_client_order_id(intent.client_order_id) is None
                        )
                    ):
                        repo.record_order_event(
                            "core-local-nosend-" + content_id(intent.intent_id),
                            event_type="CORE_LOCAL_SEND_NOT_ATTEMPTED",
                            occurred_at=self.store.clocks.sample()[0],
                            intent_id=intent.intent_id,
                            detail={
                                "contract": "ACCOUNT_BUDGET_BEFORE_IO_V1",
                                "client_order_id": intent.client_order_id,
                                "purpose": purpose.value,
                                "definitively_unsent": True,
                            },
                        )
            raise

    async def _acquire_send_constraints(self, intent: OrderIntent, purpose: OrderPurpose) -> None:
        reserve = getattr(self._broker, "reserve_submission", None)
        if reserve is not None:
            reserve(intent.client_order_id)
        await self._begin_entry_acquisition(intent, purpose)
        if purpose is OrderPurpose.ENTRY and self.store.entry_policy is not None:
            self._entry_asset = await cast(Any, self._broker).entry_asset_eligibility(intent.symbol)

    def _record_send_refusal(
        self, session: Session, intent: OrderIntent, purpose: OrderPurpose, reasons: tuple[str, ...]
    ) -> None:
        evidence_ids: tuple[str, ...] = ()
        if self._entry_asset is not None:
            put(session, "core_provider_acquisitions", self._entry_asset.id, self._entry_asset)
            evidence_ids = (self._entry_asset.id, self._entry_asset.entry_policy_id)
        record_pre_send_refusal(
            session,
            intent,
            purpose,
            reasons,
            clock=lambda: self.store.clocks.sample()[0],
            evidence_ids=evidence_ids,
        )

    def _adopt(self, intent: OrderIntent, order: BrokerOrder) -> OrderIntent:
        # Another independent recovery reader may advance SUBMITTING while POST is in flight.
        # Revalidate the immutable request and adopt its actual acknowledgement under one lock.
        from sqlalchemy import text

        from quantbot.brokers.base import BrokerProtocolError
        from quantbot.paper_core.outcomes import _adopt

        with self.store.database.transaction() as session:
            session.execute(text("BEGIN IMMEDIATE"))
            repo = StorageRepository(session)
            current = repo.get_order_intent(intent.intent_id)
            if current is None or current.model_copy(update={"state": intent.state}) != intent:
                raise BrokerProtocolError("acknowledgement request changed during recovery")
            _adopt(repo, current, order)
            adopted = repo.get_order_intent(intent.intent_id)
            assert adopted is not None
            return adopted

    async def _open_exit_orders(self, purpose: OrderPurpose) -> tuple[BrokerOrder, ...]:
        if purpose is OrderPurpose.ENTRY and paper_balance_policy(self.store.config):
            return tuple(await self._broker.get_open_orders())
        return await super()._open_exit_orders(purpose)

    def _gate(
        self,
        account_snapshot: BrokerAccountSnapshot,
        positions: tuple[Position, ...],
        intent: OrderIntent,
        purpose: OrderPurpose,
        market_eligible: bool,
        drawdown: DrawdownState | None,
        risk_approved: bool | None,
        vetoes: PortfolioVetoDecision | None,
        open_orders: tuple[BrokerOrder, ...],
    ) -> OrderGateDecision:
        from quantbot.config import validate_trading_authority
        from quantbot.execution.coordinator import _competing_exit, _risk_reducing_exit_reasons
        from quantbot.paper_core.authority import runtime_objections
        from quantbot.paper_core.health import entry_health_objections
        from quantbot.risk import OrderGateDecision, OrderPurpose

        safety = validate_trading_authority(self._settings, account_snapshot.account.account_id)
        reasons = list(safety.reasons)
        with self.store.database.transaction() as session:
            if self.store.config.evidence_lane == "BROKER_PAPER":
                reasons.extend(
                    runtime_objections(self.store, session, entry=purpose is OrderPurpose.ENTRY)
                )
            repo = StorageRepository(session)
            if purpose is OrderPurpose.ENTRY:
                from quantbot.paper_core.account_control import entry_lease_objections

                reasons.extend(entry_lease_objections(self.store))
                if not self.store.runtime_entry_enabled:
                    reasons.append("CORE_ENTRY_DISABLED")
                if self._settings.KILL_SWITCH or repo.get_kill_switch_state().engaged:
                    reasons.append("CORE_MANUAL_ENTRY_PAUSE")
                reasons.extend(entry_health_objections(self.store, session, intent.symbol))
                proof = self.store.latest_proof()
                # Equity is a market mark. Cash and spending authority must still
                # match certification; the current equity risk limits are checked below.
                current_account = account_snapshot.account
                certified_account = proof.account
                if not same_spending_authority(
                    certified_account, current_account
                ) or entry_inventory_objections(
                    self.store, session, proof, now=self.store.clocks.sample()[0]
                ):
                    reasons.append("CORE_CURRENT_CASH_OR_ACCOUNT_UNCERTIFIED")
                if paper_balance_policy(self.store.config):
                    from quantbot.paper_core.funds import open_order_objections

                    reasons.extend(open_order_objections(self.store, session, open_orders))
                actual = tuple(sorted((p.symbol, p.quantity) for p in positions if p.quantity != 0))
                if actual != tuple(sorted(proof.actual_quantities)):
                    reasons.append("CORE_INHERITED_EXPOSURE_CHANGED")
                if sorted(
                    (p.symbol, p.quantity, p.average_entry_price) for p in positions
                ) != sorted((p.symbol, p.quantity, p.average_entry_price) for p in proof.positions):
                    reasons.append("CORE_INHERITED_BASIS_CHANGED")
                if any(p.symbol == intent.symbol and p.quantity != 0 for p in positions):
                    reasons.append("CORE_SYMBOL_ALREADY_HELD")
                links = [r for r in self.store.links() if r["intent_id"] == intent.intent_id]
                if not links:
                    reasons.append("CORE_ENTRY_HANDOFF_MISSING")
                else:
                    from quantbot.paper_core.contracts import Decision

                    decision = self.store.read("core_decisions", links[0]["decision_id"], Decision)
                    policy_reasons, minimum_share = decision_entry_authority(
                        self.store,
                        session,
                        decision,
                        now=self.store.clocks.sample()[0],
                        fresh=self._entry_asset,
                        require_fresh=self._entry_asset is not None,
                    )
                    reasons.extend(policy_reasons)
                    account = account_snapshot.account

                    available = None
                    risk_price = decision.price
                    if self.store.parallel_entries:
                        from quantbot.paper_core.portfolio import (
                            CurrentPortfolioSnapshot,
                            current_entry_price,
                            portfolio_budget,
                        )

                        self._entry_snapshot = CurrentPortfolioSnapshot(
                            recorded_at_utc=self._entry_acquired_at,
                            evidence_lane=self.store.config.evidence_lane,
                            release_id=self.store.config.release_id,
                            account_id=self.store.config.account_id,
                            store_identity=self.store.database.store_identity,
                            config_id=self.store.config_id,
                            policy_id=self.store.config.policy_id,
                            proof_id=proof.id,
                            account=account,
                            positions=positions,
                            entry_symbol=intent.symbol,
                            entry_price=self._entry_quote[0] if self._entry_quote else None,
                            entry_price_asof=self._entry_quote[1] if self._entry_quote else None,
                            entry_price_source=self._entry_quote[2] if self._entry_quote else None,
                            entry_quote_id=self._entry_quote_id,
                        )
                        budget = portfolio_budget(
                            self.store,
                            session,
                            proof,
                            now=self.store.clocks.sample()[0],
                            exclude_intent=intent.intent_id,
                            market_snapshot=self._entry_snapshot,
                        )
                        reasons.extend(budget.objections)
                        available = budget.available_notional
                        quote_reasons, risk_price = current_entry_price(
                            self.store,
                            self._entry_snapshot,
                            intent,
                            decision,
                            now=self.store.clocks.sample()[0],
                        )
                        reasons.extend(quote_reasons)
                        from quantbot.paper_core.contracts import ProtectionPlan

                        stop = ProtectionPlan.for_decision(decision, self.store.config).stop_price
                        if intent.quantity * (risk_price - stop) > min(
                            budget.risk_headroom, account.equity / 400
                        ):
                            reasons.append("CORE_FIXED_RISK_ENVELOPE")
                    if not entry_risk_allowed(
                        account,
                        intent.quantity,
                        risk_price,
                        minimum_whole_share=minimum_share,
                        config=self.store.config,
                        policy=self.store.entry_policy,
                        available_notional=available,
                    ):
                        reasons.append("CORE_FIXED_RISK_ENVELOPE")
            elif purpose is OrderPurpose.STRATEGY_EXIT:
                if self.store.config.evidence_lane == "BROKER_PAPER":
                    from quantbot.paper_core.send_guard import owned_exit_objections

                    reasons.extend(owned_exit_objections(self.store, session, intent))
                reasons.extend(_risk_reducing_exit_reasons(intent, positions))
                if any(
                    _competing_exit(p, intent) for p in repo.list_unresolved_order_intents()
                ) or any(
                    o.symbol == intent.symbol
                    and o.side is intent.side
                    and o.client_order_id != intent.client_order_id
                    for o in open_orders
                ):
                    reasons.append("EXIT_REDUCING_ORDER_PENDING")
            else:
                reasons.append("CORE_UNSUPPORTED_ORDER_PURPOSE")
        if not getattr(self.store, "runtime_submission_enabled", False):
            reasons.append("CORE_SUBMISSION_DISABLED")
        if not market_eligible:
            reasons.append("MARKET_NOT_ELIGIBLE")
        if not account_snapshot.order_entry_allowed:
            reasons.append("BROKER_ORDER_ENTRY_DISABLED")
        if account_snapshot.restrictions:
            reasons.append("BROKER_ACCOUNT_RESTRICTED")
        return OrderGateDecision(
            purpose=purpose,
            allowed=not reasons,
            reasons=tuple(dict.fromkeys(reasons)),
            broker_label=safety.broker_label,
            environment_label=safety.environment_label,
        )


def initialize_fixture(
    store: CoreStore, broker: SyntheticBroker, *, now: datetime, entry_enabled: bool = False
) -> ReconciliationProof:
    require_synthetic(store, broker)
    baseline = {
        "account": broker.snapshot().account.model_dump(mode="json"),
        "quantities": tuple(sorted((s, q) for s, q in broker.quantities.items() if q != 0)),
        "positions": broker.position_snapshot(),
        "source": "LOCAL_SIMULATION_INITIAL_STATE"
        if store.config.evidence_lane == "LOCAL_SIM_REAL_DATA"
        else "EXPLICIT_SYNTHETIC_INITIAL_STATE",
    }
    with store.database.transaction() as session:
        if session.execute(select(table("core_fixture_baselines").c.id)).first() is not None:
            raise ValueError("fixture baseline can be commissioned only once")
        if (
            StorageRepository(session).list_fills()
            or StorageRepository(session).list_broker_orders()
            or StorageRepository(session).list_account_snapshots()
            or StorageRepository(session).list_reconciliations()
            or StorageRepository(session).get_kill_switch_state().reason
            != "default fail-closed state"
        ):
            raise ValueError("fixture initialization requires an empty execution ledger")
        put(session, "core_fixture_baselines", content_id(baseline), baseline)
        # An explicit empty fixture's default stop is set by its author, never a runtime resume.
        if entry_enabled:
            StorageRepository(session).set_kill_switch(
                False, reason="explicit synthetic fixture setup", updated_at=now
            )
    proof = store.proof(
        broker.snapshot().account,
        tuple(sorted((s, q) for s, q in broker.quantities.items() if q != 0)),
        now=now,
        positions=broker.position_snapshot(),
        acquisition_id="explicit-synthetic-initial-state",
    )
    certify_inventory(store, proof)
    store.record(
        "core_guardian_receipts",
        GuardianReceipt(
            recorded_at_utc=now,
            evidence_lane=store.config.evidence_lane,
            release_id=store.config.release_id,
            owner=store.config.owner,
            generation=store.config.generation,
            valid_until=now + timedelta(seconds=120),
        ),
    )
    return proof


def fixture_coordinator(store: CoreStore, broker: SyntheticBroker) -> ExecutionCoordinator:
    require_synthetic(store, broker)
    # Nonsecret placeholders satisfy the retained coordinator's credential-completeness contract.
    settings = Settings(
        _env_file=None,
        EXPECTED_ACCOUNT_ID=store.config.account_id,
        ALPACA_PAPER_API_KEY="synthetic-key",
        ALPACA_PAPER_API_SECRET="synthetic-secret",
        KILL_SWITCH=False,
        BROKER_HEALTHY=True,
        MARKET_DATA_HEALTHY=True,
        RISK_ENGINE_HEALTHY=True,
        RECONCILIATION_SUCCESSFUL=True,
    )
    return ExecutionCoordinator(
        store.database,
        cast(BrokerAdapter, broker),
        settings,
        submission_policy=SubmissionPolicy(max_not_found_retries=0),
        transactional_send_check=partial(final_send_in_session, store, clock=lambda: broker.now),
    )
