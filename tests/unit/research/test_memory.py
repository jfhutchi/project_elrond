"""What research memory must preserve, and what it must refuse to let anyone overwrite.

The import test runs against the repository's real `REFUTED.md` rather than a fixture. A parser
that works on a curated sample and not on the actual file is worth nothing, and this file is
the artifact the whole issue exists to preserve.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from quantbot.research import (
    ComparisonStructure,
    CriticVerdict,
    Critique,
    DataRole,
    DataWindow,
    EconomicProfile,
    EffectSpecification,
    EpistemicStatus,
    Estimand,
    EvidenceBasis,
    HypothesisDraft,
    HypothesisRegistry,
    PowerOverride,
    Registration,
    RegistrationRefused,
)
from quantbot.research.memory import (
    MemoryError_,
    RecordKind,
    Relation,
    ResearchMemory,
    ResearchRecord,
    Verdict,
    import_refuted_markdown,
)
from quantbot.storage import Database

NOW = datetime(2026, 8, 18, 14, 30, tzinfo=UTC)
REFUTED_MD = Path(__file__).resolve().parents[3] / "REFUTED.md"


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Database]:
    db = Database(tmp_path / "memory.db")
    yield db
    db.close()


def cleared(hypothesis_id: str = "H-2026-001", version: int = 1) -> Critique:
    """A critique raising no objection, so tests of other gates are not about this one."""
    return Critique(
        hypothesis_id=hypothesis_id,
        hypothesis_version=version,
        critic="deterministic",
        verdict=CriticVerdict.PROCEED,
        reasons=("no mechanical check produced an objection",),
        produced_at=NOW,
    )


def register(
    registry: HypothesisRegistry, candidate: HypothesisDraft, **kwargs: object
) -> Registration:
    """Register with a clean critique unless the test is about the critic gate itself."""
    kwargs.setdefault("now", NOW)
    kwargs.setdefault("critique", cleared(candidate.hypothesis_id, candidate.version))
    return registry.register(candidate, **kwargs)  # type: ignore[arg-type]



def draft(**overrides: object) -> HypothesisDraft:
    fields: dict[str, object] = {
        "hypothesis_id": "H-2026-001",
        "family_id": "trend-following",
        "question": "Does a 200-day trend filter on SPY beat buy-and-hold on Sharpe?",
        "prediction": "Sharpe is higher by at least 0.10.",
        "null_hypothesis": "The Sharpe difference is zero.",
        "falsified_if": "The paired difference fails to clear the luck bar.",
        "universe": ("SPY",),
        "features": ("close_sma_200",),
        "target": "next-session excess return",
        "windows": (
            DataWindow(
                dataset="sip-us-equities-daily",
                snapshot="2026-08-18",
                role=DataRole.PROTECTED_EVALUATION,
                start=date(2016, 1, 4),
                end=date(2026, 8, 18),
            ),
        ),
        "primary_estimand": "annualised Sharpe difference against SPY buy-and-hold",
        "effect": EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("1.0"),
            minimum_practical=Decimal("0.5"),
            justification="Cycle 16 measured SPY_SMA200 at Sharpe 0.91 in-sample.",
            comparison=ComparisonStructure.PAIRED,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        ),
        "available_observations": 2669,
        "confounders": ("regime dependence",),
        "proposed_by": "claude-opus-5",
        "basis": EvidenceBasis(
            citations=(),
            status=EpistemicStatus.DATA_DRIVEN_NO_EXTERNAL_SOURCE,
        ),
    }
    fields.update(overrides)
    return HypothesisDraft(**fields)


def finding(**overrides: object) -> ResearchRecord:
    fields: dict[str, object] = {
        "record_id": "finding-1",
        "kind": RecordKind.FINDING,
        "verdict": Verdict.REFUTED,
        "subject": "momentum ranking on US sector ETFs",
        "statement": "Top-minus-bottom is t=0.77 against a 2.87 bar.",
        "source": "cycle 15",
        "recorded_at": NOW,
    }
    fields.update(overrides)
    return ResearchRecord(**fields)


def test_an_underpowered_hypothesis_cannot_be_filed_as_refuted(database: Database) -> None:
    """The conversion this issue exists to prevent, blocked rather than discouraged.

    A hypothesis the data could never resolve was not tested. Recording it as REFUTED tells a
    later agent the mechanism failed, which is a different and false claim.
    """
    underpowered = draft(
        effect=EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("0.3"),
            minimum_practical=Decimal("0.15"),
            justification="A small but real effect.",
            comparison=ComparisonStructure.PAIRED,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        )
    )
    with database.transaction() as session, pytest.raises(RegistrationRefused) as error:
        register(HypothesisRegistry(session), underpowered, now=NOW)
    assessment = error.value.assessment
    assert assessment is not None

    with database.transaction() as session:
        HypothesisRegistry(session).record_assessment(assessment)

    refuted = finding(hypothesis_id="H-2026-001", hypothesis_version=1)
    with database.transaction() as session, pytest.raises(MemoryError_) as memory_error:
        ResearchMemory(session).record(refuted)
    assert "never tested" in str(memory_error.value)

    # Recorded as what it actually was, it stores fine.
    with database.transaction() as session:
        assert ResearchMemory(session).record(
            refuted.model_copy(update={"verdict": Verdict.UNDERPOWERED})
        )


def test_a_tested_hypothesis_can_still_be_refuted(database: Database) -> None:
    """The guard must not block the ordinary case, or it will be routed around."""
    with database.transaction() as session:
        register(HypothesisRegistry(session), draft(), now=NOW)
    with database.transaction() as session:
        assert ResearchMemory(session).record(
            finding(hypothesis_id="H-2026-001", hypothesis_version=1)
        )


def test_an_overridden_hypothesis_still_cannot_be_refuted(database: Database) -> None:
    """An operator accepting a shortfall does not turn an untested claim into a tested one."""
    underpowered = draft(
        effect=EffectSpecification(
            estimand=Estimand.SHARPE,
            expected=Decimal("0.3"),
            minimum_practical=Decimal("0.15"),
            justification="A small but real effect.",
            comparison=ComparisonStructure.PAIRED,
            economics=EconomicProfile(
                annual_rebalances=12,
                expected_annual_volatility_bps=1500,
                round_trip_cost_bps=Decimal("1.1"),
            ),
        )
    )
    with database.transaction() as session:
        register(HypothesisRegistry(session), 
            underpowered,
            now=NOW,
            override=PowerOverride(authorized_by="hutch", reason="accepted"),
        )
    with database.transaction() as session:
        memory = ResearchMemory(session)
        # The override is recorded as OVERRIDDEN, which is not in NOT_TESTED, so a refutation
        # is permitted -- the operator took responsibility for the shortfall knowingly.
        assert memory.record(finding(hypothesis_id="H-2026-001", hypothesis_version=1))


def test_a_structural_limit_cannot_be_generated(database: Database) -> None:
    """An LLM summariser produces "momentum did not work" and loses what changes decisions."""
    with pytest.raises(ValueError, match="must be curated"):
        ResearchRecord(
            record_id="limit-1",
            kind=RecordKind.STRUCTURAL_LIMIT,
            subject="statistical power",
            statement="Resolving a Sharpe gap of 0.074 would need roughly 700 years.",
            source="cycle 2",
            recorded_at=NOW,
            curated=False,
        )
    with database.transaction() as session:
        assert ResearchMemory(session).record(
            ResearchRecord(
                record_id="limit-1",
                kind=RecordKind.STRUCTURAL_LIMIT,
                subject="statistical power",
                statement="Resolving a Sharpe gap of 0.074 would need roughly 700 years.",
                source="cycle 2",
                recorded_at=NOW,
                curated=True,
            )
        )


def test_a_summary_carries_no_verdict_and_deleting_it_destroys_nothing(
    database: Database,
) -> None:
    with pytest.raises(ValueError, match="cannot carry a verdict"):
        ResearchRecord(
            record_id="summary-1",
            kind=RecordKind.SUMMARY,
            subject="momentum",
            statement="Momentum did not work.",
            verdict=Verdict.REFUTED,
            source="agent",
            recorded_at=NOW,
        )

    with database.transaction() as session:
        memory = ResearchMemory(session)
        memory.record(finding())
        memory.record(
            ResearchRecord(
                record_id="summary-1",
                kind=RecordKind.SUMMARY,
                subject="momentum",
                statement="Momentum did not work.",
                source="agent",
                recorded_at=NOW,
            )
        )
        memory.relate("summary-1", Relation.DEPENDS_ON, "finding-1", now=NOW)

    # The summary is derived; the evidence it points at is a separate row.
    with database.transaction() as session:
        memory = ResearchMemory(session)
        assert memory.get("finding-1") is not None
        assert memory.related("summary-1") == [(Relation.DEPENDS_ON, "finding-1")]


def test_recall_answers_what_have_we_learned_with_records(database: Database) -> None:
    with database.transaction() as session:
        memory = ResearchMemory(session)
        memory.record(finding())
        memory.record(
            finding(
                record_id="finding-2",
                subject="momentum ranking on global country indices",
                statement="Top minus bottom is -2.47%/yr, t=-0.78 against a 2.90 bar.",
                source="cycle 17",
            )
        )
        memory.record(
            finding(
                record_id="finding-3",
                verdict=Verdict.SURVIVED,
                subject="SPY 200-day trend",
                statement="10.34% CAGR, Sharpe 0.91, the highest measured here.",
                source="cycle 16",
            )
        )

    with database.transaction() as session:
        learned = ResearchMemory(session).recall("momentum")
    assert [record.record_id for record in learned] == ["finding-1", "finding-2"]
    assert {record.verdict for record in learned} == {Verdict.REFUTED}
    assert all(record.source for record in learned)


def test_a_conflicting_rewrite_of_a_record_is_refused(database: Database) -> None:
    with database.transaction() as session:
        memory = ResearchMemory(session)
        assert memory.record(finding()) is True
        assert memory.record(finding()) is False

    with database.transaction() as session, pytest.raises(MemoryError_, match="different content"):
        ResearchMemory(session).record(finding(statement="Actually it worked."))


def test_the_real_refuted_markdown_imports_without_rewriting_what_it_meant(
    database: Database,
) -> None:
    """Run against the repository's own file, not a fixture built to parse cleanly."""
    with database.transaction() as session:
        records = import_refuted_markdown(ResearchMemory(session), REFUTED_MD, now=NOW)

    by_kind = {kind: [r for r in records if r.kind is kind] for kind in RecordKind}
    assert len(by_kind[RecordKind.FINDING]) >= 24
    assert len(by_kind[RecordKind.STRUCTURAL_LIMIT]) >= 5
    assert by_kind[RecordKind.ANALYSIS_DEFECT]
    assert not by_kind[RecordKind.SUMMARY]

    # Verdicts come from the file's own words, and IMPOSSIBLE is not flattened into REFUTED.
    verdicts = {record.verdict for record in by_kind[RecordKind.FINDING]}
    assert Verdict.REFUTED in verdicts
    assert Verdict.SURVIVED in verdicts
    assert Verdict.IMPOSSIBLE in verdicts

    # Statements are carried verbatim: the momentum-ranking refutation keeps its numbers.
    with database.transaction() as session:
        recalled = ResearchMemory(session).recall("momentum ranking")
    assert recalled
    assert any("t=0.05" in record.statement for record in recalled)

    # And the structural limit that bounds everything is queryable rather than prose.
    with database.transaction() as session:
        limits = ResearchMemory(session).structural_limits()
    assert any("Holdout exhausted" in limit.statement for limit in limits)
    assert all(limit.curated for limit in limits)


def test_importing_twice_is_idempotent(database: Database) -> None:
    with database.transaction() as session:
        first = import_refuted_markdown(ResearchMemory(session), REFUTED_MD, now=NOW)
    with database.transaction() as session:
        second = import_refuted_markdown(ResearchMemory(session), REFUTED_MD, now=NOW)
    assert [record.record_id for record in first] == [record.record_id for record in second]
