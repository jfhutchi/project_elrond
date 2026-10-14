"""Preserve historical numeric costs; add truthful provenance and outcome revisions."""

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, Text, text

from quantbot.research.manifest import content_id
from quantbot.storage.core_cost_schema import cost_tables

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Rebuild only to permit actual NULL; leave every historical numeric byte intact.
    with op.batch_alter_table("fills") as batch:
        batch.alter_column("fee", existing_type=Text(), nullable=True)
        batch.add_column(Column("fee_source", String(32), nullable=False, server_default="UNKNOWN"))
        batch.add_column(Column("fee_reference", Text()))
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(table_name)s_%(column_0_name)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )
    Table("account_snapshots", metadata, Column("snapshot_id", String(128), primary_key=True))
    Table("core_reconciliation_proofs", metadata, Column("id", String(64), primary_key=True))
    for table in cost_tables(metadata):
        table.create(op.get_bind(), checkfirst=True)
    # Preserve the immutable v30 outcome before its current pointer can advance.
    # IDs are derived from the original canonical JSON, not expanded new models.
    connection = op.get_bind()
    for lot_id, payload in connection.execute(text("SELECT id,payload FROM core_round_trips")):
        trip = json.loads(payload)
        outcome_id = content_id(trip)
        connection.execute(
            text("INSERT INTO core_outcome_revisions (id,payload) VALUES (:id,:payload)"),
            {"id": outcome_id, "payload": payload},
        )
        decision = json.loads(
            connection.execute(
                text("SELECT payload FROM core_decisions WHERE id=:id"), {"id": trip["decision_id"]}
            ).scalar_one()
        )
        observation = json.loads(
            connection.execute(
                text("SELECT payload FROM core_observations WHERE id=:id"),
                {"id": decision["observation_id"]},
            ).scalar_one()
        )
        connection.execute(
            text(
                "INSERT INTO core_outcome_index (lot_id,outcome_id,available_at,source_start,"
                "source_end,"
                "dataset,policy_id,evidence_lane,status) VALUES "
                "(:lot,:outcome,:at,:start,:end,:dataset,:policy,:lane,:status)"
            ),
            {
                "lot": lot_id,
                "outcome": outcome_id,
                "at": trip["recorded_at_utc"],
                "start": observation["source_start"],
                "end": trip["recorded_at_utc"],
                "dataset": "EXPLICIT_SYNTHETIC",
                "policy": decision["policy_id"],
                "lane": trip["evidence_lane"],
                "status": "COSTS_PENDING",
            },
        )
    # A v30 proof with fills treated unknown fees as zero. Retain it for audit,
    # revoke its full cash authority; quantity ownership is re-proven after restart.
    op.get_bind().execute(
        text(
            "INSERT INTO core_cash_authority_revocations (snapshot_id, reason) "
            "SELECT snapshot_id, 'V30_UNPROVEN_FEE_CASH' FROM core_inventory_certifications "
            "WHERE EXISTS (SELECT 1 FROM fills)"
        )
    )
    op.get_bind().execute(text("UPDATE schema_version SET version=31 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v31 is evidence-preserving; use compatible code or repair forward")
