"""Bind the claim, the mandate and the assessment to the promotion record.

Revision ID: 0011
Revises: 0010

`promotion_state` recorded where a strategy was and said nothing about *what it was testing*.
The claim behind a deployment lived only as a `hypothesis_id` argument passed into
`assess_forward_evidence` at qualification time, which meant a caller could offer a convenient
unrefuted registration for a strategy whose real mechanism had been refuted -- and the gate would
faithfully report that it found no refutation of the thing it had been handed (#53).

`PAPER_QUALIFIED` had the matching gap in the other direction. The stage asserted that forward
evidence supported the strategy, and nothing durable recorded which assessment, under which
economic objective, had said so. A stage is not provenance (#47).

Four nullable columns on each table, and nullable is correct rather than lax: a strategy below
`REGISTERED` has no claim, and one below `PAPER_QUALIFIED` has no qualifying assessment.
`PromotionState` refuses the combinations that would be dishonest -- an economic stage that
cannot name all three -- which is a rule about meaning and belongs in the model, not a NOT NULL
that would make the legitimate states unrepresentable.

They are copied onto `promotion_events` as well as `promotion_state` because the state is a
current value and the event is the record. An audit asking what justified `PAPER_QUALIFIED` on
the day it was granted must not have to hope the current row still says the same thing.

Purely additive. Existing rows read back with four nulls, which is exactly right: nothing on the
ladder today earned its stage under this binding, and inventing values for them would be
manufacturing the provenance this revision exists to require.

The tables are declared literally rather than imported from `schema.py`, for the reason recorded
in `REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Integer, MetaData, String, Table, update

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Added to both ladder tables, in this order.
V11_COLUMNS = (
    ("hypothesis_id", String(128)),
    ("hypothesis_version", Integer),
    ("objective_identity", String(128)),
    ("evidence_identity", String(64)),
)

V11_TABLES = ("promotion_state", "promotion_events")


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def upgrade() -> None:
    """Add the binding columns and bump the version marker."""
    for table in V11_TABLES:
        for name, kind in V11_COLUMNS:
            op.add_column(table, Column(name, kind))
    connection = op.get_bind()
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=11))


def downgrade() -> None:
    """Drop the binding columns. What a stage was justified by goes with them."""
    for table in V11_TABLES:
        for name, _ in reversed(V11_COLUMNS):
            op.drop_column(table, name)
    connection = op.get_bind()
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=10))
