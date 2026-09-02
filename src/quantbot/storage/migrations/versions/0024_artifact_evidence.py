"""Add preregistered artifact tests and component evidence memory.

Revision ID: 0024
Revises: 0023

Purely additive.  Existing hypothesis and research-memory rows are not rewritten, so their
content hashes and historical meaning remain unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    update,
)

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _schema_version() -> Table:
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v24_tables() -> tuple[Table, Table]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    hypotheses = Table(
        "hypotheses",
        metadata,
        Column("hypothesis_id", String(128), primary_key=True),
        Column("version", Integer, primary_key=True),
    )
    research_records = Table(
        "research_records",
        metadata,
        Column("record_id", String(128), primary_key=True),
    )
    tests = Table(
        "artifact_test_registrations",
        metadata,
        Column("test_id", String(64), primary_key=True),
        Column("content_hash", String(64), nullable=False, unique=True),
        Column("hypothesis_id", String(128), nullable=False),
        Column("hypothesis_version", Integer, nullable=False),
        Column("registration_hash", String(64), nullable=False),
        Column("graph_hash", String(64), nullable=False),
        Column("artifact_kind", String(16), nullable=False),
        Column("artifact_content_id", String(64), nullable=False),
        Column("trial_charge", Integer, nullable=False),
        Column("power_verdict", String(32), nullable=False),
        Column("registered_at", String(40), nullable=False),
        Column("document_json", Text, nullable=False),
        ForeignKeyConstraint(
            ["hypothesis_id", "hypothesis_version"],
            [hypotheses.c.hypothesis_id, hypotheses.c.version],
        ),
        CheckConstraint("trial_charge = 1", name="artifact_test_charges_one_trial"),
    )
    Index(
        "ix_artifact_test_registrations_hypothesis",
        tests.c.hypothesis_id,
        tests.c.hypothesis_version,
    )
    Index(
        "ix_artifact_test_registrations_artifact",
        tests.c.artifact_kind,
        tests.c.artifact_content_id,
    )

    evidence = Table(
        "artifact_evidence",
        metadata,
        Column("evidence_id", String(64), primary_key=True),
        Column("source_record_id", String(128), nullable=False),
        Column("test_id", String(64)),
        Column("artifact_kind", String(16), nullable=False),
        Column("artifact_content_id", String(64), nullable=False),
        Column("epistemic_status", String(32), nullable=False),
        Column("verdict", String(32)),
        Column("hypothesis_id", String(128), nullable=False),
        Column("hypothesis_version", Integer, nullable=False),
        Column("manifest_hash", String(64), nullable=False),
        Column("recorded_at", String(40), nullable=False),
        Column("document_json", Text, nullable=False),
        ForeignKeyConstraint(
            ["hypothesis_id", "hypothesis_version"],
            [hypotheses.c.hypothesis_id, hypotheses.c.version],
        ),
        ForeignKeyConstraint(["source_record_id"], [research_records.c.record_id]),
        ForeignKeyConstraint(["test_id"], [tests.c.test_id]),
        CheckConstraint(
            "(epistemic_status = 'DIRECTLY_MEASURED') = (verdict IS NOT NULL)",
            name="direct_evidence_alone_has_verdict",
        ),
    )
    Index(
        "ix_artifact_evidence_artifact",
        evidence.c.artifact_kind,
        evidence.c.artifact_content_id,
        evidence.c.recorded_at,
    )
    Index("ix_artifact_evidence_manifest", evidence.c.manifest_hash)
    return tests, evidence


def upgrade() -> None:
    connection = op.get_bind()
    for table in v24_tables():
        # Match revisions 0021-0023: creation is idempotent, while the final metadata comparison
        # still fails closed if an existing table has the wrong shape.
        table.create(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=24))


def downgrade() -> None:
    connection = op.get_bind()
    for table in reversed(v24_tables()):
        table.drop(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=23))
