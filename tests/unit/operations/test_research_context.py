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

    Asserted by construction: it holds two identifiers and no policy, threshold or approval. A
    field that could relax a limit would be the failure `regime.py` is structurally forbidden
    from committing, reintroduced one layer up.
    """
    from dataclasses import fields

    names = {item.name for item in fields(ResearchContext)}

    assert names == {"anomaly_event_ids", "regime_snapshot_id"}


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
