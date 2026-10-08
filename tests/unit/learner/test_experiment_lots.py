"""Synthetic owned-lot economics with actual Decimal and SQLite journaling."""

from datetime import timedelta
from decimal import Decimal
from importlib import import_module

import pytest

from quantbot.learner.experiment_store import ExperimentStore
from tests.fixtures.adaptive_paper import OPEN, manifest


def lots():
    return import_module("quantbot.learner.experiment_lots")


def event(kind, **changes):
    return lots().LotEvent(
        **(
            {
                "experiment_id": manifest().experiment_id,
                "lot_id": "a" * 64,
                "decision_id": "b" * 64,
                "symbol": "SYN0",
                "account_id_hash": "c" * 64,
                "recorded_at": OPEN + timedelta(seconds=20),
                "occurred_at": OPEN,
                "kind": kind,
                "order_id": "entry",
                "quantity": Decimal(0),
                "source_hash": "d" * 64,
                "synthetic": True,
            }
            | changes
        )
    )


def prepare(
    tmp_path,
    *,
    exit_quantity=Decimal(1),
    exit_fee=Decimal(".20"),
    exit_owner="PROTECTION",
    exit_time=None,
    closed=True,
    exit_link_time=None,
):
    module = lots()
    store = ExperimentStore(tmp_path / "lots.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    events = [
        event("ENTRY_DECLARED", position_before_entry=0),
        event("ORDER_LINK", quantity=1, side="BUY", owner="ENTRY"),
        event(
            "FILL",
            quantity=1,
            price=100,
            fee=Decimal(".10"),
            side="BUY",
            fill_id="entry-fill",
            owner="ENTRY",
        ),
        event("PROTECTION_BOUND", quantity=1, occurred_at=OPEN + timedelta(seconds=1)),
        event("ORDER_TERMINAL", quantity=1, terminal=True),
        event(
            "ORDER_LINK",
            order_id="stop",
            side="SELL",
            owner=exit_owner,
            quantity=1,
            occurred_at=exit_link_time or OPEN,
        ),
        event(
            "FILL",
            quantity=exit_quantity,
            price=102,
            fee=exit_fee,
            side="SELL",
            order_id="stop",
            fill_id="exit-fill",
            owner=exit_owner,
            occurred_at=exit_time or OPEN + timedelta(seconds=5),
        ),
        event(
            "ORDER_TERMINAL",
            order_id="stop",
            quantity=exit_quantity,
            terminal=True,
            occurred_at=exit_time or OPEN + timedelta(seconds=5),
        ),
    ]
    if closed:
        events.append(
            event("RECONCILED_FLAT", occurred_at=OPEN + timedelta(seconds=10), reconciled=True)
        )
    for row in events:
        module.record_lot_event(store, row)
    return store


def test_protection_owner_exit_completes_owned_lot(tmp_path):
    module, store = lots(), prepare(tmp_path)
    outcome = module.resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.status == "COMPLETE"
    assert outcome.net_pnl == Decimal("1.70")
    assert outcome.fees == Decimal(".30")
    assert outcome.synthetic
    assert outcome.protection_verified
    assert outcome.entry_fill_ids == ("entry-fill",)
    assert outcome.exit_fill_ids == ("exit-fill",)


def test_partial_exit_residual_is_incomplete(tmp_path):
    store = prepare(tmp_path, exit_quantity=Decimal(".4"))
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.status == "INCOMPLETE"
    assert outcome.net_pnl is None
    assert outcome.residual_quantity == Decimal(".6")


def test_missing_fee_is_not_zero(tmp_path):
    store = prepare(tmp_path, exit_fee=None)
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None
    assert "FEE_UNAVAILABLE" in outcome.reasons


def test_legacy_inventory_never_adopted(tmp_path):
    with pytest.raises(ValueError):
        event("ENTRY_DECLARED", position_before_entry=1)


