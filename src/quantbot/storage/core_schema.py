"""Frozen additive v30 tables; ordinary opens never perform this migration."""

from sqlalchemy import Column, ForeignKey, Index, Integer, MetaData, String, Table, Text


def core_tables(metadata: MetaData) -> tuple[Table, ...]:
    def document(name: str) -> Table:
        return Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )

    tables = tuple(
        document(name)
        for name in (
            "core_identities",
            "core_observations",
            "core_decisions",
            "core_protection_plans",
            "core_reconciliation_proofs",
            "core_guardian_receipts",
            "core_cycle_receipts",
            "core_round_trips",
            "core_fixture_baselines",
        )
    )
    links = Table(
        "core_intent_links",
        metadata,
        Column("intent_id", String(128), ForeignKey("order_intents.intent_id"), primary_key=True),
        Column("decision_id", String(64), ForeignKey("core_decisions.id"), nullable=False),
        Column("protection_id", String(64), ForeignKey("core_protection_plans.id"), nullable=False),
        Column("observation_id", String(64), ForeignKey("core_observations.id"), nullable=False),
        Column("role", String(8), nullable=False),
        Column("lot_id", String(64), nullable=False),
        Column("payload", Text, nullable=False),
    )
    Index(
        "uq_core_entry_observation",
        links.c.observation_id,
        unique=True,
        sqlite_where=links.c.role == "ENTRY",
    )
    certs = Table(
        "core_inventory_certifications",
        metadata,
        Column("generation", Integer, primary_key=True),
        Column(
            "proof_id",
            String(64),
            ForeignKey("core_reconciliation_proofs.id"),
            nullable=False,
            unique=True,
        ),
        Column(
            "snapshot_id", String(128), ForeignKey("account_snapshots.snapshot_id"), nullable=False
        ),
    )
    raw = Table(
        "raw_account_snapshot_sources",
        metadata,
        Column(
            "snapshot_id",
            String(128),
            ForeignKey("account_snapshots.snapshot_id"),
            primary_key=True,
        ),
        Column("source", String(64), nullable=False),
    )
    return (*tables, links, certs, raw)
