"""Frozen v33 shadow learning documents and permanent session reservations."""

from sqlalchemy import Column, ForeignKey, MetaData, String, Table, Text


def learning_tables(metadata: MetaData) -> tuple[Table, ...]:
    documents = tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in ("core_models", "core_learning_runs", "core_learning_inputs")
    )
    attempts = Table(
        "core_learning_attempts",
        metadata,
        Column("session", String(10), primary_key=True),
        Column("run_id", String(64), nullable=False, unique=True),
        Column("search_id", String(128), ForeignKey("search_runs.search_id"), nullable=False),
        Column("input_id", String(64), nullable=False),
        Column("input_revision_digest", String(64), nullable=False, unique=True),
        Column("reservation_payload", Text, nullable=False),
    )
    identities = Table(
        "core_learning_considerations",
        metadata,
        Column("run_id", String(64), primary_key=True),
        Column("receipt_id", String(64), ForeignKey("core_learning_runs.id"), nullable=False),
    )
    return (*documents, attempts, identities)