def test_equal_time_ambiguity_not_guessed(tmp_path):
    store = prepare(tmp_path, exit_time=OPEN)
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None
    assert "FILL_ORDERING_UNVERIFIED" in outcome.reasons


def test_restart_retains_exit_owner(tmp_path):
    store = prepare(tmp_path)
    before = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    reopened = ExperimentStore(store.path)
    after = lots().resolve_lot(reopened, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert before == after


def test_partial_entry_protects_only_actual_quantity(tmp_path):
    module = lots()
    store = ExperimentStore(tmp_path / "partial.db", create=True)
    store.append(manifest(), idempotency_key="manifest")
    for row in (
        event("ENTRY_DECLARED", position_before_entry=0),
        event("ORDER_LINK", quantity=1, side="BUY", owner="ENTRY"),
        event(
            "FILL",
            quantity=Decimal(".4"),
            price=100,
            fee=0,
            side="BUY",
            fill_id="partial",
            owner="ENTRY",
        ),
        event("PROTECTION_BOUND", quantity=Decimal(".4"), occurred_at=OPEN + timedelta(seconds=1)),
    ):
        module.record_lot_event(store, row)
    outcome = module.resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.residual_quantity == Decimal(".4")
    assert outcome.protected_quantity == Decimal(".4")
    assert outcome.net_pnl is None


def test_unowned_exit_and_future_flat_receipt_cannot_finalize(tmp_path):
    store = prepare(tmp_path, exit_owner="UNKNOWN", closed=False)
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None
    assert "EXIT_OWNERSHIP_UNVERIFIED" in outcome.reasons
    lots().record_lot_event(
        store,
        event(
            "RECONCILED_FLAT",
            reconciled=True,
            recorded_at=OPEN + timedelta(minutes=1),
            occurred_at=OPEN + timedelta(seconds=40),
        ),
    )
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert "FLAT_RECONCILIATION_UNAVAILABLE" in outcome.reasons


def test_duplicate_fill_cannot_double_pnl(tmp_path):
    store = prepare(tmp_path)
    duplicate = event(
        "FILL",
        quantity=1,
        price=100,
        fee=Decimal(".10"),
        side="BUY",
        fill_id="entry-fill",
        owner="ENTRY",
    )
    assert lots().record_lot_event(store, duplicate) is False
    assert lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30)).net_pnl == Decimal(
        "1.70"
    )


def test_later_receipt_of_same_fill_preserves_first_availability(tmp_path):
    store = prepare(tmp_path)
    repeated = event(
        "FILL",
        quantity=1,
        price=100,
        fee=Decimal(".10"),
        side="BUY",
        fill_id="entry-fill",
        owner="ENTRY",
        recorded_at=OPEN + timedelta(seconds=40),
    )
    assert not lots().record_lot_event(store, repeated)
    entries = [
        row.decode(lots().LotEvent)
        for row in store.read_as_of(manifest().experiment_id, OPEN + timedelta(minutes=1))
        if row.kind == "LotEvent" and row.decode(lots().LotEvent).fill_id == "entry-fill"
    ]
    assert entries[0].recorded_at == OPEN + timedelta(seconds=20)


def test_terminal_receipt_before_exit_fill_is_not_complete(tmp_path):
    store = prepare(tmp_path)
    # A second incompatible terminal receipt cannot establish terminal consistency.
    lots().record_lot_event(
        store, event("ORDER_TERMINAL", order_id="stop", quantity=1, terminal=True, occurred_at=OPEN)
    )
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None
    assert "TERMINAL_FILL_RECONCILIATION_UNAVAILABLE" in outcome.reasons


def test_synthetic_event_cannot_be_relabeled_real(tmp_path):
    store = prepare(tmp_path)
    with pytest.raises(ValueError, match="MANIFEST_LOT_IDENTITY_MISMATCH"):
        lots().record_lot_event(store, event("PROTECTION_BOUND", quantity=1, synthetic=False))


