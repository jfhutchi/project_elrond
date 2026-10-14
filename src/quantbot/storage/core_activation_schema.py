"""Frozen v34 activation pointer, immutable commissioned envelopes and events."""

from sqlalchemy import CheckConstraint, Column, ForeignKey, Integer, MetaData, String, Table, Text


def activation_tables(metadata: MetaData) -> tuple[Table, ...]:
    documents = tuple(
        Table(
            name,
            metadata,
            Column("id", String(64), primary_key=True),
            Column("payload", Text, nullable=False),
        )
        for name in ("core_activation_authorities", "core_activations")
    )
    pointer = Table(
        "core_active_model",
        metadata,
        Column("id", Integer, primary_key=True),
        CheckConstraint("id = 1", name="singleton"),
        Column("activation_id", String(64), ForeignKey("core_activations.id"), nullable=False),
        Column("model_id", String(64), ForeignKey("core_models.id"), nullable=False),
        Column("generation", Integer, nullable=False),
        CheckConstraint("generation >= 1", name="generation_positive"),
        Column("receipt_id", String(64), ForeignKey("core_learning_runs.id")),
    )
    return (*documents, pointer)
