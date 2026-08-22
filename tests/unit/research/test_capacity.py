"""What confirmatory evidence is left, and every way that answer could be flattered (#50).

The failure this module exists to prevent is not a crash. It is a sentence: "the data platform is
finished, so there is data to test on". Every test below is a way that sentence could be made to
look true by the code.
"""

from __future__ import annotations

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from quantbot.market_data.instruments import AssetClass
from quantbot.market_data.pointintime import Capability
from quantbot.research.capacity import (
    DATA_SOURCES,
    SERVED_CAPABILITIES,
    CapacityStatus,
    DataSource,
    capacity_inventory,
    capacity_row,
)
from quantbot.research.registry import (
    EXHAUSTED_TRIALS,
    DataRole,
    HypothesisRegistry,
    SearchOrigin,
    WindowState,
)
from quantbot.storage import Database
from quantbot.storage.schema import hypothesis_data_windows
from tests.unit.research.test_registry import make_draft, register, window

NOW = datetime(2026, 8, 22, 14, 30, tzinfo=UTC)
TODAY = NOW.date()
REPOSITORY = Path(__file__).resolve().parents[3]


@pytest.fixture
def database(tmp_path: Path):
    db = Database(tmp_path / "capacity.db")
    yield db
    db.close()


def by_identity(session, *, as_of: date = TODAY) -> dict[str, object]:
    return {row.observation_identity: row for row in capacity_inventory(session, as_of=as_of)}


def test_the_exhausted_equity_window_reports_as_exhausted_on_an_empty_registry(
    database: Database,
) -> None:
    """The whole point, and the case a row-counting inventory gets wrong.

    Cycles 2-10 spent 68 trials on SIP US equities 2016-2026 before this registry existed, so
    there is no `hypothesis_data_windows` row to count. An inventory built from rows would report
    the most heavily searched dataset in the project as untouched.
    """
    with database.transaction() as session:
        equities = by_identity(session)["us-equities-daily"]

    assert equities.status is CapacityStatus.EXHAUSTED
    assert equities.trials == 68
    assert any("cycles 2-10" in consumer for consumer in equities.consumers)


def test_a_second_vendor_for_the_same_dates_is_not_a_second_budget(database: Database) -> None:
    """Provider diversity establishes data correctness. It does not duplicate the market event.

    `alpaca-us-equities-daily` and `sip-us-equities-daily` are one row with one budget, and a
    hypothesis registered against either name spends the same 68-trial window. Two rows here
    would be the rename defect (#40) reappearing in the operator's own view of what is left.
    """
    with database.transaction() as session:
        inventory = capacity_inventory(session, as_of=TODAY)
        identities = [row.observation_identity for row in inventory]
        equities = by_identity(session)["us-equities-daily"]

    assert len(identities) == len(set(identities)), "one observation identity, one row"
    assert set(equities.aliases) == {"sip-us-equities-daily", "alpaca-us-equities-daily"}


def test_registering_against_a_renamed_vendor_leaves_the_window_exhausted(
    database: Database,
) -> None:
    """The acceptance criterion stated as an experiment rather than as a property.

    A registration filed against the *other* vendor name for the same dates must raise the
    spend on the shared identity, not open a fresh one.
    """
    with database.transaction() as session:
        before = by_identity(session)["us-equities-daily"]
        register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-500",
                search_cardinality=7,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2018-01-02",
                        "2019-12-31",
                        dataset="alpaca-us-equities-daily",
                    ),
                ),
            ),
            now=NOW,
        )
        _consume(session, "H-2026-500")
        after = by_identity(session)["us-equities-daily"]

    assert after.status is CapacityStatus.EXHAUSTED
    assert after.trials == before.trials + 7, "the spend landed on the shared identity"
    assert "H-2026-500 v1" in after.consumers


def test_sessions_that_had_not_happened_are_the_only_new_equity_capacity(
    database: Database,
) -> None:
    """A rolling feed keeps producing sessions, and nobody searched a session that did not exist.

    This is not a loophole in exhaustion; it is the only honest mechanism by which the equity
    window ever recovers, and the point of reporting it as a date rather than as a status is
    that an operator can see how little it is. Prior consumption is untouched by it.
    """
    with database.transaction() as session:
        equities = by_identity(session)["us-equities-daily"]
        # Prior consumption records 2016-01-04 .. 2026-08-21.
        assert equities.unconsumed_from == date(2026, 8, 22)
        assert equities.status is CapacityStatus.EXHAUSTED
        assert equities.usable_for_confirmation, "unsearched sessions are real capacity"

        # A year later, more sessions -- and not one trial of the old spend has been rewritten.
        later = by_identity(session, as_of=date(2027, 8, 22))["us-equities-daily"]

    assert later.unconsumed_from == date(2026, 8, 22)
    assert later.trials == equities.trials
    assert later.consumers == equities.consumers


