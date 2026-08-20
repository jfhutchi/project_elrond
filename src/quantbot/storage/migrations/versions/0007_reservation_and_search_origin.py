"""Separate reserving a protected window from spending it, and pin where a trial count came from.

Revision ID: 0007
Revises: 0006

Two findings, one migration, because both reshape `hypotheses` / `hypothesis_data_windows`.

#22: a `PROTECTED_EVALUATION` window was written at registration and blocked every later overlap
forever, whether or not the experiment ever ran. Registering and then abandoning a hypothesis
burned a clean holdout silently. Windows now carry `state` plus an expiry.

#23: `search_cardinality` was a bare self-report added straight into the multiple-testing burden,
so a miner that evaluated 5,000 candidates could declare 1. `search_origin` records where the
number came from, and an unattested agent claim is not an accepted origin.

The backfill is deliberately literal about what the old code did rather than generous:

* windows become `CONSUMED`, dated from their hypothesis's own `registered_at`, because consuming
  at registration is exactly what the old implementation did. Marking them `RESERVED` would
  retroactively unblock holdouts nobody can prove were untouched.
* `search_origin` becomes `LEGACY_SELF_REPORTED`, a value new registrations may not use. Calling
  those rows attested or measured would launder the precise claim this revision exists to stop.

The tables are declared literally rather than imported from `schema.py`, for the reason recorded
in `REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
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
    text,
    update,
)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _v6_shapes() -> tuple[Table, Table]:
    """`hypotheses` and `hypothesis_data_windows` as they stood before this revision."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
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
        Column("estimand", String(32), nullable=False),
        Column("expected_effect", Text, nullable=False),
        Column("power_verdict", String(32), nullable=False),
        Column("required_observations", Integer, nullable=False),
        Column("available_observations", Integer, nullable=False),
        Column("document_json", Text, nullable=False),
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
    windows = Table(
        "hypothesis_data_windows",
        metadata,
        Column("hypothesis_id", String(128), primary_key=True),
        Column("version", Integer, primary_key=True),
        Column("dataset", String(128), primary_key=True),
        Column("role", String(32), primary_key=True),
        Column("start_date", String(10), nullable=False),
        Column("end_date", String(10), nullable=False),
        ForeignKeyConstraint(
            ["hypothesis_id", "version"],
            ["hypotheses.hypothesis_id", "hypotheses.version"],
            ondelete="CASCADE",
        ),
        CheckConstraint("end_date >= start_date", name="range_ordered"),
    )
    Index(
        "ix_hypothesis_data_windows_dataset",
        windows.c.dataset,
        windows.c.start_date,
    )
    return hypotheses, windows


def upgrade() -> None:
    """Add the reservation state and the search origin, backfilling honestly."""
    connection = op.get_bind()

    # SQLite cannot alter a column in place, so `batch_alter_table` rebuilds each table. The
    # server_default exists only to make the backfill possible and is stripped in the second
    # pass, because head does not declare one and a migrated database has to match head exactly.
    # This is the same two-pass shape 0003 used, for the same reason.
    hypotheses, windows = _v6_shapes()
    with op.batch_alter_table("hypotheses", copy_from=hypotheses) as batch:
        batch.add_column(
            Column(
                "search_origin",
                String(32),
                nullable=False,
                server_default=text("'LEGACY_SELF_REPORTED'"),
            )
        )
        batch.add_column(Column("search_attested_by", String(128)))
    with op.batch_alter_table("hypothesis_data_windows", copy_from=windows) as batch:
        batch.add_column(
            Column("state", String(16), nullable=False, server_default=text("'CONSUMED'"))
        )
        batch.add_column(Column("reserved_until", String(10)))
        batch.add_column(Column("consumed_at", String(40)))

    # Consuming at registration is precisely what the old implementation did, so this records
    # the old behaviour rather than inventing a time.
    connection.execute(
        text(
            "UPDATE hypothesis_data_windows SET consumed_at = ("
            "  SELECT h.registered_at FROM hypotheses h"
            "  WHERE h.hypothesis_id = hypothesis_data_windows.hypothesis_id"
            "    AND h.version = hypothesis_data_windows.version"
            ") WHERE consumed_at IS NULL"
        )
    )

    # Pass two: the same columns declared as head declares them -- no server_default, and
    # NOT NULL where head says NOT NULL -- plus the constraints. `copy_from` describes the shape
    # to rebuild into, so omitting the default here is what removes it.
    hypotheses, windows = _v6_shapes()
    hypotheses.append_column(Column("search_origin", String(32), nullable=False))
    hypotheses.append_column(Column("search_attested_by", String(128)))
    windows.append_column(Column("state", String(16), nullable=False))
    windows.append_column(Column("reserved_until", String(10)))
    windows.append_column(Column("consumed_at", String(40)))

    with op.batch_alter_table("hypotheses", copy_from=hypotheses) as batch:
        batch.create_check_constraint(
            "attestation_is_signed",
            "(search_origin = 'OPERATOR_ATTESTED') = (search_attested_by IS NOT NULL)",
        )
        batch.create_check_constraint(
            "theory_searches_nothing",
            "search_origin != 'NO_DATA_DEPENDENT_SEARCH' OR search_cardinality = 1",
        )
    with op.batch_alter_table("hypothesis_data_windows", copy_from=windows) as batch:
        batch.create_check_constraint(
            "consumption_is_dated", "(state = 'CONSUMED') = (consumed_at IS NOT NULL)"
        )

    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=7))


def downgrade() -> None:
    """Drop the reservation state and the search origin, restoring the V6 shape."""
    connection = op.get_bind()
    hypotheses, windows = _v6_shapes()
    hypotheses.append_column(Column("search_origin", String(32), nullable=False))
    hypotheses.append_column(Column("search_attested_by", String(128)))
    windows.append_column(Column("state", String(16), nullable=False))
    windows.append_column(Column("reserved_until", String(10)))
    windows.append_column(Column("consumed_at", String(40)))

    with op.batch_alter_table("hypotheses", copy_from=hypotheses) as batch:
        batch.drop_column("search_origin")
        batch.drop_column("search_attested_by")
    with op.batch_alter_table("hypothesis_data_windows", copy_from=windows) as batch:
        batch.drop_column("state")
        batch.drop_column("reserved_until")
        batch.drop_column("consumed_at")

    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=6))
