"""Create the isolated immutable shadow-forecast evidence store.

Revision ID: 0001
Revises: None

The schema deliberately contains no strategy, signal, order, risk, reconciliation,
kill-switch, or qualification tables. Historical shapes are declared literally.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _v1_shapes() -> tuple[Table, Table, Table]:
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    marker = Table(
        "forecast_schema_version",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
        CheckConstraint("id = 1", name="singleton"),
    )
    forecasts = Table(
        "model_forecasts",
        metadata,
        Column("forecast_id", String(64), primary_key=True),
        Column("evidence_role", String(16), nullable=False),
        Column("forecast_provider", String(64), nullable=False),
        Column("model_id", String(256), nullable=False),
        Column("model_revision", String(128), nullable=False),
        Column("tokenizer_id", String(256), nullable=False),
        Column("tokenizer_revision", String(128), nullable=False),
        Column("kronos_code_revision", String(128), nullable=False),
        Column("integration_version", String(64), nullable=False),
        Column("symbol", String(32), nullable=False),
        Column("as_of", String(40), nullable=False),
        Column("latest_source_bar_at", String(40)),
        Column("source_data_hash", String(64), nullable=False),
        Column("source_provider", String(128), nullable=False),
        Column("source_vintage_id", String(256), nullable=False),
        Column("source_available_at", String(40), nullable=False),
        Column("source_adjustment_metadata_json", Text, nullable=False),
        Column("source_bar_count", Integer, nullable=False),
        Column("lookback", Integer, nullable=False),
        Column("horizon", Integer, nullable=False),
        Column("attempt_number", Integer, nullable=False),
        Column("status", String(32), nullable=False),
        Column("generated_at", String(40), nullable=False),
        Column("request_json", Text, nullable=False),
        Column("result_json", Text),
        Column("error_code", String(64)),
        CheckConstraint("evidence_role = 'SHADOW'", name="shadow_evidence_only"),
        CheckConstraint("source_bar_count >= 0", name="source_bar_count_nonnegative"),
        CheckConstraint("lookback >= 1", name="lookback_positive"),
        CheckConstraint("horizon >= 1", name="horizon_positive"),
        CheckConstraint("attempt_number >= 0", name="attempt_number_nonnegative"),
        CheckConstraint(
            "status IN ('SUCCESS', 'FAILED', 'SKIPPED', 'INSUFFICIENT_DATA')",
            name="status_valid",
        ),
        CheckConstraint(
            "(status = 'SUCCESS' AND result_json IS NOT NULL AND error_code IS NULL) OR "
            "(status != 'SUCCESS' AND result_json IS NULL AND error_code IS NOT NULL)",
            name="status_payload_consistent",
        ),
    )
    Index(
        "ix_model_forecasts_symbol_as_of",
        forecasts.c.symbol,
        forecasts.c.as_of,
    )
    Index(
        "ix_model_forecasts_status",
        forecasts.c.status,
        forecasts.c.generated_at,
    )

    evaluations = Table(
        "model_forecast_evaluations",
        metadata,
        Column("evaluation_id", String(64), primary_key=True),
        Column(
            "forecast_id",
            String(64),
            ForeignKey("model_forecasts.forecast_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        Column("evidence_role", String(16), nullable=False),
        Column("scoring_version", String(64), nullable=False),
        Column("evaluated_at", String(40), nullable=False),
        Column("realized_through", String(40), nullable=False),
        Column("outcome_provider", String(128), nullable=False),
        Column("outcome_vintage_id", String(256), nullable=False),
        Column("outcome_available_at", String(40), nullable=False),
        Column("outcome_adjustment_metadata_json", Text, nullable=False),
        Column("outcome_source_hash", String(64), nullable=False),
        Column("outcome_json", Text, nullable=False),
        Column("predicted_terminal_return", Text, nullable=False),
        Column("realized_terminal_return", Text, nullable=False),
        Column("directionally_correct", Boolean, nullable=False),
        Column("absolute_error", Text, nullable=False),
        Column("squared_error", Text, nullable=False),
        Column("baselines_json", Text, nullable=False),
        CheckConstraint("evidence_role = 'SHADOW'", name="shadow_evidence_only"),
    )
    Index(
        "ix_model_forecast_evaluations_forecast_id",
        evaluations.c.forecast_id,
        evaluations.c.evaluated_at,
    )
    return marker, forecasts, evaluations


def upgrade() -> None:
    connection = op.get_bind()
    marker, forecasts, evaluations = _v1_shapes()
    marker.create(connection)
    forecasts.create(connection)
    evaluations.create(connection)
    connection.execute(marker.insert().values(id=1, version=1))


def downgrade() -> None:
    connection = op.get_bind()
    marker, forecasts, evaluations = _v1_shapes()
    evaluations.drop(connection)
    forecasts.drop(connection)
    marker.drop(connection)
