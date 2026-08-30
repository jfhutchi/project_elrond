"""Was there a public reason for this anomaly, and did anyone manage to look (#56).

Revision ID: 0020
Revises: 0019

`catalyst.py` produced `CatalystAssessment` with an `as_row()` written for a table that did not
exist, and nothing ever called it. This is that table, and the wiring that fills it.

The column that carries the design is `outcome`, and two of its three values look alike from a
distance. `NO_PUBLIC_CATALYST_FOUND` is a **finding**: every source answered and none had
anything, which for unusual activity is the interesting case. `CATALYST_UNKNOWN` means somebody
could not look. Storing an outage as the former would turn every provider incident into a
discovery -- and provider incidents cluster when markets are busy, which is when anomalies fire,
so the manufactured discoveries would arrive exactly where they are most tempting to believe.

`unhealthy_sources` is stored beside `sources_consulted` rather than derived from it, because the
denominator of a negative finding is the thing a later reader most needs and least able to
reconstruct: which sources were asked, and which of them actually answered.

`published_after_event` is counted rather than dropped. A non-zero count beside
`NO_PUBLIC_CATALYST_FOUND` says the world did explain the move, just not before it -- which is a
different fact from silence, and the one that would otherwise read as look-ahead if an
enrichment ever slipped.

One assessment per event per lookback window: re-running the same lookup re-derives the same
content-addressed id and stores once, while a different window is a different question and gets
its own row.

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
    UniqueConstraint,
    update,
)

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V20_TABLES = ("catalyst_assessments",)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v20_tables() -> MetaData:
    """The catalyst table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    # Primary key only: the events table already exists and this revision does not redefine it.
    # Declared so the foreign key below resolves without importing the live schema.
    Table(
        "market_anomaly_events",
        metadata,
        Column("event_id", String(128), primary_key=True),
    )
    assessments = Table(
        "catalyst_assessments",
        metadata,
        Column("assessment_id", String(128), primary_key=True),
        Column("anomaly_event_id", String(128), nullable=False),
        Column("observed_at", String(40), nullable=False),
        Column("lookback_seconds", Integer, nullable=False),
        # CATALYST_FOUND, NO_PUBLIC_CATALYST_FOUND (a finding), CATALYST_UNKNOWN (nobody looked).
        Column("outcome", String(32), nullable=False),
        # The denominator of a negative finding: who was asked, and who actually answered.
        Column("sources_consulted", Text, nullable=False),
        Column("unhealthy_sources", Text, nullable=False),
        Column("catalyst_ids", Text, nullable=False),
        # Counted, never dropped: the world explained the move, just not before it.
        Column("published_after_event", Integer, nullable=False),
        Column("unplaceable", Integer, nullable=False),
        Column("detail", Text, nullable=False),
        ForeignKeyConstraint(
            ["anomaly_event_id"],
            ["market_anomaly_events.event_id"],
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "anomaly_event_id",
            "lookback_seconds",
            name="one_assessment_per_event_and_window",
        ),
        CheckConstraint("lookback_seconds > 0", name="a_window_has_width"),
        CheckConstraint("published_after_event >= 0", name="counts_are_not_negative"),
        CheckConstraint("unplaceable >= 0", name="unplaceable_is_not_negative"),
        # The confusion this whole design exists to prevent, enforced by the database rather
        # than only by the model that writes to it.
        CheckConstraint(
            "NOT (outcome = 'NO_PUBLIC_CATALYST_FOUND' AND unhealthy_sources <> '')",
            name="an_outage_is_not_a_negative_finding",
        ),
        CheckConstraint(
            "NOT (outcome = 'NO_PUBLIC_CATALYST_FOUND' AND unplaceable > 0)",
            name="an_unplaceable_item_is_not_silence",
        ),
        CheckConstraint(
            "(outcome = 'CATALYST_FOUND') = (catalyst_ids <> '')",
            name="a_positive_finding_names_what_it_found",
        ),
    )
    Index("ix_catalyst_assessments_event", assessments.c.anomaly_event_id)
    Index("ix_catalyst_assessments_outcome", assessments.c.outcome)
    return metadata


def upgrade() -> None:
    """Create the table and bump the version marker."""
    connection = op.get_bind()
    metadata = v20_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V20_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=20))


def downgrade() -> None:
    """Drop it. Every recorded catalyst lookup and its source health goes too."""
    connection = op.get_bind()
    metadata = v20_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V20_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=19))
