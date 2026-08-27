"""Persist what Elrond refused, so the refusing can eventually be judged.

Revision ID: 0012
Revises: 0011

Rejections were recorded as free-text strings inside `signals.payload_json` -- no queryable gate,
no version, no parameters, no outcome. A risk governor can look prudent while quietly destroying
edge, or look overly conservative while preventing the losses that would have ended the account,
and nothing in this project could tell those apart (#57).

**Two tables, and the second is the point.** `rejected_candidates` is one row per refusal event;
`rejection_reasons` is one row per rule that refused it. A single entry can be refused by four
vetoes at once, and storing those as four events would inflate the sample fourfold -- the
manufacture of sample size that killed cycle 11's overnight result. The shape makes the honest
count available: reasons are counted from the child table, candidates from the parent.

The counterfactual outcome is deliberately **not** here beyond a state column. What was refused is
known at decision time; what would have happened is not known until the horizon has elapsed, and a
schema that invited both in one write would invite computing an outcome from data that does not
exist yet. Every row this revision can produce is `COUNTERFACTUAL_UNAVAILABLE`, with a reason.

`rule_parameters_hash` sits on the reason rather than the event because it is what makes an
analysis honest across a threshold change: outcomes recorded under different parameters are
different datasets, and grouping on the hash is what stops one rule being judged on evidence
gathered under another.

Purely additive. No existing row is read or rewritten.

The tables are declared literally rather than imported from `schema.py`, for the reason recorded
in `REFUTED.md` and enforced by `test_no_migration_imports_the_live_schema_module`.
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
    update,
)

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V12_TABLES = ("rejected_candidates", "rejection_reasons")


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v12_tables() -> MetaData:
    """The shadow tables exactly as this revision creates them."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    Table(
        "rejected_candidates",
        metadata,
        # Content-addressed on strategy, symbol, session and the refusing gates, so re-evaluating
        # a session cannot write a second observation of the same refusal.
        Column("observation_id", String(128), primary_key=True),
        # Which decision this was, independent of what any gate concluded (#57).
        Column("candidate_id", String(128), nullable=False),
        Column("strategy_id", String(128), nullable=False),
        Column("configuration_hash", String(128), nullable=False),
        Column("symbol", String(32), nullable=False),
        Column("direction", String(8), nullable=False),
        Column("observed_at", String(40), nullable=False),
        # Eastern, for the reason every other date in this project is.
        Column("session_date", String(10), nullable=False),
        Column("reference_price", Text, nullable=False),
        Column("hypothesis_id", String(128)),
        # Context this project cannot yet produce (#59, #56). Nullable so absence is recorded
        # rather than defaulted.
        Column("market_regime_snapshot_id", String(128)),
        Column("catalyst_snapshot_id", String(128)),
        Column("anomaly_event_id", String(128)),
        Column("portfolio_snapshot_id", String(128)),
        Column("state", String(32), nullable=False),
        Column("unavailable_reason", Text, nullable=False),
    )
    reasons = Table(
        "rejection_reasons",
        metadata,
        Column("observation_id", String(128), primary_key=True),
        Column("gate_id", String(64), primary_key=True),
        Column("gate_version", Integer, nullable=False),
        Column("rule_parameters_hash", String(128), nullable=False),
        Column("detail", Text, nullable=False),
        ForeignKeyConstraint(
            ["observation_id"],
            ["rejected_candidates.observation_id"],
            ondelete="CASCADE",
        ),
    )
    Index("ix_rejection_reasons_gate_id", reasons.c.gate_id, reasons.c.rule_parameters_hash)
    return metadata


def upgrade() -> None:
    """Create the shadow tables and bump the version marker."""
    connection = op.get_bind()
    metadata = v12_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V12_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=12))


def downgrade() -> None:
    """Drop them. Every record of what was refused goes too, which is why this is not routine."""
    connection = op.get_bind()
    metadata = v12_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V12_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=11))