def test_protection_only_bound_after_exit_is_unverified(tmp_path):
    store = prepare(tmp_path)
    # Distinct conflicting binding quantities must not be reduced by maximum alone.
    lots().record_lot_event(
        store, event("PROTECTION_BOUND", quantity=2, occurred_at=OPEN + timedelta(seconds=6))
    )
    outcome = lots().resolve_lot(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert outcome.net_pnl is None
    assert "PROTECTION_UNVERIFIED" in outcome.reasons


def test_outcome_revision_is_durable_and_unchanged_tick_deduplicates(tmp_path):
    store = prepare(tmp_path)
    assert lots().journal_lot_outcome(store, "a" * 64, now=OPEN + timedelta(seconds=30))
    assert not lots().journal_lot_outcome(
        ExperimentStore(store.path), "a" * 64, now=OPEN + timedelta(seconds=40)
    )
    revisions = [
        row.decode(lots().LotOutcomeRevision)
        for row in store.read_as_of(manifest().experiment_id, OPEN + timedelta(seconds=50))
        if row.kind == "LotOutcomeRevision"
    ]
    assert len(revisions) == 1
    assert revisions[0].outcome.net_pnl == Decimal("1.70")
    assert revisions[0].recorded_at == OPEN + timedelta(seconds=30)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        "missing",
        "duplicate",
        "wrong_hash",
        "wrong_quantity",
        "wrong_order",
        "late",
        "protection",
        "future",
    ],
)
def test_cross_clock_exit_requires_exact_unique_deadline_broker_evidence(tmp_path, invalid):
    from pydantic import ValidationError

    from quantbot.research.manifest import content_id

    module = lots()
    local = OPEN + timedelta(seconds=5, milliseconds=117)
    store = prepare(
        tmp_path,
        exit_owner="PROTECTION" if invalid == "protection" else "DEADLINE",
        exit_link_time=local,
    )
    link = next(
        row.decode(module.LotEvent)
        for row in store.read_as_of(manifest().experiment_id, OPEN + timedelta(seconds=30))
        if row.kind == "LotEvent"
        and row.decode(module.LotEvent).kind == "ORDER_LINK"
        and row.decode(module.LotEvent).side == "SELL"
    )
    values = dict(
        experiment_id=manifest().experiment_id,
        lot_id=link.lot_id,
        recorded_at=OPEN + timedelta(seconds=20),
        order_id=link.order_id,
        quantity=1,
        ownership_hash=content_id(link),
        broker_order_id="broker-exit",
        broker_order_hash="e" * 64,
        submitted_at=OPEN + timedelta(seconds=4, milliseconds=996),
    )
    if invalid == "wrong_hash":
        values["ownership_hash"] = "f" * 64
    if invalid == "wrong_quantity":
        values["quantity"] = Decimal(".9")
    if invalid == "wrong_order":
        values["order_id"] = "foreign-exit"
    if invalid == "late":
        values["submitted_at"] = OPEN + timedelta(seconds=5, milliseconds=1)
    if invalid == "future":
        values["submitted_at"] = OPEN + timedelta(seconds=21)
        with pytest.raises(ValidationError):
            module.DeadlineBrokerClockEvidence(**values)
    elif invalid != "missing":
        anchor = module.DeadlineBrokerClockEvidence(**values)
        store.append(anchor, idempotency_key="anchor")
        if invalid == "duplicate":
            store.append(
                anchor.model_copy(update={"broker_order_hash": "f" * 64}),
                idempotency_key="conflicting-anchor",
            )
    outcome = module.resolve_lot(store, link.lot_id, now=OPEN + timedelta(seconds=30))
    if invalid is None:
        assert outcome.status == "COMPLETE"
        assert outcome.net_pnl == Decimal("1.70")
    else:
        assert outcome.net_pnl is None
        assert "EXIT_OWNERSHIP_UNVERIFIED" in outcome.reasons
