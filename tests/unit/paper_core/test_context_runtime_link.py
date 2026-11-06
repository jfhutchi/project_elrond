"""The runtime attaches observed context without changing price strategy inputs."""

import pytest
from tests.fixtures.core_m2 import prepare
from tests.unit.learner.test_context_integration import news, stores
from tests.unit.paper_core.test_external_context import NOW, permissions


@pytest.mark.parametrize("permitted", [False, True])
def test_runtime_context_is_linked_only_when_available_without_empty_write(tmp_path, permitted):
    from quantbot.paper_core.context_integration import load_context_capture
    from quantbot.paper_core.roles import attach_observed_context

    research, _ = stores(tmp_path)
    news(research)
    if permitted:
        with research.connection() as session:
            research._set(
                session, "context:source-permissions", [permissions().model_dump(mode="json")]
            )
    db, _, _, _, original, _ = prepare(tmp_path)
    try:
        observation = original.model_copy(update={"symbol": "AAPL"})
        linked = attach_observed_context(research, observation, at=NOW)
        assert linked.features == observation.features
        if permitted:
            assert linked.context_snapshot_id
            snapshot = load_context_capture(research, linked.context_snapshot_id).snapshot
            assert snapshot.used_document_ids == ()
        else:
            assert linked == observation
            with research.connection() as session:
                assert session.execute("select count(*) from receipts").fetchone()[0] == 0
    finally:
        db.close()
