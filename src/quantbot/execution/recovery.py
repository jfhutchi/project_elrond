"""Fail-closed startup reconstruction and broker reconciliation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from quantbot.brokers import BrokerAdapter, BrokerHealth
from quantbot.brokers.base import BrokerModel
from quantbot.config import Settings, validate_trading_safety
from quantbot.domain import BrokerOrder, IntentState, OrderIntent
from quantbot.execution.coordinator import persist_broker_acknowledgement
from quantbot.execution.reconciliation import (
    ReconciliationPolicy,
    ReconciliationResult,
    ReconciliationSnapshot,
    compare_reconciliation,
    missing_local_snapshot_result,
    record_reconciliation,
    snapshot_acquisition_failure,
)
from quantbot.execution.snapshot_capture import (
    Capture,
    capture_account_positions,
    validate_capture_freshness,
)
from quantbot.storage import Database, StorageRepository

_TERMINAL_ORDER_STATUSES = frozenset(
    {"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "stopped"}
)


class StartupRecoveryResult(BrokerModel):
    ready: bool
    reasons: tuple[str, ...]
    reconciliation: ReconciliationResult
    resolved_intent_ids: tuple[str, ...]
    unresolved_intent_ids: tuple[str, ...]
    broker_health: BrokerHealth


def is_open_order(order: BrokerOrder) -> bool:
    """Whether a stored order is still working, by its last recorded status."""
    return order.status.strip().lower() not in _TERMINAL_ORDER_STATUSES


_is_open_order = is_open_order


class StartupRecovery:
    """Resolve pending submissions and compare read-only broker state before readiness."""

    def __init__(
        self,
        database: Database,
        broker: BrokerAdapter,
        settings: Settings,
        *,
        reconciliation_policy: ReconciliationPolicy | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._broker = broker
        self._settings = settings
        self._policy = reconciliation_policy or ReconciliationPolicy()
        self._clock = clock or (lambda: datetime.now(UTC))

    def _pending_intents(self) -> tuple[OrderIntent, ...]:
        with self._database.transaction() as session:
            return tuple(StorageRepository(session).list_unresolved_order_intents())

    def _mark_unknown(self, intent: OrderIntent) -> OrderIntent:
        with self._database.transaction() as session:
            return StorageRepository(session).transition_order_intent(
                intent.intent_id,
                IntentState.SUBMISSION_UNKNOWN,
                expected_current=IntentState.SUBMITTING,
            )

    def _adopt(self, intent: OrderIntent, order: BrokerOrder) -> OrderIntent:
        with self._database.transaction() as session:
            current = StorageRepository(session).get_order_intent(intent.intent_id)
            if current is None:
                raise RuntimeError(f"order intent disappeared during recovery: {intent.intent_id}")
            return persist_broker_acknowledgement(StorageRepository(session), current, order)

    async def _recover_submissions(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        resolved: list[str] = []
        unresolved: list[str] = []
        for intent in self._pending_intents():
            if intent.state not in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}:
                continue
            found = await self._broker.get_order_by_client_id(
                intent.client_order_id,
                expected=intent,
            )
            if found is None:
                if intent.state is IntentState.SUBMITTING:
                    self._mark_unknown(intent)
                unresolved.append(intent.intent_id)
                continue
            self._adopt(intent, found)
            resolved.append(intent.intent_id)
        return tuple(sorted(resolved)), tuple(sorted(unresolved))

    def _local_snapshot(self, account_id: str) -> ReconciliationSnapshot | None:
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            account_snapshot = repository.get_latest_account_snapshot(account_id)
            if account_snapshot is None:
                return None
            return ReconciliationSnapshot(
                account=account_snapshot.account,
                positions=account_snapshot.positions,
                open_orders=tuple(
                    order for order in repository.list_broker_orders() if _is_open_order(order)
                ),
                fills=tuple(repository.list_fills()),
            )

    async def prepare_snapshots(self) -> tuple[Capture, ...]:
        return (
            await capture_account_positions(
                self._broker,
                clock=self._clock,
                clock_healthy=lambda at: at.tzinfo is not None and at.utcoffset() is not None,
            ),
        )

    async def recover(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None = None,
        _capture_deadline: float | None = None,
        _prior_captures: tuple[Capture, ...] = (),
        _prior_failure_reasons: tuple[str, ...] = (),
        _prepared_captures: tuple[Capture, ...] | None = None,
    ) -> StartupRecoveryResult:
        """Run startup reads and durable recovery without mutating broker state."""
        health = await self._broker.connect()
        account_id = self._settings.EXPECTED_ACCOUNT_ID or health.account_id
        local_snapshot = self._local_snapshot(account_id) if account_id else None
        own_deadline = monotonic() + 5
        deadline = (
            min(_capture_deadline, own_deadline) if _capture_deadline is not None else own_deadline
        )
        remaining = deadline - monotonic()
        if _prepared_captures is not None:
            capture = (
                _prepared_captures[0]
                if _prepared_captures
                else Capture("SKIPPED_AFTER_INGESTION_FAILURE", None, None, ())
            )
        else:
            capture = (
                await capture_account_positions(
                    self._broker,
                    clock=self._clock,
                    clock_healthy=lambda at: at.tzinfo is not None and at.utcoffset() is not None,
                    total_timeout_seconds=min(5, remaining),
                )
                if remaining > 0
                else Capture("ACQUISITION_TIMEOUT", None, None, ())
            )
        captures = (*_prior_captures, capture)
        candidate = capture.diagnostic_snapshot()
        if candidate is None and _prior_captures:
            # A rejected ingestion sample can still expose genuine differences.
            # It is diagnostic only: no failed capture can authorize readiness.
            candidate = _prior_captures[-1].diagnostic_snapshot()
        if capture.status != "ACQUIRED" or capture.snapshot is None:
            comparison = (
                compare_reconciliation(local_snapshot, candidate, self._policy)
                if (local_snapshot is not None and candidate is not None)
                else None
            )
            reason = "SNAPSHOT_" + capture.status
            return self._capture_refusal(
                reconciliation_id,
                started_at=started_at,
                run_id=run_id,
                comparison=snapshot_acquisition_failure(reason, comparison),
                health=health,
                captures=captures,
                reasons=(*_prior_failure_reasons, reason),
            )
        account_snapshot = capture.attempts[-1].after
        assert account_snapshot is not None
        resolved, unresolved = await self._recover_submissions()
        # Order acknowledgement recovery legitimately advances the order/fill ledger.
        # It must not replace the frozen expected account/position values.
        acknowledged = self._local_snapshot(account_id) if account_id else None
        if local_snapshot is not None and acknowledged is not None:
            local_snapshot = local_snapshot.model_copy(
                update={
                    "open_orders": acknowledged.open_orders,
                    "fills": acknowledged.fills,
                }
            )
        broker_snapshot = ReconciliationSnapshot(
            account=capture.snapshot.account,
            positions=capture.snapshot.positions,
            open_orders=tuple(await self._broker.get_open_orders()),
            fills=tuple(await self._broker.get_fills()),
        )
        # Freeze the original expected state across all acquisition retries.
        comparison = (
            missing_local_snapshot_result(account_snapshot.account)
            if local_snapshot is None
            else compare_reconciliation(local_snapshot, broker_snapshot, self._policy)
        )
        freshness = validate_capture_freshness(capture, now=self._clock(), clock_healthy=True)
        acquisition_reasons = (
            *_prior_failure_reasons,
            *(("SNAPSHOT_" + freshness,) if freshness else ()),
        )
        for reason in acquisition_reasons:
            comparison = snapshot_acquisition_failure(reason, comparison)
        completed_at = max(datetime.now(UTC), started_at)
        with self._database.transaction() as session:
            repository = StorageRepository(session)
            record_reconciliation(
                repository,
                reconciliation_id,
                comparison,
                started_at=started_at,
                completed_at=completed_at,
                run_id=run_id,
                snapshot_acquisition=[c.evidence() for c in captures],
            )
            durable_kill = repository.get_kill_switch_state().engaged

        effective_settings = self._settings.model_copy(
            update={
                "BROKER_HEALTHY": self._settings.BROKER_HEALTHY and health.healthy,
                "RECONCILIATION_SUCCESSFUL": comparison.allows_new_orders,
                "KILL_SWITCH": self._settings.KILL_SWITCH or durable_kill,
            }
        )
        safety = validate_trading_safety(
            effective_settings,
            account_snapshot.account.account_id,
        )
        reasons = [*safety.reasons, *acquisition_reasons]
        if not account_snapshot.order_entry_allowed:
            reasons.append("BROKER_ORDER_ENTRY_DISABLED")
        if account_snapshot.restrictions:
            reasons.append("BROKER_ACCOUNT_RESTRICTED")
        if unresolved:
            reasons.append("SUBMISSION_RECOVERY_REQUIRED")
        ordered_reasons = tuple(dict.fromkeys(reasons))
        return StartupRecoveryResult(
            ready=not ordered_reasons,
            reasons=ordered_reasons,
            reconciliation=comparison,
            resolved_intent_ids=resolved,
            unresolved_intent_ids=unresolved,
            broker_health=health,
        )

    def _capture_refusal(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None,
        comparison: ReconciliationResult,
        health: BrokerHealth,
        captures: tuple[Capture, ...],
        reasons: tuple[str, ...],
    ) -> StartupRecoveryResult:
        with self._database.transaction() as session:
            record_reconciliation(
                StorageRepository(session),
                reconciliation_id,
                comparison,
                started_at=started_at,
                completed_at=max(datetime.now(UTC), started_at),
                run_id=run_id,
                snapshot_acquisition=[c.evidence() for c in captures],
            )
        return StartupRecoveryResult(
            ready=False,
            reasons=tuple(dict.fromkeys((*reasons, "RECONCILIATION_REQUIRED"))),
            reconciliation=comparison,
            resolved_intent_ids=(),
            unresolved_intent_ids=tuple(
                sorted(
                    intent.intent_id
                    for intent in self._pending_intents()
                    if intent.state in {IntentState.SUBMITTING, IntentState.SUBMISSION_UNKNOWN}
                )
            ),
            broker_health=health,
        )
