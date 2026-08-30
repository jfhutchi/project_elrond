"""Persist what a refusal cost, without ever amending the refusal.

Revision ID: 0017
Revises: 0016

0012 made refusals durable: which rule refused, under which parameters, on which evidence. This
is the other half -- what would have happened -- and the schema is where its restrictions stop
being conventions.

**Append-only by construction.** The outcome references `rejected_candidates` and never updates
it. The refusal is what the system knew at decision time, and that is precisely what #57 studies;
rewriting it once hindsight arrives would destroy the only record of it.

**One policy, one answer.** `(observation_id, policy_identity)` is unique. A hypothetical
execution policy -- entry reference, horizon, cost model, benchmark convention -- produces one
outcome per refusal. A second policy is a second dataset and gets its own row, because pooling
outcomes computed under different execution assumptions defeats the question this table exists to
answer.

**Non-evaluated states carry no number.** `net_excess_bps` and its components are nullable and
populated only on `COUNTERFACTUAL_EVALUATED`. A zero return is a measurement; `UNAVAILABLE` (not
yet) and `UNEVALUABLE` (never) are the absence of one, and a quotable figure beside either label
gets quoted. `unevaluable_reason` is what keeps never distinguishable from not yet.

**Nothing here can be mistaken for an order.** No quantity, no client order id, no side.

Purely additive. No existing row is read or rewritten.

The table is declared literally rather than imported from `schema.py`, for the reason recorded in
`REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import (
    Column,
    ForeignKeyConstraint,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    update,
)

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V17_TABLES = ("counterfactual_outcomes",)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v17_tables() -> MetaData:
    """The outcome table exactly as this revision creates it.

    `rejected_candidates` is declared as a primary-key-only stub so the foreign key can resolve.
    It is created by 0012 and is not in `V17_TABLES`, so nothing here creates or alters it --
    the stub owns nothing and exists only to make the reference resolvable in this `MetaData`.
    """
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "rejected_candidates",
        metadata,
        Column("observation_id", String(128), primary_key=True),
    )
    outcomes = Table(
        "counterfactual_outcomes",
        metadata,
        Column("outcome_id", String(128), primary_key=True),
        Column("observation_id", String(128), nullable=False),
        Column("policy_identity", String(128), nullable=False),
        Column("state", String(40), nullable=False),
        Column("horizon_session", String(10)),
        Column("evaluated_at", String(40)),
        Column("net_excess_bps", Text),
        Column("gross_return_bps", Text),
        Column("benchmark_return_bps", Text),
        Column("unevaluable_reason", String(40)),
        Column("detail", Text, nullable=False),
        ForeignKeyConstraint(
            ["observation_id"],
            ["rejected_candidates.observation_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "observation_id",
            "policy_identity",
            name="one_outcome_per_refusal_and_policy",
        ),
    )
    Index(
        "ix_counterfactual_outcomes_policy",
        outcomes.c.policy_identity,
        outcomes.c.state,
    )
    return metadata


def upgrade() -> None:
    """Create the outcome table and bump the version marker."""
    connection = op.get_bind()
    metadata = v17_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V17_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=17))


def downgrade() -> None:
    """Drop it. Every measured counterfactual goes too, so this is not routine."""
    connection = op.get_bind()
    metadata = v17_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V17_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=16))
