"""A new store starts from what this project already knows (#52, #190 Phase 3).

`REFUTED.md` holds settled findings from cycles 2-12, and `load_prior_findings` exists because
the live store was once empty while that file held twenty-four of them -- so the novelty gate
passed questions the project had already answered.

Its only caller was `research-cycle`, which fails closed without `QUANTBOT_MODEL_GENERATOR`. That
made a freshly created store report `BELOW_M0` -- "nothing shows this platform can reject a
hypothesis" -- while its refutation history sat checked into the repository, and made the reported
milestone depend on an LLM endpoint that has nothing to do with whether anything was ever refuted.
"""

from __future__ import annotations

import json
from datetime import date
from io import StringIO
from pathlib import Path

import pytest

from quantbot.cli import main
from quantbot.research.memory import RecordKind, ResearchMemory, Verdict
from quantbot.research.milestone import Milestone, assess_milestone
from quantbot.storage import Database


def test_a_created_store_reports_m0_without_any_credential_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The measurement, not the intention: the milestone before and after, on a real store.

    No broker key, no market-data key and no model endpoint is set anywhere in this test. That
    is the point -- whether the platform has ever refuted a hypothesis is a fact about its
    history, and it was being gated behind a service that has nothing to do with it.
    """
    target = tmp_path / "quantbot.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(target))
    for credential in (
        "ALPACA_PAPER_API_KEY",
        "ALPACA_PAPER_API_SECRET",
        "QUANTBOT_MODEL_ENDPOINT",
        "QUANTBOT_MODEL_GENERATOR",
        "QUANTBOT_MODEL_CRITIC",
    ):
        monkeypatch.delenv(credential, raising=False)

    output = StringIO()
    assert main(["create-database", "--confirm"], output=output) == 0
    payload = json.loads(output.getvalue())
    assert payload["created"] is True
    assert payload["prior_findings_seeded"] > 0, (
        "a new store that has forgotten REFUTED.md is the exact failure load_prior_findings "
        "was written for"
    )

    database = Database(target)
    try:
        with database.transaction() as session:
            refutations = [
                record
                for record in ResearchMemory(session).recall("", kinds=[RecordKind.FINDING])
                if record.verdict is Verdict.REFUTED
            ]
            assert len(refutations) >= 15, "REFUTED.md's settled findings did not land"

            report = assess_milestone(session, as_of=date(2026, 9, 2))
    finally:
        database.close()

    assert report.current is Milestone.M0_RESEARCH_PLATFORM_OPERATIONAL, report.explain()

    # And the M1 blocker is now the real one, stated plainly, rather than a model-endpoint error
    # standing in front of it.
    assert report.next_check is not None
    assert report.next_check.level is Milestone.M1_CANDIDATE_EDGE_DETECTED
    assert "RESEARCH_SURVIVOR" in report.next_check.explain()


def test_seeding_cannot_overwrite_a_record_a_decision_was_based_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Idempotent through `ResearchMemory.record`: an exact repeat is accepted, a conflict is not.

    Asserted rather than assumed, because this now runs on a path an operator takes once and
    then never thinks about again.
    """
    from quantbot.research.prior import load_prior_findings

    target = tmp_path / "quantbot.db"
    monkeypatch.setenv("QUANTBOT_DB_PATH", str(target))
    assert main(["create-database", "--confirm"], output=StringIO()) == 0

    database = Database(target)
    try:
        with database.transaction() as session:
            memory = ResearchMemory(session)
            before = len(memory.recall("", kinds=[RecordKind.FINDING]))
            assert load_prior_findings(memory) == 0, "a repeat seeded something new"
            assert len(memory.recall("", kinds=[RecordKind.FINDING])) == before
    finally:
        database.close()
