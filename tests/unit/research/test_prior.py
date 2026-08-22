"""The project's settled findings, in the form the gates actually read (#6).

Written because of a live gap rather than a hypothetical one. On 2026-08-22 the running system
held no research records, no registered hypotheses, no recorded trials and no claimed windows,
while `REFUTED.md` recorded twenty-four settled hypotheses. Every gate built to stop a repeat
was reading an empty table, so the constraint existed only in prose -- which is the exact
situation `window_consumption`'s docstring says it exists to prevent.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from quantbot.research.director import ResearchTask, TaskState
from quantbot.research.memory import RecordKind, ResearchMemory, Verdict
from quantbot.research.prior import PRIOR_FINDINGS, load_prior_findings, prior_records
from quantbot.research.stages import SETTLED, novelty_stage
from quantbot.storage.database import Database

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
REFUTED = Path(__file__).resolve().parents[3] / "REFUTED.md"


def memory(tmp_path: Path):
    return Database(tmp_path / "research.db")


def task(question: str, family_id: str) -> ResearchTask:
    """A PROPOSED task, built directly: the novelty stage takes a task, not a director."""
    return ResearchTask(
        task_id=f"task-{family_id}",
        state=TaskState.PROPOSED,
        question=question,
        family_id=family_id,
        created_at=NOW,
        updated_at=NOW,
    )


def test_the_seed_and_refuted_md_cannot_drift_apart_silently() -> None:
    """Adding a row to REFUTED.md without seeding it must fail, not pass quietly.

    This is why the findings are curated as data rather than parsed out of the markdown: a
    parser that silently matched nothing would reproduce the empty ledger it is meant to fix,
    and that failure looks identical to success. This check is the safety net instead.
    """
    rows = re.findall(r"^\| (\d+) \|", REFUTED.read_text(encoding="utf-8"), re.MULTILINE)
    documented = {int(number) for number in rows}
    seeded = {number for number, _, _, _ in PRIOR_FINDINGS}

    assert seeded == documented, (
        f"REFUTED.md and the seed disagree; only in the file: {sorted(documented - seeded)}, "
        f"only in the seed: {sorted(seeded - documented)}"
    )


def test_a_question_already_settled_is_blocked_by_the_novelty_gate(tmp_path: Path) -> None:
    """The whole point, end to end.

    Momentum sorting ETFs is REFUTED #22 at t=0.05. Before the seed existed, asking it again
    passed the gate -- because memory had nothing to recall -- and would have spent a trial to
    re-learn a stored result, raising the luck bar for every hypothesis after it.
    """
    database = memory(tmp_path)
    try:
        with database.transaction() as session:
            store = ResearchMemory(session)
            assert load_prior_findings(store) == len(PRIOR_FINDINGS)

            result = novelty_stage(store)(
                task("whether the momentum ranking carries information at all", "momentum")
            )

        assert result.next_state is TaskState.BLOCKED
        assert "refuted-022" in result.reason or "refuted-022" in str(result.detail)
    finally:
        database.close()


def test_a_genuinely_new_question_is_not_blocked(tmp_path: Path) -> None:
    """A gate that blocks everything is one somebody removes.

    A false match silently retires a live idea and nobody looks at it again, which is worse than
    the trial a near-duplicate costs.
    """
    database = memory(tmp_path)
    try:
        with database.transaction() as session:
            store = ResearchMemory(session)
            load_prior_findings(store)

            result = novelty_stage(store)(
                task("do municipal bond auction schedules predict short-rate moves?", "rates")
            )

        assert result.next_state is not TaskState.BLOCKED
    finally:
        database.close()


def test_loading_twice_records_nothing_new(tmp_path: Path) -> None:
    """Seeding is idempotent, so a scheduled loop calling it every cycle is harmless.

    The first version of this test pinned its own clock while the CLI passed `datetime.now`, so
    every real re-seed differed from the stored record and the second `research-cycle` of the day
    crashed. The records now carry a fixed `SEEDED_AT`, which makes the repeat byte-identical --
    and `test_the_seed_is_identical_on_every_call` pins that rather than this one.
    """
    database = memory(tmp_path)
    try:
        with database.transaction() as session:
            store = ResearchMemory(session)
            first = load_prior_findings(store)
            second = load_prior_findings(store)

        assert first == len(PRIOR_FINDINGS)
        assert second == 0
    finally:
        database.close()


def test_a_conflicting_edit_is_refused_rather_than_overwriting(tmp_path: Path) -> None:
    """A stored finding is what a decision was based on.

    Silently overwriting it would rewrite the reason an idea was retired, with no trace that the
    justification changed.
    """
    database = memory(tmp_path)
    try:
        with database.transaction() as session:
            store = ResearchMemory(session)
            load_prior_findings(store)
            altered = prior_records()[0].model_copy(update={"statement": "actually it worked fine"})

            with pytest.raises(Exception, match="already exists with different content"):
                store.record(altered)
    finally:
        database.close()


def test_every_seeded_finding_carries_a_verdict_the_gate_recognises() -> None:
    """A finding whose verdict the novelty gate does not treat as settled blocks nothing.

    `SETTLED` is REFUTED and LITERATURE_REFUTED. UNECONOMIC and IMPOSSIBLE are deliberately not
    in it -- an effect that could not pay its costs at one cost level is a different claim from
    one that does not exist -- so those entries are recorded without being expected to block.
    """
    recognised = 0
    for record in prior_records():
        assert record.kind is RecordKind.FINDING
        assert record.verdict is not None
        if record.verdict in SETTLED:
            recognised += 1

    assert recognised >= 20, "most of the settled findings should actually settle something"
    assert {r.verdict for r in prior_records()} <= set(Verdict)


def test_the_seed_is_identical_on_every_call() -> None:
    """Two calls must produce the same records, or re-seeding conflicts with itself.

    A wall-clock `recorded_at` broke this in a way no fixed-clock test could see: the store
    refuses a conflicting write, correctly, and the crash only appeared on the second CLI run.
    """
    assert prior_records() == prior_records()