def test_the_day_the_window_was_spent_is_not_yet_new_capacity(database: Database) -> None:
    """`unconsumed_from` must be a session after the last searched one, not the same one."""
    with database.transaction() as session:
        same_day = by_identity(session, as_of=date(2026, 8, 21))["us-equities-daily"]

    assert same_day.unconsumed_from is None
    assert not same_day.usable_for_confirmation


def test_crypto_capacity_is_not_equity_capacity(database: Database) -> None:
    """A different market is a different question.

    Crypto is genuinely new data for crypto claims and for a mechanism that claims cross-asset
    generality. Treating it as confirmation for a US-equity-specific effect is the rename defect
    wearing an asset class, and the inventory must not make that easy by pooling the rows.
    """
    with database.transaction() as session:
        inventory = by_identity(session)

    crypto = inventory["crypto-daily"]
    equities = inventory["us-equities-daily"]

    assert crypto.source.asset_class is AssetClass.CRYPTO
    assert crypto.status is CapacityStatus.AVAILABLE
    assert crypto.trials == 0, "the equity spend did not land here"
    assert equities.trials == 68
    assert "NOT new evidence for a US-equity-specific effect" in crypto.source.answers


def test_macro_capacity_is_its_own_row_with_vintage_semantics(database: Database) -> None:
    """FRED/ALFRED is real capacity for macro claims, and not an independent equity sample."""
    with database.transaction() as session:
        macro = by_identity(session)["us-macro-monthly"]

    assert macro.source.capability is Capability.MACRO_SERIES
    assert macro.source.point_in_time, "a revised figure must not read as what was published"
    assert macro.status is CapacityStatus.AVAILABLE
    assert set(macro.aliases) == {"fred-macro-monthly", "alfred-macro-monthly"}
    assert "does not create an independent equity sample" in macro.source.answers


def test_a_capability_with_no_provider_reports_unsupported(database: Database) -> None:
    """`OPTIONS_DERIVED` is declared in two enums and served by nothing.

    A row saying `AVAILABLE` would be capacity the project cannot spend, and the research
    director would happily plan against it.
    """
    with database.transaction() as session:
        options = by_identity(session)["options-implied-daily"]

    assert Capability.OPTIONS_DERIVED not in SERVED_CAPABILITIES
    assert options.status is CapacityStatus.UNSUPPORTED
    assert not options.usable_for_confirmation


def test_unsupported_flips_to_available_when_a_provider_arrives(database: Database) -> None:
    """Derived, not declared: implementing a provider is what changes the answer.

    If this were a hand-set status field, shipping an options provider would leave the inventory
    saying UNSUPPORTED until somebody remembered -- and an inventory that has to be remembered
    goes stale in whichever direction the last editor guessed.
    """
    served = DataSource(
        dataset="fundamentals-only",
        asset_class=AssetClass.EQUITY,
        capability=Capability.FUNDAMENTALS,
        earliest=date(2020, 1, 1),
        latest=None,
        point_in_time=True,
        permitted_roles=(DataRole.PROTECTED_EVALUATION,),
        answers="filings",
    )
    unserved = DataSource(
        dataset="options-only",
        asset_class=AssetClass.OPTIONS_DERIVED,
        capability=Capability.OPTIONS_DERIVED,
        earliest=date(2020, 1, 1),
        latest=None,
        point_in_time=True,
        permitted_roles=(),
        answers="implied vol",
    )
    with database.transaction() as session:
        assert capacity_row(session, served, as_of=TODAY).status is CapacityStatus.AVAILABLE
        assert capacity_row(session, unserved, as_of=TODAY).status is CapacityStatus.UNSUPPORTED


def test_a_source_that_cannot_answer_point_in_time_questions_is_quality_blocked(
    database: Database,
) -> None:
    """Data that only says what is true now cannot say what was knowable then."""
    revised = DataSource(
        dataset="latest-only-fundamentals",
        asset_class=AssetClass.EQUITY,
        capability=Capability.FUNDAMENTALS,
        earliest=date(2020, 1, 1),
        latest=None,
        point_in_time=False,
        permitted_roles=(DataRole.DISCOVERY,),
        answers="restated figures with no vintage",
    )
    with database.transaction() as session:
        row = capacity_row(session, revised, as_of=TODAY)

    assert row.status is CapacityStatus.QUALITY_BLOCKED
    assert not row.usable_for_confirmation


