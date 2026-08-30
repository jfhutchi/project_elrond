"""One economic instrument, across the names providers give it (#83 part A).

Two failure modes motivate the whole layer, and both are invisible in the resulting data:

* a rename **splits** one instrument's history into two, so evidence about it is silently halved;
* a recycled ticker **merges** two instruments' histories, so evidence about one is attributed to
  another.

Everything below exists to make those expressible failures rather than silent ones. The worked
example is FB to META, because it is real, it is recent, and `FB` was subsequently reusable.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantbot.market_data.instrument import (
    AssetClass,
    InstrumentIdentity,
    ResolutionStatus,
    SymbolMapping,
    mint_instrument_id,
    rename,
    resolve_symbol,
    unresolved_instrument,
)
from quantbot.storage import Database, StorageRepository

LISTED = date(2012, 5, 18)
RENAMED = date(2022, 6, 9)


def meta() -> InstrumentIdentity:
    """The FB to META rename, as the layer records it."""
    return rename(
        unresolved_instrument(
            symbol="FB", asset_class=AssetClass.EQUITY, first_seen=LISTED, provider="alpaca"
        ),
        provider="alpaca",
        new_symbol="META",
        effective=RENAMED,
    )


# --- identity survives the label ------------------------------------------------------------------


def test_a_rename_does_not_change_the_instrument() -> None:
    """The entire purpose. An identity that moved with its ticker would not be one."""
    before = unresolved_instrument(
        symbol="FB", asset_class=AssetClass.EQUITY, first_seen=LISTED, provider="alpaca"
    )
    after = rename(before, provider="alpaca", new_symbol="META", effective=RENAMED)

    assert after.instrument_id == before.instrument_id
    assert after.current_symbol == "META"


def test_the_id_is_not_content_addressed_over_attributes_that_change() -> None:
    """Deliberately unlike everything else here, and worth stating as a test.

    A content-addressed identity is right when the object is a measurement. It is wrong when the
    object is a *thing* whose description changes: hashing the description would mint a new
    identity every time the world moved, which is the opposite of durable.
    """
    first = mint_instrument_id(
        asset_class=AssetClass.EQUITY, first_symbol="FB", first_seen=LISTED
    )
    again = mint_instrument_id(
        asset_class=AssetClass.EQUITY, first_symbol="fb", first_seen=LISTED
    )

    assert first == again, "case must not mint two identities for one instrument"
    assert first != mint_instrument_id(
        asset_class=AssetClass.EQUITY, first_symbol="META", first_seen=LISTED
    ), "a different founding symbol is a different instrument"


def test_an_old_event_stays_interpretable_after_the_rename() -> None:
    """Rewriting history would destroy the record of what the system actually saw."""
    identity = meta()

    assert identity.symbol_at("alpaca", date(2015, 1, 2)) == "FB"
    assert identity.symbol_at("alpaca", date(2023, 1, 3)) == "META"
    assert identity.symbol_at("alpaca", date(2010, 1, 4)) is None, "before it existed"


def test_a_recycled_ticker_does_not_match_the_instrument_that_gave_it_up() -> None:
    """The merge failure, made impossible rather than unlikely.

    `FB` after 2022 belongs to whatever next uses it. A lookup that ignored the date would
    silently attribute that instrument's evidence to Meta.
    """
    identity = meta()

    assert resolve_symbol([identity], provider="alpaca", symbol="FB", moment=date(2015, 1, 2))
    assert (
        resolve_symbol([identity], provider="alpaca", symbol="FB", moment=date(2023, 1, 3))
        is None
    )


def test_a_rename_cannot_produce_overlapping_windows() -> None:
    """Two windows covering one date would give `symbol_at` two answers."""
    identity = unresolved_instrument(
        symbol="FB", asset_class=AssetClass.EQUITY, first_seen=LISTED, provider="alpaca"
    )

    with pytest.raises(ValueError, match="two answers|overlap"):
        rename(identity, provider="alpaca", new_symbol="META", effective=LISTED)


def test_one_provider_has_at_most_one_current_symbol() -> None:
    """Otherwise the mapping is not a function and every lookup is a coin toss."""
    with pytest.raises(ValueError, match="not a function"):
        InstrumentIdentity(
            instrument_id="instrument-abcd",
            asset_class=AssetClass.EQUITY,
            current_symbol="META",
            status=ResolutionStatus.UNRESOLVED,
            mappings=(
                SymbolMapping(provider="alpaca", symbol="FB", valid_from=LISTED),
                SymbolMapping(provider="alpaca", symbol="META", valid_from=RENAMED),
            ),
        )


def test_a_lower_case_symbol_is_refused_rather_than_normalised_silently() -> None:
    """Two casings of one ticker would be two mappings, splitting a history invisibly."""
    with pytest.raises(ValueError, match="not upper-case"):
        SymbolMapping(provider="alpaca", symbol="fb", valid_from=LISTED)


# --- a resolver is a source, never the key -------------------------------------------------


def test_an_instrument_nobody_could_resolve_is_still_recordable() -> None:
    """The case that matters: a resolver outage must not stop a decision being attributed."""
    identity = unresolved_instrument(
        symbol="SPY", asset_class=AssetClass.ETF, first_seen=LISTED, provider="alpaca"
    )

    assert identity.status is ResolutionStatus.UNRESOLVED
    assert identity.figi is None
    assert identity.resolved is False
    assert identity.instrument_id.startswith("instrument-")


def test_a_resolved_identity_must_name_the_resolver_that_answered() -> None:
    """Without provenance it is an assertion, and this project does not store those."""
    with pytest.raises(ValueError, match="must name the resolver"):
        InstrumentIdentity(
            instrument_id="instrument-abcd",
            asset_class=AssetClass.EQUITY,
            current_symbol="META",
            status=ResolutionStatus.RESOLVED,
            figi="BBG000MM2P62",
        )


def test_ambiguous_is_conflicting_evidence_and_unresolved_is_none() -> None:
    """Collapsing them would lose the difference between two genuinely different states."""
    with pytest.raises(ValueError, match="not ambiguity"):
        InstrumentIdentity(
            instrument_id="instrument-abcd",
            asset_class=AssetClass.EQUITY,
            current_symbol="ACME",
            status=ResolutionStatus.AMBIGUOUS,
            ambiguous_candidates=("BBG000000001",),
        )

    with pytest.raises(ValueError, match="only an AMBIGUOUS"):
        InstrumentIdentity(
            instrument_id="instrument-abcd",
            asset_class=AssetClass.EQUITY,
            current_symbol="ACME",
            status=ResolutionStatus.UNRESOLVED,
            ambiguous_candidates=("BBG000000001", "BBG000000002"),
        )

    genuine = InstrumentIdentity(
        instrument_id="instrument-abcd",
        asset_class=AssetClass.EQUITY,
        current_symbol="ACME",
        status=ResolutionStatus.AMBIGUOUS,
        ambiguous_candidates=("BBG000000001", "BBG000000002"),
    )
    assert genuine.resolved is False, "ambiguity is not resolution"


def test_a_cik_must_say_where_it_came_from() -> None:
    """A CIK asserted by a market-data vendor and one read from EDGAR are different claims."""
    with pytest.raises(ValueError, match="must say where it came from"):
        InstrumentIdentity(
            instrument_id="instrument-abcd",
            asset_class=AssetClass.EQUITY,
            current_symbol="META",
            status=ResolutionStatus.UNRESOLVED,
            sec_cik="0001326801",
        )

    attributed = InstrumentIdentity(
        instrument_id="instrument-abcd",
        asset_class=AssetClass.EQUITY,
        current_symbol="META",
        status=ResolutionStatus.UNRESOLVED,
        sec_cik="0001326801",
        sec_cik_source="sec-edgar-submissions",
    )
    assert attributed.sec_cik_source == "sec-edgar-submissions"


# --- durability ----------------------------------------------------------------------------


def test_an_identity_and_its_symbol_history_round_trip(tmp_path: Path) -> None:
    """Through the real schema, so the layer is durable rather than in-process."""
    database = Database.create(tmp_path / "instruments.db")
    identity = meta()

    with database.transaction() as session:
        repository = StorageRepository(session)
        assert repository.save_instrument(identity.as_row(), identity.mapping_rows()) is True
        assert repository.save_instrument(identity.as_row(), identity.mapping_rows()) is False
        stored = repository.get_instrument(identity.instrument_id)
        windows = repository.list_instrument_symbols(identity.instrument_id)
    database.close()

    assert stored is not None
    assert stored["current_symbol"] == "META"
    assert stored["status"] == "UNRESOLVED"
    assert len(windows) == 2
    assert {row["symbol"] for row in windows} == {"FB", "META"}
    closed = next(row for row in windows if row["symbol"] == "FB")
    assert closed["valid_to"] == "2022-06-08", "the old window is closed, not deleted"


def test_a_dated_lookup_survives_the_rename_in_storage(tmp_path: Path) -> None:
    """The property, asserted where it will actually be used: a query, not a method call."""
    database = Database.create(tmp_path / "instruments.db")
    identity = meta()

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_instrument(identity.as_row(), identity.mapping_rows())

        assert (
            repository.instrument_for_symbol(
                provider="alpaca", symbol="FB", moment="2015-01-02"
            )
            == identity.instrument_id
        )
        assert (
            repository.instrument_for_symbol(
                provider="alpaca", symbol="META", moment="2023-01-03"
            )
            == identity.instrument_id
        )
        assert (
            repository.instrument_for_symbol(
                provider="alpaca", symbol="FB", moment="2023-01-03"
            )
            is None
        ), "a recycled ticker must not resolve to the instrument that gave it up"
    database.close()


def test_a_rename_rewrites_windows_without_losing_coverage(tmp_path: Path) -> None:
    """Rewriting the windows is how a closed one gets its end date; coverage is preserved."""
    database = Database.create(tmp_path / "instruments.db")
    before = unresolved_instrument(
        symbol="FB", asset_class=AssetClass.EQUITY, first_seen=LISTED, provider="alpaca"
    )

    with database.transaction() as session:
        repository = StorageRepository(session)
        repository.save_instrument(before.as_row(), before.mapping_rows())
        after = rename(before, provider="alpaca", new_symbol="META", effective=RENAMED)
        repository.replace_instrument_symbols(after.instrument_id, after.mapping_rows())

        # Every date that resolved before must still resolve, to the same instrument.
        assert (
            repository.instrument_for_symbol(
                provider="alpaca", symbol="FB", moment="2015-01-02"
            )
            == before.instrument_id
        )
    database.close()


def test_the_migration_is_additive(tmp_path: Path) -> None:
    """A real 0017 store through the real migration."""
    from alembic import command
    from alembic.config import Config

    from quantbot.storage.schema import SCHEMA_VERSION

    path = tmp_path / "v17.db"
    config = Config()
    config.set_main_option(
        "script_location", str(Path("src/quantbot/storage/migrations").resolve())
    )
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path.as_posix()}")
    command.upgrade(config, "0017")

    database = Database.migrate(path)
    try:
        with database.transaction() as session:
            assert StorageRepository(session).get_instrument("instrument-nothing") is None
        with database.engine.connect() as connection:
            marker = connection.exec_driver_sql("SELECT version FROM schema_version").fetchone()
    finally:
        database.close()

    assert marker is not None and marker[0] == SCHEMA_VERSION


def test_an_identity_records_when_and_how_it_was_resolved() -> None:
    """A mapping with no lookup time cannot have a refresh policy applied to it."""
    resolved = InstrumentIdentity(
        instrument_id="instrument-abcd",
        asset_class=AssetClass.EQUITY,
        current_symbol="META",
        status=ResolutionStatus.RESOLVED,
        figi="BBG000MM2P62",
        resolver="openfigi",
        resolver_version="v3",
        resolved_at=datetime(2026, 8, 30, tzinfo=UTC),
        source_refs=("openfigi:mapping/v3",),
    )

    row = resolved.as_row()
    assert row["resolver"] == "openfigi"
    assert row["resolver_version"] == "v3"
    assert row["resolved_at"] is not None
    assert resolved.resolved is True
