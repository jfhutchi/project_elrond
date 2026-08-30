"""Refusals carry what the market looked like when they were made (#56, #59).

`rejected_candidates.market_regime_snapshot_id` has existed since migration 0012 and
`anomaly_event_id` since 0016. `RejectedCandidate` has had the matching fields the whole time.
Nothing ever wrote either one, so every refusal on record was uncontextualised while the schema
implied otherwise -- and a study conditioning on regime would have found every column null and
concluded the context did not vary.

That is the sixth instance in this project of a control correct in isolation and never reached
in production. The failure mode here is quieter than the others: a null column is not an error,
it is an absence, and absence is exactly what this project treats as honest everywhere else.
So these tests assert the ids are *present* on a real refusal, not merely that the plumbing
type-checks.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from quantbot.operations.runners import AdaptiveMomentumRunner, ResearchContext
from quantbot.risk.vetoes import (
    PortfolioVetoDecision,
    PortfolioVetoPolicy,
    VetoName,
    VetoRejection,
)
from quantbot.storage import Database, StorageRepository
from tests.unit.anomaly.test_persistence import NOW as EVENT_OBSERVED
from tests.unit.anomaly.test_persistence import event as anomaly_event

NOW = datetime(2026, 8, 14, 20, 5, tzinfo=UTC)
SESSION = date(2026, 8, 14)


def decision() -> PortfolioVetoDecision:
    rejection = VetoRejection(
        veto=VetoName.CORRELATION_CONCENTRATION,
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        reason="QQQ, SPY move together and would exceed the cluster ceiling",
        dependence_snapshot_identity="dependence-v1-cccc",
    )
    return PortfolioVetoDecision(
        symbol="SPY",
        policy_version=1,
        rule_parameters_hash="veto-policy-v1-aaaa",
        evaluated=(VetoName.CORRELATION_CONCENTRATION,),
        rejections=(rejection,),
    )


def record(repository: StorageRepository, research: ResearchContext) -> dict[str, object]:
    runner = SimpleNamespace(
        _identity=SimpleNamespace(
            strategy_id="adaptive-momentum", configuration_hash="cfg-abc"
        ),
        _veto_policy=PortfolioVetoPolicy(),
    )
    AdaptiveMomentumRunner._record_rejection(  # noqa: SLF001 - the composition is the subject
        runner,
        repository,
        symbol="SPY",
        reference_price=Decimal("500"),
        observed_at=NOW,
        candidate_key="entry",
        vetoes=decision(),
        research=research,
    )
    parent, _ = repository.list_rejected_candidates()[0]
    return dict(parent)


@pytest.fixture
def repository(tmp_path: Path):
    database = Database.create(tmp_path / "context.db")
    try:
        with database.transaction() as session:
            yield StorageRepository(session)
    finally:
        database.close()


def test_a_refusal_records_the_regime_it_was_made_in(repository) -> None:
    """Without it a regime study sees one undifferentiated pool of rejections."""
    row = record(
        repository,
        ResearchContext(regime_snapshot_id="market-regime-v1-abcd1234"),
    )

    assert row["market_regime_snapshot_id"] == "market-regime-v1-abcd1234"


def test_a_refusal_records_the_anomaly_that_fired_for_its_own_symbol(repository) -> None:
    """Per-symbol, not per-cycle. Binding another symbol's event would attach an explanation to
    a refusal it does not explain, which is worse than leaving it absent.
    """
    row = record(
        repository,
        ResearchContext(
            anomaly_event_ids={"SPY": "anomaly-spy-0001", "QQQ": "anomaly-qqq-0002"}
        ),
    )

    assert row["anomaly_event_id"] == "anomaly-spy-0001"


def test_a_symbol_with_no_anomaly_records_absence_rather_than_a_neighbours_event(
    repository,
) -> None:
    """The specific way this binding could manufacture a finding.

    If a symbol with no anomaly inherited whatever event the cycle happened to produce, every
    refusal in a volatile cycle would look catalysed and the association would be an artifact of
    the wiring rather than the market.
    """
    row = record(repository, ResearchContext(anomaly_event_ids={"QQQ": "anomaly-qqq-0002"}))

    assert row["anomaly_event_id"] is None


def test_absent_context_is_null_rather_than_a_neutral_label(repository) -> None:
    """A substituted context cannot be told from a measured one once it is inside an analysis."""
    row = record(repository, ResearchContext())

    assert row["market_regime_snapshot_id"] is None
    assert row["anomaly_event_id"] is None
    # And the refusal itself is unaffected -- context is not a precondition for recording one.
    assert row["symbol"] == "SPY"


def test_the_context_carries_no_authority_over_the_decision() -> None:
    """`ResearchContext` describes a decision; it must never be able to change one.

    Asserted by construction: it holds identifiers and no policy, threshold or approval. A field
    that could relax a limit would be the failure `regime.py` is structurally forbidden from
    committing, reintroduced one layer up.

    The exact-set assertion is the point. This test failed when `catalyst_ids` was added, which
    is correct behaviour: every widening of this object gets looked at once, deliberately, rather
    than accumulating fields until one of them carries a decision.
    """
    from dataclasses import fields

    names = {item.name for item in fields(ResearchContext)}

    assert names == {"anomaly_event_ids", "regime_snapshot_id", "catalyst_ids"}
    assert all(
        "id" in name or "snapshot" in name for name in names
    ), "every field names a record; none of them is a value a gate could read"


# --- containment ---------------------------------------------------------------------------
#
# The first version of `_record_anomalies` promised containment in its docstring and had no
# `except` anywhere in it, so a detector raising would have taken the trading cycle down while
# the comment above it said the opposite. These tests exist so that cannot recur silently.


def failing_runner(database, method: str):
    """A runner whose research recording is guaranteed to fail."""

    def explode(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("the detector fell over")

    namespace = SimpleNamespace(_database=database)
    setattr(namespace, method, explode)
    return with_real_containment(namespace)


def with_real_containment(namespace: SimpleNamespace) -> SimpleNamespace:
    """Use the production `_record_contained_failure`, not a stub.

    The incident write is half of what containment means here, so a stub would leave these tests
    asserting only that nothing raised -- which the exception handler would satisfy while
    recording nothing at all.
    """
    namespace._record_contained_failure = (  # noqa: SLF001
        lambda **keywords: AdaptiveMomentumRunner._record_contained_failure(  # noqa: SLF001
            namespace, **keywords
        )
    )
    return namespace


def test_a_detector_failure_does_not_stop_the_trading_cycle(tmp_path: Path) -> None:
    """An observability feature must not be able to halt trading.

    Anomaly detection describes decisions; it does not make them. Losing a cycle of it is a gap
    in a research dataset, and stopping the cycle to protect that dataset is the wrong trade in
    both directions.
    """
    database = Database.create(tmp_path / "contained.db")
    try:
        runner = failing_runner(database, "_detect_and_record_anomalies")

        result = AdaptiveMomentumRunner._record_anomalies(  # noqa: SLF001
            runner, {}, None, observed_at=NOW
        )

        assert result == {}, "no events, and no exception either"
    finally:
        database.close()


def test_a_contained_failure_is_recorded_rather_than_swallowed(tmp_path: Path) -> None:
    """Contained is not silent.

    A swallowed failure that leaves no trace produces a dataset with invisible holes, which for
    research purposes is worse than a loud failure: a later study cannot distinguish "no anomaly
    fired" from "detection was broken that day".
    """
    database = Database.create(tmp_path / "incident.db")
    try:
        runner = failing_runner(database, "_detect_and_record_anomalies")
        AdaptiveMomentumRunner._record_anomalies(runner, {}, None, observed_at=NOW)  # noqa: SLF001

        with database.transaction() as session:
            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert len(incidents) == 1
    assert incidents[0].kind == "anomaly-detection"
    assert "fell over" in incidents[0].message


def test_a_regime_failure_is_contained_and_recorded_the_same_way(tmp_path: Path) -> None:
    """Same rule, and worth asserting separately: it is a different call path with its own
    `except`, so one being right says nothing about the other.
    """
    database = Database.create(tmp_path / "regime.db")
    try:
        runner = with_real_containment(
            SimpleNamespace(
                _database=database,
                _config=SimpleNamespace(regime_symbol="SPY"),
                _market_data=SimpleNamespace(
                    provider="alpaca",
                    feed=SimpleNamespace(value="sip"),
                    adjustment=SimpleNamespace(value="all"),
                ),
            )
        )
        # A bar list the builder cannot read: enough to reach the try, guaranteed to fail in it.
        result = AdaptiveMomentumRunner._record_regime(  # noqa: SLF001
            runner,
            {"SPY": [object()]},  # type: ignore[dict-item]
            as_of_session=SESSION,
            observed_at=NOW,
        )

        with database.transaction() as session:
            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert result is None, "absent, never a default label"
    assert [item.kind for item in incidents] == ["market-regime"]


def test_a_benchmark_with_no_bars_is_absent_rather_than_an_incident(tmp_path: Path) -> None:
    """Not every gap is a fault. A cycle that read no benchmark bars has nothing to describe, and
    filing an incident for it would train the operator to ignore the ones that mean something.
    """
    database = Database.create(tmp_path / "nobars.db")
    try:
        runner = with_real_containment(
            SimpleNamespace(_database=database, _config=SimpleNamespace(regime_symbol="SPY"))
        )
        result = AdaptiveMomentumRunner._record_regime(  # noqa: SLF001
            runner, {}, as_of_session=SESSION, observed_at=NOW
        )

        with database.transaction() as session:
            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert result is None
    assert incidents == []


# --- catalyst enrichment reaches production ------------------------------------------------
#
# `CatalystAssessment.as_row()` existed for a table that did not, and nothing called either.
# These assert the wiring, which is the part that was missing all seven previous times.


class StubSource:
    """A catalyst source that answers however the test says, and records what it was asked."""

    def __init__(self, result=None, error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.asked: list[tuple[str, ...]] = []

    async def fetch(self, symbols, *, window_start, observed_at, retrieved_at):
        self.asked.append(tuple(symbols))
        if self._error is not None:
            raise self._error
        return self._result


def healthy_source(*candidates):
    from quantbot.anomaly.catalyst import SourceHealth, SourceResult

    return SourceResult(
        source="alpaca-news", health=SourceHealth.HEALTHY, candidates=tuple(candidates)
    )


def catalyst_runner(database, source):
    """Always with the production containment handler, and always a *sequence* of sources.

    The runner takes several (#83) so `assess_catalysts` has something to apply its rule to: one
    unhealthy source makes the whole assessment UNKNOWN however many others answered.

    Without it these tests would pass or fail on whether the stub happened to raise, rather than
    on the wiring under test -- and the first version did exactly that: an assessment for an
    anomaly no test had recorded hit the foreign key, and the missing handler turned a contained
    failure into an `AttributeError`.
    """
    return with_real_containment(
        SimpleNamespace(
            _database=database,
            _catalyst_sources=() if source is None else (source,),
        )
    )


async def run_catalysts(runner, events, observed_at=NOW):
    return await AdaptiveMomentumRunner._record_catalysts(  # noqa: SLF001
        runner, events, observed_at=observed_at
    )


@pytest.mark.asyncio
async def test_enrichment_is_asked_only_about_symbols_that_produced_an_anomaly(
    tmp_path: Path,
) -> None:
    """The routing gate's 98.24% search saving is the reason enrichment is affordable at all.

    Asking a news provider about every symbol every cycle would spend exactly the saving that
    justified building this, and would do it while adding nothing: a symbol with no anomaly has
    no event for a catalyst to explain.
    """
    database = Database.create(tmp_path / "asked.db")
    try:
        source = StubSource(healthy_source())
        await run_catalysts(catalyst_runner(database, source), {"SPY": "anomaly-1"})
    finally:
        database.close()

    assert source.asked == [("SPY",)]


@pytest.mark.asyncio
async def test_no_configured_source_records_nothing_rather_than_unknown(
    tmp_path: Path,
) -> None:
    """A lookup that never happened is not an outcome.

    Recording `CATALYST_UNKNOWN` for every event would fill the table with rows saying nobody
    had been asked, which reads later as "this was investigated and could not be established"
    -- a much stronger statement than "enrichment is not configured".
    """
    database = Database.create(tmp_path / "unconfigured.db")
    try:
        result = await run_catalysts(catalyst_runner(database, None), {"SPY": "anomaly-1"})

        with database.transaction() as session:
            repository = StorageRepository(session)
            stored = repository.list_catalyst_assessments()
            incidents = repository.list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert result == {}
    assert stored == []
    # And nothing went wrong either. Without this the test passes when the code *does* try to
    # use the absent source, blows up, and has the failure contained -- which is a different
    # behaviour with the same visible result.
    assert incidents == []


@pytest.mark.asyncio
async def test_a_provider_failure_is_contained_and_recorded(tmp_path: Path) -> None:
    """Enrichment describes a decision; it must not be able to stop one.

    And the gap is an incident, so a later study can tell "no catalyst was found" from "the
    lookup never completed that day".
    """
    database = Database.create(tmp_path / "contained.db")
    try:
        runner = catalyst_runner(
            database, StubSource(error=RuntimeError("provider exploded"))
        )
        result = await run_catalysts(runner, {"SPY": "anomaly-1"})

        with database.transaction() as session:
            incidents = StorageRepository(session).list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert result == {}
    assert [item.kind for item in incidents] == ["catalyst-enrichment"]


@pytest.mark.asyncio
async def test_the_assessment_is_actually_written_and_its_id_returned(tmp_path: Path) -> None:
    """Built and returned is not stored, and the two are easy to confuse in a diff.

    Without this the recorder could assemble a perfectly correct assessment, hand back its id,
    and persist nothing -- leaving refusals pointing at rows that do not exist.
    """
    database = Database.create(tmp_path / "written.db")
    try:
        with database.transaction() as session:
            StorageRepository(session).record_anomaly_event(anomaly_event().as_row())

        runner = catalyst_runner(database, StubSource(healthy_source()))
        result = await run_catalysts(
            runner, {"SPY": anomaly_event().event_id}, observed_at=EVENT_OBSERVED
        )

        with database.transaction() as session:
            repository = StorageRepository(session)
            stored = repository.list_catalyst_assessments()
            incidents = repository.list_incidents(unresolved_only=True)
    finally:
        database.close()

    assert incidents == [], "the write path completed rather than being contained"
    assert len(stored) == 1
    assert stored[0]["outcome"] == "NO_PUBLIC_CATALYST_FOUND"
    assert result["SPY"] == stored[0]["assessment_id"], (
        "the id handed to the refusal is the id of the row that was written"
    )


def test_a_refusal_records_the_catalyst_assessed_for_its_own_anomaly(repository) -> None:
    """Per symbol, like the anomaly id and for the same reason.

    Binding a neighbour's catalyst would attach an explanation to a refusal it does not explain.
    """
    row = record(
        repository,
        ResearchContext(
            catalyst_ids={"SPY": "catalyst-v1-spy", "QQQ": "catalyst-v1-qqq"}
        ),
    )

    assert row["catalyst_snapshot_id"] == "catalyst-v1-spy"


def test_a_symbol_with_no_catalyst_assessment_records_absence(repository) -> None:
    """Most refusals have no anomaly at all, so most must carry no catalyst."""
    row = record(repository, ResearchContext(catalyst_ids={"QQQ": "catalyst-v1-qqq"}))

    assert row["catalyst_snapshot_id"] is None


def test_enrichment_runs_only_over_the_anomalies_at_the_call_site() -> None:
    """Asserted structurally, because a unit test on the recorder cannot see this.

    `_record_catalysts` faithfully asks about whatever it is given; the property that matters is
    *what the cycle gives it*. Passing the whole sliced universe would spend the routing gate's
    98.24% search saving -- the entire reason enrichment is affordable -- and no test of the
    recorder would notice, which is exactly how the first version of this passed.
    """
    import ast

    tree = ast.parse(Path("src/quantbot/operations/runners.py").read_text(encoding="utf-8"))
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_record_catalysts"
    )
    first = call.args[0] if call.args else None

    assert isinstance(first, ast.Name), (
        "enrichment must be handed the anomaly events by name, not a comprehension over the "
        f"universe (got {ast.dump(first) if first else 'nothing'})"
    )
    assert first.id == "anomaly_event_ids"


def test_the_composition_root_supplies_a_catalyst_source() -> None:
    """The eighth instance of the pattern, prevented rather than found later.

    `_record_catalysts` returns `{}` when no source is configured -- correct, and indistinguishable
    from a system where enrichment is simply never wired. Every unit test above would pass with
    `catalyst_source` omitted at the composition root, and the whole subsystem would be dead code
    that looks alive. Asserted structurally, because that is the only place the fact lives.
    """
    import ast

    tree = ast.parse(Path("src/quantbot/runtime.py").read_text(encoding="utf-8"))
    runner = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AdaptiveMomentumRunner"
    )
    supplied = {keyword.arg for keyword in runner.keywords}

    assert "catalyst_sources" in supplied, "enrichment would never run in production"


def test_credentials_are_not_unwrapped_before_the_request() -> None:
    """A `SecretStr` that becomes a `str` early is one `repr` away from a logged key."""
    from pydantic import SecretStr

    from quantbot.anomaly.news import AlpacaNewsSource

    news = AlpacaNewsSource(
        StubSource(None), api_key=SecretStr("pk-secret"), api_secret=SecretStr("sk-secret")
    )

    assert "pk-secret" not in repr(vars(news))
    assert "sk-secret" not in repr(vars(news))


# --- several sources, and the rule that only has meaning with several (#83) -------------------


@pytest.mark.asyncio
async def test_every_configured_source_is_consulted(tmp_path: Path) -> None:
    """The denominator of a negative finding is which sources were actually asked.

    With one source the question does not arise. With two, a result that consulted only the first
    would report "no public catalyst" on the strength of half the evidence it claimed.
    """
    database = Database.create(tmp_path / "two.db")
    try:
        with database.transaction() as session:
            StorageRepository(session).record_anomaly_event(anomaly_event().as_row())
        first, second = StubSource(healthy_source()), StubSource(healthy_source())
        runner = SimpleNamespace(
            _database=database, _catalyst_sources=(first, second)
        )
        await run_catalysts(
            with_real_containment(runner),
            {"SPY": anomaly_event().event_id},
            observed_at=EVENT_OBSERVED,
        )

        with database.transaction() as session:
            stored = StorageRepository(session).list_catalyst_assessments()
    finally:
        database.close()

    assert first.asked == [("SPY",)] and second.asked == [("SPY",)]
    assert stored[0]["sources_consulted"].count("alpaca-news") == 2


@pytest.mark.asyncio
async def test_one_unhealthy_source_makes_the_whole_answer_unknown(tmp_path: Path) -> None:
    """The rule `assess_catalysts` has always had and the runner could never exercise.

    A source that could not answer makes the question unanswerable regardless of what the others
    found. Letting the healthy one stand in would turn a provider outage into a discovery -- and
    outages cluster when markets are busy, which is when anomalies fire.
    """
    from quantbot.anomaly.catalyst import SourceHealth, SourceResult

    database = Database.create(tmp_path / "mixed.db")
    try:
        with database.transaction() as session:
            StorageRepository(session).record_anomaly_event(anomaly_event().as_row())
        runner = SimpleNamespace(
            _database=database,
            _catalyst_sources=(
                StubSource(healthy_source()),
                StubSource(
                    SourceResult(
                        source="second-provider",
                        health=SourceHealth.RATE_LIMITED,
                        detail="rate limited",
                    )
                ),
            ),
        )
        await run_catalysts(
            with_real_containment(runner),
            {"SPY": anomaly_event().event_id},
            observed_at=EVENT_OBSERVED,
        )

        with database.transaction() as session:
            stored = StorageRepository(session).list_catalyst_assessments()
    finally:
        database.close()

    assert stored[0]["outcome"] == "CATALYST_UNKNOWN"
    assert "second-provider=RATE_LIMITED" in stored[0]["unhealthy_sources"]
