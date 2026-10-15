"""Additive v32 documents; frozen predecessor factories remain unchanged."""

from sqlalchemy import Column, MetaData, String, Table, Text


def health_tables(metadata: MetaData) -> tuple[Table, ...]:
    return tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in ("core_health_events", "core_time_evidence")
    )