def test_a_live_reservation_is_neither_free_nor_spent(database: Database) -> None:
    """A reservation is a trial someone intends to spend (#22).

    Counting it as consumption would let an abandoned registration sterilise a dataset; ignoring
    it entirely would let two hypotheses claim the same holdout. It is its own status, and it
    lapses on its own.
    """
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-501",
                universe=("BTC/USD",),
                features=("close_sma_020",),
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2022-01-01",
                        "2023-12-31",
                        dataset="crypto-daily",
                    ),
                ),
            ),
            now=NOW,
        )
        held = by_identity(session)["crypto-daily"]
        assert held.status is CapacityStatus.RESERVED
        assert held.reserved_by == ("H-2026-501 v1",)
        assert held.trials == 0, "a reservation is not a spend"

        # It lapses rather than waiting to be released: abandonment is the absence of events.
        lapsed = by_identity(session, as_of=date(2027, 8, 22))["crypto-daily"]

    assert lapsed.status is CapacityStatus.AVAILABLE
    assert lapsed.reserved_by == ()


def test_consumption_past_the_bar_is_exhausted_and_below_it_is_partial(
    database: Database,
) -> None:
    """The two intermediate states are distinguishable, or the panel says only yes and no."""
    with database.transaction() as session:
        register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-502",
                universe=("BTC/USD",),
                features=("close_sma_020",),
                search_cardinality=EXHAUSTED_TRIALS - 1,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
                windows=(
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2022-01-01",
                        "2023-12-31",
                        dataset="crypto-daily",
                    ),
                ),
            ),
            now=NOW,
        )
        _consume(session, "H-2026-502")
        partial = by_identity(session)["crypto-daily"]
        assert partial.status is CapacityStatus.PARTIALLY_CONSUMED
        assert partial.usable_for_confirmation

        register(
            HypothesisRegistry(session),
            make_draft(
                hypothesis_id="H-2026-503",
                universe=("ETH/USD",),
                features=("close_ret_005",),
                search_cardinality=5,
                search_origin=SearchOrigin.OPERATOR_ATTESTED,
                search_attested_by="hutch",
                windows=(
                    # A different slice of the same source: two hypotheses may not claim the
                    # same protected window (#22), and exhaustion is a property of the source
                    # rather than of one range within it.
                    window(
                        DataRole.PROTECTED_EVALUATION,
                        "2024-01-01",
                        "2024-12-31",
                        dataset="crypto-daily",
                    ),
                ),
            ),
            now=NOW,
        )
        _consume(session, "H-2026-503")
        spent = by_identity(session)["crypto-daily"]

    assert spent.status is CapacityStatus.EXHAUSTED


def test_every_implemented_provider_is_represented_in_served_capabilities() -> None:
    """`SERVED_CAPABILITIES` is hand-assembled, so a walk checks nothing was added past it.

    Without this, shipping an options provider would leave the inventory reporting UNSUPPORTED
    for a capability the project can now serve -- understating capacity, which is the safe
    direction, and still wrong. The opposite edit -- deleting a provider -- would overstate it,
    which is not safe at all.
    """
    served: set[str] = set()
    for path in (REPOSITORY / "src" / "quantbot").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "capabilities":
                continue
            for inner in ast.walk(node):
                # `Capability.X` written inline, and the module constants providers return.
                if isinstance(inner, ast.Attribute) and getattr(inner.value, "id", "") == (
                    "Capability"
                ):
                    served.add(inner.attr)
                elif isinstance(inner, ast.Name) and inner.id.endswith("_CAPABILITIES"):
                    served.add(inner.id)

    named = {item.value for item in SERVED_CAPABILITIES}
    unrepresented = {item for item in served if not item.endswith("_CAPABILITIES")} - named
    assert not unrepresented, (
        "a provider serves a capability the capacity inventory does not know about, so the "
        f"inventory understates what the project can research: {sorted(unrepresented)}"
    )


def test_every_declared_source_says_what_a_claim_must_be_about() -> None:
    """`answers` is the field that stops a source being reused for the wrong question.

    An inventory listing observations without saying which claims they bear on invites exactly
    the substitution #50 is about -- crypto sessions read as equity evidence because both rows
    said AVAILABLE.
    """
    for source in DATA_SOURCES:
        assert len(source.answers) > 40, f"{source.dataset} does not say what it can answer"
        assert source.earliest < TODAY


def _consume(session, hypothesis_id: str) -> None:
    """Mark a registration's windows as actually read, which is what spends them (#22)."""
    session.execute(
        hypothesis_data_windows.update()
        .where(hypothesis_data_windows.c.hypothesis_id == hypothesis_id)
        .values(state=WindowState.CONSUMED.value, consumed_at=NOW.isoformat())
    )
