"""What has Elrond learned about X, and the ways that answer could be a fabrication (#6)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from quantbot.research.memory import RecordKind, ResearchMemory, ResearchRecord, Verdict
from quantbot.research.models import (
    CostClass,
    ModelCapabilities,
    ModelRole,
    ModelRuntime,
    ModelSpec,
    OpenAICompatibleTransport,
    RoleRouting,
)
from quantbot.research.synthesis import SynthesisRefused, synthesize_lessons
from quantbot.storage import Database

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path) -> Iterator[Database]:
    db = Database(tmp_path / "synthesis.db")
    yield db
    db.close()


def runtime_returning(answer: dict[str, object]) -> ModelRuntime:
    def post(url: str, payload: object, *, timeout_seconds: float) -> str:
        return json.dumps(
            {
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": 900, "completion_tokens": 200},
            }
        )

    spec = ModelSpec(
        provider="openai-compatible",
        model="summariser",
        version="1",
        endpoint="http://localhost:11434/v1",
        capabilities=ModelCapabilities(
            context_tokens=32768, structured_output=True, local=True, cost_class=CostClass.LOCAL
        ),
    )
    return ModelRuntime(
        RoleRouting(chains={ModelRole.SUMMARIZER: (spec,)}), OpenAICompatibleTransport(post)
    )


def seed(memory: ResearchMemory) -> None:
    memory.record(
        ResearchRecord(
            record_id="finding-1",
            kind=RecordKind.FINDING,
            verdict=Verdict.REFUTED,
            subject="momentum ranking on US sector ETFs",
            statement="Top-minus-bottom is t=0.77 against a 2.87 bar.",
            source="cycle 15",
            recorded_at=NOW,
        )
    )
    memory.record(
        ResearchRecord(
            record_id="finding-2",
            kind=RecordKind.FINDING,
            verdict=Verdict.UNDERPOWERED,
            subject="momentum on crypto majors",
            statement="Could only detect 1.60 Sharpe; measured 0.53.",
            source="cycle 16",
            recorded_at=NOW,
        )
    )


def test_a_lesson_is_a_summary_that_cites_the_records_it_rests_on(database: Database) -> None:
    """CLAUDE.md's own acceptance test: evidence-linked knowledge, not regenerated narrative."""
    with database.transaction() as session:
        memory = ResearchMemory(session)
        seed(memory)
        lesson, response = synthesize_lessons(
            runtime_returning(
                {
                    "claims": [
                        {
                            "statement": "Sector-ETF momentum did not clear its bar.",
                            "cites": ["finding-1"],
                        },
                        {
                            "statement": "Crypto momentum remains untested at usable power.",
                            "cites": ["finding-2"],
                        },
                    ],
                    "open_questions": ["does momentum survive on a longer macro history"],
                }
            ),
            memory,
            "momentum",
            record_id="lesson-momentum",
            now=NOW,
        )

    assert lesson.kind is RecordKind.SUMMARY
    # A summary is derived, not evidence. `ResearchRecord` forbids it carrying a verdict.
    assert lesson.verdict is None
    assert "finding-1" in lesson.statement
    assert "finding-2" in lesson.statement
    assert "Open: does momentum survive" in lesson.statement
    assert lesson.detail["records"] == "finding-1, finding-2"
    assert lesson.detail["model"].startswith("summariser")
    assert response.total_tokens == 1100


def test_a_claim_citing_a_record_nobody_supplied_refuses_the_whole_synthesis(
    database: Database,
) -> None:
    """The mechanical guarantee, and the reason it is mechanical.

    Whether a summary is faithful to its sources is a judgment. Whether it cites an id that was
    never supplied is arithmetic, checkable without reading a word of the prose. One invented
    citation is enough to distrust the rest.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        seed(memory)

        with pytest.raises(SynthesisRefused, match="finding-99"):
            synthesize_lessons(
                runtime_returning(
                    {
                        "claims": [
                            {"statement": "Momentum works on equities.", "cites": ["finding-99"]}
                        ]
                    }
                ),
                memory,
                "momentum",
                record_id="lesson-momentum",
                now=NOW,
            )


def test_nothing_is_synthesised_from_nothing(database: Database) -> None:
    """An empty recall raises rather than summarising the absence.

    "No evidence was found about X" is a sentence a later reader quotes as though somebody had
    looked, and it would be stored beside sentences where somebody did.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        seed(memory)

        with pytest.raises(SynthesisRefused, match="no records"):
            synthesize_lessons(
                runtime_returning({"claims": [{"statement": "x", "cites": ["finding-1"]}]}),
                memory,
                "carry trades in frontier FX",
                record_id="lesson-empty",
                now=NOW,
            )


def test_a_synthesis_is_never_built_from_another_synthesis(database: Database) -> None:
    """Each round drifts one step further from the evidence, citing something itself uncited."""
    with database.transaction() as session:
        memory = ResearchMemory(session)
        seed(memory)
        memory.record(
            ResearchRecord(
                record_id="lesson-earlier",
                kind=RecordKind.SUMMARY,
                subject="momentum",
                statement="Momentum has been broadly unpromising. [finding-1]",
                source="an earlier synthesis",
                recorded_at=NOW,
            )
        )

        lesson, _ = synthesize_lessons(
            runtime_returning(
                {"claims": [{"statement": "Sector momentum failed.", "cites": ["finding-1"]}]}
            ),
            memory,
            "momentum",
            record_id="lesson-new",
            now=NOW,
        )

    assert "lesson-earlier" not in lesson.detail["records"]


def test_a_synthesis_citing_the_excluded_summary_is_refused(database: Database) -> None:
    """Excluding summaries from the input is only half of it.

    If a model could still cite one -- from an earlier call, or by guessing an id pattern -- the
    exclusion would be cosmetic. The citation check is what makes it real.
    """
    with database.transaction() as session:
        memory = ResearchMemory(session)
        seed(memory)
        memory.record(
            ResearchRecord(
                record_id="lesson-earlier",
                kind=RecordKind.SUMMARY,
                subject="momentum",
                statement="Momentum has been broadly unpromising.",
                source="an earlier synthesis",
                recorded_at=NOW,
            )
        )

        with pytest.raises(SynthesisRefused, match="lesson-earlier"):
            synthesize_lessons(
                runtime_returning(
                    {"claims": [{"statement": "As established.", "cites": ["lesson-earlier"]}]}
                ),
                memory,
                "momentum",
                record_id="lesson-new",
                now=NOW,
            )
