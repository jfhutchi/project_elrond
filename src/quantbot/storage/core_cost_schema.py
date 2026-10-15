"""Frozen v31 cost/source/quantity authority extensions to v30."""

from sqlalchemy import Column, ForeignKey, Integer, MetaData, String, Table, Text


def cost_tables(metadata: MetaData) -> tuple[Table, ...]:
    documents = tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in (
            "core_broker_event_sources",
            "core_cost_items",
            "core_outcome_revisions",
            "core_cash_activities",
        )
    )
    cursors = Table(
        "core_source_cursors",
        metadata,
        Column("source_id", String(64), primary_key=True),
        Column("available_at", String(40), nullable=False),
        Column("event_id", String(64), nullable=False),
    )
    quantity = Table(
        "core_quantity_certifications",
        metadata,
        Column("generation", Integer, primary_key=True),
        Column(
            "proof_id",
            String(64),
            ForeignKey("core_reconciliation_proofs.id"),
            unique=True,
            nullable=False,
        ),
        Column(
            "snapshot_id", String(128), ForeignKey("account_snapshots.snapshot_id"), nullable=False
        ),
    )
    revoked = Table(
        "core_cash_authority_revocations",
        metadata,
        Column(
            "snapshot_id",
            String(128),
            ForeignKey("account_snapshots.snapshot_id"),
            primary_key=True,
        ),
        Column("reason", Text, nullable=False),
    )
    current = Table(
        "core_outcome_index",
        metadata,
        Column("lot_id", String(64), primary_key=True),
        Column("outcome_id", String(64), ForeignKey("core_outcome_revisions.id"), nullable=False),
        Column("available_at", String(40), nullable=False),
        Column("source_start", String(40), nullable=False),
        Column("source_end", String(40), nullable=False),
        Column("dataset", String(64), nullable=False),
        Column("policy_id", String(128), nullable=False),
        Column("evidence_lane", String(32), nullable=False),
        Column("status", String(32), nullable=False),
    )
    return (*documents, cursors, quantity, revoked, current)
