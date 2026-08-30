"""Track one economic instrument across the names providers give it.

Revision ID: 0018
Revises: 0017

Every record in this project keys on a ticker string. That silently splits one instrument's
history in two when it renames, and merges two histories into one when a symbol is recycled --
and both failures are invisible, because the resulting data looks perfectly well formed.

`instrument_id` is minted once from the facts true at first sight and never recomputed. It is
deliberately **not** content-addressed over the row, unlike almost everything else here: an
identity hashed over its attributes changes whenever an attribute changes, which is precisely
what a durable identity must not do. Attributes move around the id.

`instrument_symbols` is historical rather than current. A mapping carries the window it was valid
over, so an event recorded in 2015 under an old ticker stays interpretable after a 2022 rename,
and a recycled ticker cannot silently match an earlier instrument. That is the property the layer
exists to provide, and it only works if old windows are kept rather than rewritten.

A resolver is a source, never the key. OpenFIGI, SEC EDGAR or any future vendor supplies evidence
about an instrument; none of them defines one. So `status` distinguishes `RESOLVED`, `AMBIGUOUS`
(a resolver returned several candidates and choosing one would invent a fact) and `UNRESOLVED`
(nobody looked, or nobody could answer). An instrument nobody could resolve is still recordable,
which is the case that matters -- a resolver outage must not stop a decision being attributed.

`sec_cik_source` is separate from `sec_cik` because a CIK asserted by a market-data vendor and one
read from EDGAR are different claims, and #83 asks that the authoritative one stay attributable.

Purely additive. No existing row is read or rewritten, and nothing yet keys on these tables --
migrating existing ticker-keyed records is a separate, reviewable step.

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

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

V18_TABLES = ("instruments", "instrument_symbols")


def _schema_version() -> Table:
    """The version marker as this revision sees it, declared rather than imported."""
    return Table(
        "schema_version",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("version", Integer, nullable=False),
    )


def v18_tables() -> MetaData:
    """The instrument tables exactly as this revision creates them."""
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    instruments = Table(
        "instruments",
        metadata,
        Column("instrument_id", String(64), primary_key=True),
        Column("asset_class", String(16), nullable=False),
        Column("current_symbol", String(32), nullable=False),
        Column("status", String(16), nullable=False),
        Column("figi", String(32)),
        Column("composite_figi", String(32)),
        Column("share_class_figi", String(32)),
        Column("sec_cik", String(16)),
        Column("sec_cik_source", String(64)),
        Column("exchange_mic", String(8)),
        Column("ambiguous_candidates", Text, nullable=False),
        Column("resolver", String(64)),
        Column("resolver_version", String(32)),
        Column("resolved_at", String(40)),
        Column("source_refs", Text, nullable=False),
    )
    Index("ix_instruments_current_symbol", instruments.c.current_symbol)

    symbols = Table(
        "instrument_symbols",
        metadata,
        Column("instrument_id", String(64), primary_key=True),
        Column("provider", String(64), primary_key=True),
        Column("symbol", String(32), primary_key=True),
        Column("valid_from", String(10), primary_key=True),
        Column("valid_to", String(10)),
        ForeignKeyConstraint(
            ["instrument_id"], ["instruments.instrument_id"], ondelete="CASCADE"
        ),
    )
    Index(
        "ix_instrument_symbols_lookup",
        symbols.c.provider,
        symbols.c.symbol,
        symbols.c.valid_from,
    )
    return metadata


def upgrade() -> None:
    """Create the instrument tables and bump the version marker."""
    connection = op.get_bind()
    metadata = v18_tables()
    metadata.create_all(bind=connection, tables=[metadata.tables[name] for name in V18_TABLES])
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=18))


def downgrade() -> None:
    """Drop them. Every recorded identity and mapping history goes too."""
    connection = op.get_bind()
    metadata = v18_tables()
    metadata.drop_all(
        bind=connection, tables=[metadata.tables[name] for name in reversed(V18_TABLES)]
    )
    marker = _schema_version()
    connection.execute(update(marker).where(marker.c.id == 1).values(version=17))
