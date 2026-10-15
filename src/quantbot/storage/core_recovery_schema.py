"""Additive v36 work and current-authority projections; immutable evidence stays intact."""

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table


def recovery_tables(metadata: MetaData) -> tuple[Table, ...]:
    work = Table(
        "core_outcome_work",
        metadata,
        Column("lot_id", String(64), primary_key=True),
        Column("version", Integer, nullable=False),
        Column("processed_version", Integer, nullable=False),
        Column("attempted_at", String(40), nullable=False),
    )
    memberships = Table(
        "core_guardian_memberships",
        metadata,
        Column("plan_id", String(64), primary_key=True),
        Column(
            "receipt_id",
            String(64),
            ForeignKey("core_guardian_receipts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    current = Table(
        "core_guardian_current",
        metadata,
        Column("id", String(16), primary_key=True),
        Column(
            "receipt_id",
            String(64),
            ForeignKey("core_guardian_receipts.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    executable = Table(
        "core_runtime_current",
        metadata,
        Column("id", String(16), primary_key=True),
        Column(
            "document_id", String(64), ForeignKey("core_runtime_config_history.id"), nullable=False
        ),
    )
    documents = Table(
        "core_document_index",
        metadata,
        Column("kind", String(64), primary_key=True),
        Column("id", String(64), primary_key=True),
        Column("reference", String(128), nullable=False),
        Column("available_at", String(40), nullable=False),
        Index("ix_core_document_reference", "kind", "reference", "available_at"),
    )
    proof_fills = Table(
        "core_proof_fill_memberships",
        metadata,
        Column(
            "proof_id",
            String(64),
            ForeignKey("core_reconciliation_proofs.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        Column("fill_id", String(128), primary_key=True),
        Index("ix_core_proof_fill_reference", "fill_id", "proof_id"),
    )
    return work, memberships, current, executable, documents, proof_fills
