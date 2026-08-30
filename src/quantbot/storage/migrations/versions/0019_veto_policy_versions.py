"""Keep every version of the veto policy, so old refusals stay attributable.

Revision ID: 0019
Revises: 0018

`PortfolioVetoPolicy.identity` is written into every durable refusal. Editing a threshold in
place would therefore convert every historical refusal into a claim about a rule that no longer
exists -- the refusal keeps citing an identity nothing can resolve. So a change is an append: a
new row supersedes the old one, and the old row keeps its body and its identity forever.

Two constraints here do work that the application layer cannot do alone:

`version` is unique, so two rows cannot claim the same ordinal. Ordering the history is the whole
point of storing it, and an ambiguous ordinal makes "which rule was in force" unanswerable.

`supersedes` is unique, which is the stronger one. `apply_proposal` refuses a proposal composed
against a stale head, but that check lives in one process. Two processes -- a console request and
a scheduled apply -- can both read version 3 and both write a successor to it, and neither would
see the other. The result is a forked history where two rules each claim to follow the same
predecessor, and no reader can tell which one was in force. In-process optimistic concurrency
cannot see that race; a unique index can, and the second writer fails instead of forking.

`body` is the full policy, not a diff. A version is reconstructed by reading one row rather than
replaying every change that produced it, so a corrupted or missing intermediate row cannot make
later versions unreadable. `changes` is the human-facing diff and is derived, not authoritative.

Purely additive. Nothing reads these rows yet -- the runtime still constructs its policy from
settings, and moving it to read the stored head is a separate, reviewable step.

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

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V19_TABLES = ("veto_policy_versions",)


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v19_tables() -> MetaData:
    """The policy-version table exactly as this revision creates it."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    versions = Table(
        "veto_policy_versions",
        metadata,
        Column("identity", String(128), primary_key=True),
        Column("version", Integer, nullable=False),
        # NULL only for version 1, the shipped default. Unique so the chain cannot fork.
        Column("supersedes", String(128)),
        # OPERATOR or PROJECT_DEFAULT. Never written from a proposal (#72).
        Column("authority", String(32), nullable=False),
        Column("applied_by", String(128), nullable=False),
        Column("applied_at", String(40), nullable=False),
        Column("reason", Text, nullable=False),
        # The whole policy, so a version is read rather than replayed.
        Column("body", Text, nullable=False),
        # The derived diff, for a reader. Authority lives in `body`.
        Column("changes", Text, nullable=False),
        ForeignKeyConstraint(
            ["supersedes"], ["veto_policy_versions.identity"], ondelete="RESTRICT"
        ),
        UniqueConstraint("version", name="one_row_per_version_ordinal"),
        UniqueConstraint("supersedes", name="one_successor_per_version"),
        CheckConstraint("version >= 1", name="versions_start_at_one"),
        CheckConstraint(
            "(version = 1) = (supersedes IS NULL)", name="only_the_first_has_no_predecessor"
        ),
    )
    Index("ix_veto_policy_versions_applied_at", versions.c.applied_at)
    return metadata


def upgrade() -> None:
    """Create the table and bump the version marker."""
    connection = op.get_bind()
    metadata = v19_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V19_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=19))


def downgrade() -> None:
    """Drop it. Every recorded policy version and the provenance of each change goes too."""
    connection = op.get_bind()
    metadata = v19_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V19_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=18))
