"""Generalise the registry beyond Sharpe and record power decisions.

Revision ID: 0003
Revises: 0002

Two changes. `power_assessments` is additive. The `hypotheses` reshaping is not: a single
`expected_sharpe` column is wrong for four of the five estimands #19 introduces, and a column
holding an information coefficient under that name is exactly the misleading instrumentation
this project keeps getting caught by.

SQLite cannot alter columns in place, so the reshaping runs through `batch_alter_table`, which
rebuilds. It runs twice on purpose. The first pass renames and adds the two new columns with a
`server_default` so existing rows are backfilled; the second strips that default, because a
default left behind is a real difference from head metadata and the store's post-upgrade check
rejects the database on the next open. It was caught that way rather than by reading the code.

Each pass names the shape it starts from -- V2 from revision 0002's own declaration, V3 from
this revision -- for the same reason 0001 and 0002 name their own tables: a migration that
reads live metadata stops describing what it actually did as soon as the next revision lands.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from types import ModuleType

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


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported.

    A migration that reaches into `quantbot.storage.schema` stops describing the schema it
    actually operated on the moment a later revision reshapes that table.
    """
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _power_assessments() -> Table:
    """The assessment log exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    table = Table(
        "power_assessments",
        metadata,
        Column("assessment_id", Integer, primary_key=True, autoincrement=True),
        Column("hypothesis_id", String(128), nullable=False),
        Column("version", Integer, nullable=False),
        # REGISTRATION or EXECUTION. The bar moves between them, so both are kept.
        Column("stage", String(16), nullable=False),
        Column("assessed_at", String(40), nullable=False),
        Column("verdict", String(32), nullable=False),
        Column("estimand", String(32), nullable=False),
        Column("cumulative_trials", Integer, nullable=False),
        Column("luck_threshold_z", Text, nullable=False),
        Column("observations_required", Integer, nullable=False),
        Column("observations_available", Integer, nullable=False),
        Column("minimum_detectable_effect", Text, nullable=False),
        # Present only on an operator override, which is the audit trail for one.
        Column("overridden_by", String(128)),
        Column("document_json", Text, nullable=False),
    )
    Index("ix_power_assessments_hypothesis", table.c.hypothesis_id, table.c.version)
    Index("ix_power_assessments_verdict", table.c.verdict)
    return table


revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The values every V2 row must have had: the registry only accepted annualised Sharpe, and
#: only stored registrations that had already cleared the gate.
V2_ESTIMAND = "SHARPE"
V2_VERDICT = "POWERED"


def _revision_0002() -> ModuleType:
    return import_module("quantbot.storage.migrations.versions.0002_hypothesis_registry")


def _v2_hypotheses() -> Table:
    """The `hypotheses` table exactly as revision 0002 left it, taken from 0002 itself."""
    _metadata, hypotheses, _windows = _revision_0002().v2_tables()
    assert isinstance(hypotheses, Table)
    return hypotheses


def _v3_hypotheses(*, backfill_defaults: bool) -> Table:
    """This revision's `hypotheses`, optionally still carrying the backfill defaults."""
    metadata = MetaData(naming_convention=_revision_0002().NAMING_CONVENTION)
    hypotheses = Table(
        "hypotheses",
        metadata,
        Column("hypothesis_id", String(128), primary_key=True),
        Column("version", Integer, primary_key=True),
        Column("family_id", String(128), nullable=False),
        Column("parent_hypothesis_id", String(128)),
        Column("parent_version", Integer),
        Column("registered_at", String(40), nullable=False),
        Column("content_hash", String(64), nullable=False, unique=True),
        Column("search_cardinality", Integer, nullable=False),
        Column("cumulative_trials", Integer, nullable=False),
        Column("luck_threshold_z", Text, nullable=False),
        Column("expected_effect", Text, nullable=False),
        Column("required_observations", Integer, nullable=False),
        Column("available_observations", Integer, nullable=False),
        Column("document_json", Text, nullable=False),
        Column(
            "estimand",
            String(32),
            nullable=False,
            server_default=V2_ESTIMAND if backfill_defaults else None,
        ),
        Column(
            "power_verdict",
            String(32),
            nullable=False,
            server_default=V2_VERDICT if backfill_defaults else None,
        ),
        ForeignKeyConstraint(
            ["parent_hypothesis_id", "parent_version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
        ),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint(
            "(parent_hypothesis_id IS NULL) = (parent_version IS NULL)",
            name="parent_is_whole",
        ),
    )
    Index("ix_hypotheses_family_id", hypotheses.c.family_id)
    return hypotheses


def upgrade() -> None:
    """Widen the registry to any estimand, and add the power-assessment record."""
    connection = op.get_bind()
    _power_assessments().create(bind=connection)

    with op.batch_alter_table("hypotheses", copy_from=_v2_hypotheses()) as batch:
        batch.alter_column("expected_sharpe", new_column_name="expected_effect")
        batch.alter_column("required_sessions", new_column_name="required_observations")
        batch.alter_column("available_sessions", new_column_name="available_observations")
        batch.add_column(Column("estimand", String(32), nullable=False, server_default=V2_ESTIMAND))
        batch.add_column(
            Column("power_verdict", String(32), nullable=False, server_default=V2_VERDICT)
        )

    with op.batch_alter_table(
        "hypotheses", copy_from=_v3_hypotheses(backfill_defaults=True)
    ) as batch:
        batch.alter_column("estimand", existing_type=String(32), server_default=None)
        batch.alter_column("power_verdict", existing_type=String(32), server_default=None)

    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=3))


def downgrade() -> None:
    """Return the registry to its Sharpe-only shape."""
    connection = op.get_bind()
    with op.batch_alter_table(
        "hypotheses", copy_from=_v3_hypotheses(backfill_defaults=False)
    ) as batch:
        batch.drop_column("power_verdict")
        batch.drop_column("estimand")
        batch.alter_column("expected_effect", new_column_name="expected_sharpe")
        batch.alter_column("required_observations", new_column_name="required_sessions")
        batch.alter_column("available_observations", new_column_name="available_sessions")
    _power_assessments().drop(bind=connection)
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=2))
