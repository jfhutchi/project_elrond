"""Add the durable record of every autonomous proposal attempt.

Revision ID: 0028
Revises: 0027

Purely additive.  Nothing existing is read or rewritten.

The research producer opened tasks and recorded why on the opening task event, which answers "why
does this task exist" and nothing else.  An attempt that opened nothing -- no eligible question, a
model that answered outside its slate, a lost race -- left no durable trace at all, so the only
record that the system had looked and declined was a log line.  This table is that record, and it
is also what the experiment stage reads to find the pre-registered template a task was opened
for, instead of parsing the task id.

Two uniqueness rules carry the restart and concurrency guarantees in the database rather than in
a check a second process could race: one proposal per task, and one proposal per
(dataset, sequence), so two proposers that both saw a dataset as free cannot both open a question
against it.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    update,
)

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
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


def v28_tables() -> tuple[Table, ...]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Referenced by the foreign key only. Declared minimally so this migration does not depend on
    # the shape of a table it does not change.
    Table("research_tasks", metadata, Column("task_id", String(128), primary_key=True))
    proposals = Table(
        "research_proposals",
        metadata,
        Column("proposal_id", String(64), primary_key=True),
        Column("outcome", String(16), nullable=False),
        Column("task_id", String(128), ForeignKey("research_tasks.task_id")),
        Column("template_id", String(128)),
        Column("template_version", Integer),
        Column("catalogue_hash", String(64)),
        Column("dataset_identity", String(128)),
        Column("dataset_sequence", Integer),
        Column("proposer_version", Integer, nullable=False),
        Column("code_commit", String(64), nullable=False),
        Column("attempted_at", String(40), nullable=False),
        Column("evidence_json", Text, nullable=False),
        CheckConstraint(
            "outcome IN ('CREATED', 'NO_ELIGIBLE', 'REFUSED')", name="proposal_outcome_is_known"
        ),
        CheckConstraint(
            "(outcome = 'CREATED') = (task_id IS NOT NULL)",
            name="only_a_created_proposal_names_a_task",
        ),
        CheckConstraint(
            "(task_id IS NULL) = (dataset_sequence IS NULL)",
            name="a_dataset_sequence_belongs_to_a_task",
        ),
        CheckConstraint(
            "task_id IS NULL OR (template_id IS NOT NULL AND template_version IS NOT NULL "
            "AND catalogue_hash IS NOT NULL AND dataset_identity IS NOT NULL)",
            name="a_created_proposal_names_its_template_and_dataset",
        ),
        CheckConstraint(
            "dataset_sequence IS NULL OR dataset_sequence >= 1",
            name="dataset_sequence_is_positive",
        ),
        UniqueConstraint("task_id"),
        UniqueConstraint("dataset_identity", "dataset_sequence"),
    )
    Index("ix_research_proposals_attempted_at", proposals.c.attempted_at)
    return (proposals,)


def upgrade() -> None:
    connection = op.get_bind()
    for table in v28_tables():
        table.create(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=28))


def downgrade() -> None:
    connection = op.get_bind()
    schema = _schema_version()
    # The version write comes first, and the order is the fix rather than a style choice. pysqlite
    # opens a transaction only on DML, so a `DROP TABLE` issued first runs in autocommit and is
    # durable immediately. A chained downgrade through here to 0026 then reaches 0027's refusal,
    # the caller rolls back the version rows -- and the store is left claiming revision 28 with
    # this table gone. Writing the version first opens the transaction, so the drop joins it and
    # the rollback undoes both. Measured both ways on 2026-09-12 by fingerprinting the file after
    # a refused `downgrade 0026`: drop-first changed it and lost the table, version-first did not.
    connection.execute(update(schema).where(schema.c.id == 1).values(version=27))
    for table in reversed(v28_tables()):
        table.drop(bind=connection, checkfirst=True)
