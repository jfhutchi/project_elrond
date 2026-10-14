"""Frozen v35 additive runtime evidence schema."""

from sqlalchemy import Column, ForeignKey, MetaData, String, Table, Text


def runtime_tables(metadata: MetaData) -> tuple[Table, ...]:
    documents = tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in (
            "core_runtime_commissions",
            "core_runtime_events",
            "core_role_heartbeats",
            "core_provider_acquisitions",
            "core_external_owner_events",
            "core_runtime_config_history",
            "core_canonical_sources",
        )
    )
    current = Table(
        "core_role_current",
        metadata,
        Column("role", String(16), primary_key=True),
        Column("heartbeat_id", String(64), ForeignKey("core_role_heartbeats.id"), nullable=False),
    )
    progress = Table(
        "core_provider_progress",
        metadata,
        Column("account_id", String(128), primary_key=True),
        Column("watermark", String(40), nullable=False),
        Column(
            "document_id", String(64), ForeignKey("core_provider_acquisitions.id"), nullable=False
        ),
    )
    canonical_current = Table(
        "core_canonical_current",
        metadata,
        Column("id", String(16), primary_key=True),
        Column("source_id", String(64), ForeignKey("core_canonical_sources.id"), nullable=False),
    )
    return (*documents, current, progress, canonical_current)
