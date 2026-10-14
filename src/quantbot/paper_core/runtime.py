"""Fixture initialization only. Operational launcher and model activation are later milestones."""

from datetime import datetime, timedelta
from functools import partial
from typing import cast

from sqlalchemy import select

from quantbot.brokers import BrokerAdapter
from quantbot.config import Settings
from quantbot.execution import ExecutionCoordinator
from quantbot.execution.coordinator import SubmissionPolicy
from quantbot.paper_core.contracts import GuardianReceipt, ReconciliationProof
from quantbot.paper_core.send_guard import final_send_in_session
from quantbot.paper_core.simulation import SyntheticBroker
from quantbot.paper_core.store import CoreStore, certify_inventory, put, table
from quantbot.research.manifest import content_id
from quantbot.storage import StorageRepository


def require_synthetic(store: CoreStore, broker: SyntheticBroker) -> None:
    if not isinstance(broker, SyntheticBroker) or broker.account_id != store.config.account_id:
        raise ValueError("M1 permits only an explicit matching synthetic broker")


def initialize_fixture(
    store: CoreStore, broker: SyntheticBroker, *, now: datetime, entry_enabled: bool = False
) -> ReconciliationProof:
    require_synthetic(store, broker)
    baseline = {
        "account": broker.snapshot().account.model_dump(mode="json"),
        "quantities": tuple(sorted((s, q) for s, q in broker.quantities.items() if q != 0)),
        "positions": broker.position_snapshot(),
        "source": "EXPLICIT_SYNTHETIC_INITIAL_STATE",
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
            owner=store.config.owner,
            generation=store.config.generation,
            valid_until=now + timedelta(seconds=120),
        ),
    )
    return proof


def coordinator(store: CoreStore, broker: SyntheticBroker) -> ExecutionCoordinator:
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
