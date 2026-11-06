from datetime import timedelta

import pytest
from tests.fixtures.core_m4 import episodes

from quantbot.paper_core import learning
from quantbot.paper_core.contracts import LearningPolicy


@pytest.mark.asyncio
async def test_opening_context_excluded_before_intraday_training_materializes(
    tmp_path, monkeypatch
):
    db, store, broker, parent, *_ = await episodes(tmp_path, count=1)
    try:
        cutoff = broker.now + timedelta(hours=8)
        policy = LearningPolicy(parent_model=parent)
        with db.transaction() as session:
            regular, _, considered = learning._select(session, policy, cutoff, execution_now=cutoff)
            assert len(regular) == 1
        original = learning._revision_metadata

        def opening_rows(session, at):
            return [
                dict(row, feature_context="OPENING_CONTIGUOUS_V1") for row in original(session, at)
            ]

        monkeypatch.setattr(learning, "_revision_metadata", opening_rows)
        with db.transaction() as session:
            selected, excluded, observed = learning._select(
                session, policy, cutoff, execution_now=cutoff
            )
        assert not selected
        assert observed == considered
        assert excluded == [(considered[0], "OPENING_CONTEXT_REQUIRES_SEPARATE_VALIDATION")]
        assert store.documents("core_round_trips"), "Evaluation exclusion never deletes outcomes"
    finally:
        db.close()
