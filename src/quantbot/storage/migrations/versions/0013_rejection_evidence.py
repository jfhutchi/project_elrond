"""Persist what each refusal rested on, not only which rule refused.

Revision ID: 0013
Revises: 0012

#0012 made refusals durable with `gate_id`, `gate_version`, `rule_parameters_hash` and prose. The
parameter hash answers *which rule was in force*. It does not answer *what the rule was looking
at*, and #74 and #81 then added exactly that to the live decision -- expected against observed
session, market state, the dependence snapshot a cluster came from -- where it reached the ledger
as English and nothing else (#85).

Two refusals sharing a `rule_parameters_hash` need not be comparable. The same freshness policy
refuses a missing Tuesday and a missing Thursday. The same correlation threshold refuses under a
matrix measured in March and one measured in August. Pooling those because the hashes match is
the same error as pooling across a threshold change, one level down.

**Four columns, not one per field.** A column per structured field would mean the next veto with
evidence silently loses it until somebody remembers a migration. A kind, a schema version, a
canonical payload and its hash carry any veto's context, and the field set per kind is validated
in `quantbot.risk.evidence` where it can fail loudly at the boundary.

**Nullable, and no backfill.** Rows written before this revision genuinely had no structured
evidence, and inventing some from today's calendar or today's bars would answer a different
question: what the rule *would* refuse now, not what it *did* refuse then. A NULL here reads back
as `LEGACY_UNRECORDED`, which live code is forbidden from writing -- so the marker cannot become
a thing a current row claims about itself.

Purely additive. No existing row is read or rewritten.

The table is declared literally rather than imported from `schema.py`, for the reason recorded in
`REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, Integer, MetaData, Table, update

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Added to `rejection_reasons`, in creation order.
V13_COLUMNS: tuple[tuple[str, sa.types.TypeEngine[str] | sa.types.TypeEngine[int]], ...] = (
    ("evidence_kind", sa.String(32)),
    ("evidence_schema_version", sa.Integer()),
    ("evidence_payload", sa.Text()),
    ("evidence_identity", sa.String(128)),
)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def upgrade() -> None:
    """Add the evidence columns and bump the version marker.

    Every column is nullable. Making them NOT NULL would require a value for rows that predate
    the concept, and the only values available are invented ones.
    """
    for name, kind in V13_COLUMNS:
        op.add_column("rejection_reasons", sa.Column(name, kind, nullable=True))
    # Grouping a counterfactual study by decision basis is the query this exists to serve, so it
    # gets an index rather than a table scan per gate.
    op.create_index(
        "ix_rejection_reasons_evidence_identity",
        "rejection_reasons",
        ["evidence_identity"],
    )
    connection = op.get_bind()
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=13))


def downgrade() -> None:
    """Drop them. Every recorded decision basis goes with them, which is why this is not routine."""
    op.drop_index("ix_rejection_reasons_evidence_identity", table_name="rejection_reasons")
    for name, _ in reversed(V13_COLUMNS):
        op.drop_column("rejection_reasons", name)
    connection = op.get_bind()
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=12))
