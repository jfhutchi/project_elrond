"""Add immutable supervised-learner artifacts, predictions, and outcomes.

Revision ID: 0029
Revises: 0028

Purely additive. A prediction and its later outcome are separate rows so observing a target can
never rewrite what the model said before that target was knowable.
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
    update,
)

revision: str = "0029"
down_revision: str | Sequence[str] | None = "0028"
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


def v29_tables() -> tuple[Table, ...]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    datasets = Table(
        "learner_datasets",
        metadata,
        Column("dataset_id", String(64), primary_key=True),
        Column("document_hash", String(64), nullable=False, unique=True),
        Column("document_json", Text, nullable=False),
    )
    models = Table(
        "learner_model_versions",
        metadata,
        Column("model_id", String(64), primary_key=True),
        Column(
            "dataset_id",
            String(64),
            ForeignKey(datasets.c.dataset_id, ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("family", String(64), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("artifact_hash", String(64), nullable=False),
        Column("document_hash", String(64), nullable=False, unique=True),
        Column("document_json", Text, nullable=False),
    )
    Index("ix_learner_model_versions_family_created", models.c.family, models.c.created_at)
    predictions = Table(
        "learner_predictions",
        metadata,
        Column("prediction_id", String(64), primary_key=True),
        Column(
            "model_id",
            String(64),
            ForeignKey(models.c.model_id, ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("evaluation_class", String(32), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("recorded_at", String(40), nullable=False),
        Column("cohort_id", String(128), nullable=False),
        Column("document_hash", String(64), nullable=False, unique=True),
        Column("document_json", Text, nullable=False),
        CheckConstraint(
            "evaluation_class IN ('HISTORICAL_REPLAY', 'SHADOW', 'PROSPECTIVE_LOCAL')",
            name="evaluation_class_is_known",
        ),
    )
    Index(
        "ix_learner_predictions_model_created",
        predictions.c.model_id,
        predictions.c.created_at,
    )
    Index("ix_learner_predictions_cohort", predictions.c.cohort_id, predictions.c.prediction_id)
    outcomes = Table(
        "learner_outcomes",
        metadata,
        Column(
            "prediction_id",
            String(64),
            ForeignKey(predictions.c.prediction_id, ondelete="RESTRICT"),
            primary_key=True,
        ),
        Column("resolved_at", String(40), nullable=False),
        Column("source_hash", String(64), nullable=False),
        Column("document_hash", String(64), nullable=False, unique=True),
        Column("document_json", Text, nullable=False),
    )
    Index("ix_learner_outcomes_resolved_at", outcomes.c.resolved_at)
    evaluations = Table(
        "learner_evaluations",
        metadata,
        Column("evaluation_id", String(64), primary_key=True),
        Column(
            "model_id",
            String(64),
            ForeignKey(models.c.model_id, ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("evaluation_class", String(32), nullable=False),
        Column("created_at", String(40), nullable=False),
        Column("document_hash", String(64), nullable=False, unique=True),
        Column("document_json", Text, nullable=False),
        CheckConstraint(
            "evaluation_class IN ('HISTORICAL_REPLAY', 'SHADOW', 'PROSPECTIVE_LOCAL')",
            name="evaluation_class_is_known",
        ),
    )
    Index(
        "ix_learner_evaluations_model_created",
        evaluations.c.model_id,
        evaluations.c.created_at,
    )
    return datasets, models, predictions, outcomes, evaluations


def upgrade() -> None:
    connection = op.get_bind()
    for table in v29_tables():
        table.create(bind=connection, checkfirst=True)
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=29))


def downgrade() -> None:
    connection = op.get_bind()
    schema = _schema_version()
    connection.execute(update(schema).where(schema.c.id == 1).values(version=28))
    for table in reversed(v29_tables()):
        table.drop(bind=connection, checkfirst=True)
