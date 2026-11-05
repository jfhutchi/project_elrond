"""Frozen v37 additive evidence and uniqueness for the five-minute portfolio policy."""

from sqlalchemy import Column, ForeignKey, Index, MetaData, String, Table, Text


def throughput_tables(metadata: MetaData) -> tuple[Table, ...]:
    documents = tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in (
            "core_session_openings",
            "core_batches",
            "core_entry_reservations",
            "core_holding_deadlines",
        )
    )
    opening = Table(
        "core_session_opening_index",
        metadata,
        Column("account_id", String(128), primary_key=True),
        Column("policy_id", String(128), primary_key=True),
        Column("session_date", String(10), primary_key=True),
        Column("store_identity", String(36), nullable=False),
        Column(
            "document_id",
            String(64),
            ForeignKey("core_session_openings.id"),
            nullable=False,
            unique=True,
        ),
    )
    members = Table(
        "core_batch_members",
        metadata,
        Column("batch_id", String(64), ForeignKey("core_batches.id"), primary_key=True),
        Column("symbol", String(32), primary_key=True),
        Column("decision_id", String(64), nullable=False, unique=True),
        Column(
            "reservation_id",
            String(64),
            ForeignKey("core_entry_reservations.id"),
            nullable=False,
            unique=True,
        ),
        Column("session_date", String(10), nullable=False),
        Index("ix_core_batch_session", "session_date", "decision_id"),
    )
    deadlines = Table(
        "core_holding_deadline_index",
        metadata,
        Column("lot_id", String(64), primary_key=True),
        Column(
            "document_id",
            String(64),
            ForeignKey("core_holding_deadlines.id"),
            nullable=False,
            unique=True,
        ),
    )
    return *documents, opening, members, deadlines
