"""Unreviewed research volume must not prevent an honest missing-label verdict."""

from datetime import timedelta

from quantbot.learner.context_integration import ContextSnapshotRecord, evaluate_retained_context
from tests.unit.learner.test_context_integration import Charger, Guard, stores
from tests.unit.paper_core.test_external_context import NOW


def test_unreviewed_capture_backlog_does_not_open_sources_or_spend_fit_budget(tmp_path):
    research, experiments = stores(tmp_path)
    for i in range(220):
        experiments.append(
            ContextSnapshotRecord(
                experiment_id="synthetic-context",
                recorded_at=NOW,
                snapshot_id=f"{i:064x}",
            ),
            idempotency_key=f"unreviewed-{i}",
        )
    charger = Charger()
    result = evaluate_retained_context(
        experiments,
        research,
        experiment_id="synthetic-context",
        split_at=NOW - timedelta(days=1),
        as_of=NOW,
        guard=Guard(),
        charger=charger,
    )
    assert result.status == "INSUFFICIENT_EVIDENCE"
    assert result.reason == "NO_ELIGIBLE_INDEPENDENT_REVIEWS"
    assert result.eligible_examples == 0
    assert charger.calls == []
