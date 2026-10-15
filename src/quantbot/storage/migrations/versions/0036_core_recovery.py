"""Bound outcome catch-up and separate executable authority from economic history."""

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, text

from quantbot.storage.core_recovery_schema import recovery_tables

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata = MetaData(
        naming_convention={
            "pk": "pk_%(table_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        }
    )
    for name in (
        "core_guardian_receipts",
        "core_runtime_config_history",
        "core_reconciliation_proofs",
    ):
        Table(name, metadata, Column("id", String(64), primary_key=True))
    for target in recovery_tables(metadata):
        target.create(op.get_bind(), checkfirst=True)
    op.execute(
        text(
            "INSERT INTO core_outcome_work(lot_id,version,processed_version,attempted_at) "
            "SELECT DISTINCT lot_id,1,0,'' FROM core_intent_links WHERE role='ENTRY'"
        )
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            text(
                "INSERT INTO core_proof_fill_memberships "
                "SELECT DISTINCT p.id,j.value FROM core_reconciliation_proofs p,"
                "json_each(p.payload,'$.fill_ids') j"
            )
        )
        op.execute(
            text(
                "INSERT INTO core_document_index "
                "SELECT 'core_cash_activity_sources',s.id,a.id,"
                "json_extract(s.payload,'$.recorded_at_utc') "
                "FROM core_broker_event_sources s JOIN core_cash_activities a "
                "ON json_extract(s.payload,'$.cash_activity')=a.payload"
            )
        )
        op.execute(
            text(
                "INSERT INTO core_guardian_memberships SELECT j.value,r.id "
                "FROM core_guardian_receipts r,json_each(r.payload,'$.protection_ids') j"
            )
        )
        op.execute(
            text(
                "INSERT INTO core_guardian_current SELECT 'current',id "
                "FROM core_guardian_receipts "
                "ORDER BY json_extract(payload,'$.recorded_at_utc') DESC,id DESC LIMIT 1"
            )
        )
        for name, field in (
            ("core_broker_event_sources", "client_order_id"),
            ("core_outcome_revisions", "lot_id"),
            ("core_cost_items", "fill_id"),
            ("core_reconciliation_proofs", "account.account_id"),
            ("core_round_trips", "reconciliation_id"),
        ):
            op.execute(
                text(
                    f"INSERT INTO core_document_index SELECT '{name}',id,"
                    f"json_extract(payload,'$.{field}'),"
                    f"json_extract(payload,'$.recorded_at_utc') FROM {name}"
                )
            )
    op.execute(text("UPDATE schema_version SET version=36 WHERE id=1"))


def downgrade() -> None:
    raise RuntimeError("v36 is evidence-preserving; repair forward")
