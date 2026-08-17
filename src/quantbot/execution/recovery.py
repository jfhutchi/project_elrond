"""Fail-closed startup reconstruction and broker reconciliation."""

from __future__ import annotations

from datetime import UTC, datetime

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
    ) -> None:
        self._database = database
        self._broker = broker
        self._settings = settings
        self._policy = reconciliation_policy or ReconciliationPolicy()

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

    async def recover(
        self,
        reconciliation_id: str,
        *,
        started_at: datetime,
        run_id: str | None = None,
    ) -> StartupRecoveryResult:
        """Run startup reads and durable recovery without mutating broker state."""
        health = await self._broker.connect()
        account_snapshot = await self._broker.get_account()
        resolved, unresolved = await self._recover_submissions()
        broker_snapshot = ReconciliationSnapshot(
            account=account_snapshot.account,
            positions=tuple(await self._broker.get_positions()),
            open_orders=tuple(await self._broker.get_open_orders()),
            fills=tuple(await self._broker.get_fills()),
        )
        local_account_id = self._settings.EXPECTED_ACCOUNT_ID or account_snapshot.account.account_id
        local_snapshot = self._local_snapshot(local_account_id)
        comparison = (
            missing_local_snapshot_result(account_snapshot.account)
            if local_snapshot is None
            else compare_reconciliation(local_snapshot, broker_snapshot, self._policy)
        )
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
        reasons = list(safety.reasons)
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
