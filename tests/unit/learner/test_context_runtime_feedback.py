"""Retained market outcomes never manufacture semantic reading corrections."""

from datetime import timedelta
from decimal import Decimal

import pytest

from quantbot.learner.context_integration import journal_invalidation
from quantbot.learner.five_minute import FEATURE_NAMES
from quantbot.paper_core.context_integration import capture_research_context
from quantbot.paper_core.contracts import Decision, Observation, RoundTrip
from tests.unit.learner.test_context_integration import news, stores
from tests.unit.paper_core.test_external_context import NOW, permissions


def evidence(tmp_path, *, action="BUY", complete=False):
    research, experiments = stores(tmp_path)
    doc = news(research)
    with research.connection() as db:
        research._set(db, "context:source-permissions", [permissions().model_dump(mode="json")])
    snapshot = capture_research_context(
        research, symbol="AAPL", at=NOW, permissions={"alpaca-news": permissions()}
    )
    at = NOW + timedelta(days=1)
    observation = Observation(
        symbol="AAPL",
        source_start=at - timedelta(minutes=5),
        source_end=at,
        available_at=at,
        recorded_at_utc=at,
        price=100,
        raw_data_digest="synthetic",
        features=tuple((name, 0.01) for name in FEATURE_NAMES),
        dependencies=(),
        dependency_available_at=(),
        sequence=1,
        session_close=at + timedelta(hours=6),
        context_snapshot_id=snapshot.snapshot_id,
    )
    decision = Decision(
        recorded_at_utc=at,
        observation_id=observation.id,
        policy_id="policy",
        model_id="model",
        learning_receipt_id="receipt",
        config_id="config",
        inventory_id="inventory",
        inventory_generation=1,
        symbol="AAPL",
        action=action,
        gates=(),
        quantity=1,
        price=100,
        valid_until=at + timedelta(seconds=30),
        session_close=at + timedelta(hours=6),
        context_snapshot_id=snapshot.snapshot_id,
    )
    outcome_at = at + timedelta(minutes=31)
    trip = RoundTrip(
        recorded_at_utc=outcome_at,
        lot_id="lot",
        decision_id=decision.id,
        entry_fill_ids=("entry",),
        exit_fill_ids=("exit",),
        terminal_order_ids=("terminal",),
        protection_id="protection",
        reconciliation_id="proof",
        open_quantity=0,
        gross_pnl=-1,
        entry_notional=100,
        source_state_hash="state",
        source_start=at,
        source_end=at + timedelta(minutes=30),
        available_at=outcome_at,
        status="COMPLETE" if complete else "COSTS_PENDING",
        cost_reason="PROVEN_ACTUAL_FEES" if complete else "FEE_UNAVAILABLE",
        net_pnl=Decimal("-1.1") if complete else None,
        actual_fees=Decimal("0.1") if complete else None,
        cost_item_ids=("actual-fee",) if complete else (),
    )
    return research, experiments, doc, observation, decision, trip


def run(rows, **updates):
    from quantbot.learner.context_runtime_feedback import journal_runtime_context_feedback

    research, experiments, _, observation, decision, trip = rows
    args = dict(
        experiment_id="synthetic-context",
        core_store_id="synthetic-core",
        decisions=(decision,),
        observations=(observation,),
        round_trips=(trip,),
        outcome_objections={trip.id: ()},
        as_of=trip.available_at,
    )
    return journal_runtime_context_feedback(experiments, research, **(args | updates))


def test_losing_trade_links_original_context_without_semantic_label_or_unknown_net(tmp_path):
    from quantbot.learner.context_runtime_feedback import (
        ContextOutcomeFeedback,
        load_runtime_context_feedback,
    )

    rows = evidence(tmp_path)
    result = run(rows)
    assert result.feedback_written == 1 and result.actual_net_eligible == 0
    assert ("ACTUAL_NET_UNAVAILABLE", 1) in result.gate_counts
    events = rows[1].read_as_of("synthetic-context", rows[-1].available_at)
    assert all(event.kind != "ReadingReview" for event in events)
    feedback = next(
        e.decode(ContextOutcomeFeedback) for e in events if e.kind == "ContextOutcomeFeedback"
    )
    assert feedback.gross_return_bps == -100 and feedback.actual_net_return_bps is None
    assert feedback.context_use == "OBSERVED_ONLY"
    assert feedback.decision_at == NOW + timedelta(days=1)
    assert feedback.context_at == NOW
    assert rows[2] in feedback.lineage_refs
    repeated = run(rows, as_of=rows[-1].available_at + timedelta(days=1))
    assert repeated.feedback_written == repeated.snapshots_written == 0
    retained, excluded = load_runtime_context_feedback(
        rows[1], rows[0], experiment_id="synthetic-context", as_of=rows[-1].available_at
    )
    assert retained == (feedback,) and not excluded


def test_wait_and_unverified_reconciliation_never_become_outcome_examples(tmp_path):
    rows = evidence(tmp_path, action="WAIT")
    report = run(rows)
    assert report.snapshots_written == 1 and report.feedback_written == 0
    assert ("WAIT_NO_EXECUTED_OUTCOME", 1) in report.gate_counts
    report = run(rows, decisions=())
    assert report.feedback_written == 0


def test_closed_cost_complete_outcome_requires_explicit_current_eligibility(tmp_path):
    rows = evidence(tmp_path, complete=True)
    result = run(rows, outcome_objections={})
    assert result.feedback_written == 0
    assert ("OUTCOME_ELIGIBILITY_UNVERIFIED", 1) in result.gate_counts
    result = run(rows)
    assert result.actual_net_eligible == result.feedback_written == 1


def test_future_labels_and_postdecision_context_do_not_enter_journal(tmp_path):
    rows = evidence(tmp_path)
    result = run(rows, as_of=rows[-1].available_at - timedelta(seconds=1))
    assert result.feedback_written == 0
    assert ("OUTCOME_UNAVAILABLE_AT_CUTOFF", 1) in result.gate_counts
    observation = rows[3].model_copy(
        update={"available_at": rows[4].recorded_at_utc + timedelta(seconds=1)}
    )
    decision = rows[4].model_copy(update={"observation_id": observation.id})
    result = run(rows, observations=(observation,), decisions=(decision,), round_trips=())
    assert ("CONTEXT_OR_PRICE_UNAVAILABLE_AT_DECISION", 1) in result.gate_counts


def test_redaction_invalidates_retained_feedback_without_deleting_decision_reference(tmp_path):
    from quantbot.learner.context_runtime_feedback import load_runtime_context_feedback

    rows = evidence(tmp_path, complete=True)
    assert run(rows).feedback_written == 1
    at = rows[-1].available_at + timedelta(seconds=1)
    journal_invalidation(
        rows[1], experiment_id="synthetic-context", reference_id=rows[2], at=at, reason="REDACTED"
    )
    retained, exclusions = load_runtime_context_feedback(
        rows[1], rows[0], experiment_id="synthetic-context", as_of=at
    )
    assert not retained and exclusions[0][1] == "SOURCE_INVALIDATED"
    result = run(rows, as_of=at)
    assert result.feedback_written == 0
    assert ("SOURCE_INVALIDATED", 1) in result.gate_counts


def test_modeled_costs_remain_separate_from_unknown_actual_net(tmp_path):
    from quantbot.learner.context_runtime_feedback import load_runtime_context_feedback

    rows = evidence(tmp_path)
    trip = rows[-1].model_copy(
        update={
            "modeled_costs": Decimal("0.2"),
            "modeled_net_pnl": Decimal("-1.2"),
            "cost_item_ids": ("modeled-cost-evidence",),
        }
    )
    assert run(rows, round_trips=(trip,), outcome_objections={trip.id: ()}).feedback_written == 1
    (feedback,), _ = load_runtime_context_feedback(
        rows[1], rows[0], experiment_id="synthetic-context", as_of=trip.available_at
    )
    assert feedback.actual_net_return_bps is None
    assert feedback.modeled_net_return_bps == -120


def test_explicit_independent_review_import_is_idempotent_and_never_infers_truth(tmp_path):
    import pytest

    from quantbot.learner.context_runtime_feedback import journal_reading_review
    from quantbot.paper_core.context_integration import load_context_capture
    from tests.unit.learner.test_context_feedback import review

    rows = evidence(tmp_path, action="WAIT")
    run(rows)
    snapshot = load_context_capture(rows[0], rows[4].context_snapshot_id).snapshot
    correction = review(snapshot.readings[0])
    assert journal_reading_review(
        rows[1], rows[0], correction, snapshot_id=snapshot.snapshot_id, as_of=rows[-1].available_at
    )
    assert not journal_reading_review(
        rows[1], rows[0], correction, snapshot_id=snapshot.snapshot_id, as_of=rows[-1].available_at
    )
    with pytest.raises(ValueError, match="exact|EXACT"):
        journal_reading_review(
            rows[1],
            rows[0],
            correction.model_copy(update={"reading_id": "f" * 64}),
            snapshot_id=snapshot.snapshot_id,
            as_of=rows[-1].available_at,
        )
    assert not any(
        e.kind == "ReadingReview"
        for e in rows[1].read_as_of(
            "synthetic-context", rows[-1].available_at - timedelta(seconds=1)
        )
    )


def test_cost_revision_supersedes_old_feedback_without_rewriting_it(tmp_path):
    from quantbot.learner.context_runtime_feedback import (
        ContextModeledOutcomeLabel,
        ModeledCostContract,
        load_runtime_context_feedback,
    )
    from quantbot.research.manifest import content_id

    rows = evidence(tmp_path)
    contract = ModeledCostContract(recorded_at=NOW, source_policy_refs=("registered-policy",))
    assert run(rows, modeled_cost_contract=contract).feedback_written == 1
    old_label = next(
        e.decode(ContextModeledOutcomeLabel)
        for e in rows[1].read_as_of("synthetic-context", rows[-1].available_at)
        if e.kind == "ContextModeledOutcomeLabel"
    )
    at = rows[-1].available_at + timedelta(minutes=1)
    trip = rows[-1].model_copy(
        update={
            "revision": 2,
            "supersedes": rows[-1].id,
            "recorded_at_utc": at,
            "available_at": at,
            "source_state_hash": "new-state",
            "status": "COMPLETE",
            "actual_fees": Decimal("0.1"),
            "net_pnl": Decimal("-1.1"),
            "cost_item_ids": ("actual-fee",),
            "cost_reason": "PROVEN_ACTUAL_FEES",
        }
    )
    result = run(rows, round_trips=(trip,), outcome_objections={trip.id: ()}, as_of=at)
    assert result.feedback_written == result.actual_net_eligible == 1
    retained, excluded = load_runtime_context_feedback(
        rows[1], rows[0], experiment_id="synthetic-context", as_of=at
    )
    assert len(retained) == 1 and retained[0].outcome_id == trip.id
    from quantbot.learner.context_integration import ContextInvalidationRecord

    invalidations = [
        e.decode(ContextInvalidationRecord).invalidation
        for e in rows[1].read_as_of("synthetic-context", at)
        if e.kind == "ContextInvalidationRecord"
    ]
    assert any(
        row.reference_id == rows[-1].id and row.reason == "EVIDENCE_SUPERSEDED"
        for row in invalidations
    )
    assert {content_id(old_label), old_label.feedback_id} <= {
        row.reference_id for row in invalidations
    }
    assert excluded[0][1] == "SOURCE_INVALIDATED"


@pytest.mark.parametrize(
    "reason",
    [
        "CONTEXT_OUTCOME_CORRECTION_PENDING",
        "CONTEXT_OUTCOME_QUARANTINED",
        "CONTEXT_OUTCOME_NOT_CURRENT",
    ],
)
def test_known_objection_suspends_retained_label_and_old_revision_cannot_release(tmp_path, reason):
    from quantbot.learner.context_runtime_feedback import load_runtime_context_feedback

    rows = evidence(tmp_path, complete=True)
    assert run(rows).feedback_written == 1
    later = rows[-1].available_at + timedelta(minutes=1)
    revised = rows[-1].model_copy(
        update={
            "revision": 2,
            "supersedes": rows[-1].id,
            "available_at": later,
            "recorded_at_utc": later,
            "source_state_hash": "pending-correction",
        }
    )
    run(rows, round_trips=(revised,), outcome_objections={revised.id: (reason,)}, as_of=later)
    retained, excluded = load_runtime_context_feedback(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        as_of=later,
    )
    assert not retained and any(value == "OUTCOME_ELIGIBILITY_SUSPENDED" for _, value in excluded)
    run(rows, as_of=later + timedelta(seconds=1))
    retained, _ = load_runtime_context_feedback(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        as_of=later + timedelta(seconds=1),
    )
    assert not retained
    run(
        rows,
        round_trips=(revised,),
        outcome_objections={revised.id: ()},
        as_of=later + timedelta(seconds=2),
    )
    retained, _ = load_runtime_context_feedback(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        as_of=later + timedelta(seconds=2),
    )
    assert len(retained) == 1 and retained[0].outcome_id == revised.id


@pytest.mark.asyncio
async def test_selected_retained_label_rechecks_current_work_without_new_outcome_revision(tmp_path):
    from sqlalchemy import update

    from quantbot.learner.context_runtime_feedback import (
        ContextOutcomeFeedback,
        load_runtime_context_feedback,
        revalidate_retained_outcomes,
    )
    from quantbot.paper_core.guardian import guardian_cycle
    from quantbot.paper_core.outcomes import process_outcomes
    from quantbot.paper_core.store import table
    from quantbot.paper_core.trader import trader_cycle
    from tests.fixtures.core_m2 import prepare

    context_dir = tmp_path / "context"
    context_dir.mkdir()
    rows = evidence(context_dir, complete=True)
    run(rows)
    events = rows[1].read_as_of("synthetic-context", rows[-1].available_at)
    source = next(
        e.decode(ContextOutcomeFeedback) for e in events if e.kind == "ContextOutcomeFeedback"
    )
    core_dir = tmp_path / "core"
    core_dir.mkdir()
    db, store, broker, proof, observation, model = prepare(core_dir)
    try:
        decision = await trader_cycle(store, broker, observation, model, proof, now=broker.now)
        broker.now += timedelta(minutes=30)
        await guardian_cycle(store, broker, now=broker.now)
        process_outcomes(store, cutoff=broker.now)
        trip = RoundTrip.model_validate(store.documents("core_outcome_revisions")[0])
        current = source.model_copy(
            update={
                "core_store_id": db.store_identity,
                "decision_id": decision.id,
                "lot_id": trip.lot_id,
                "outcome_id": trip.id,
                "outcome_revision": trip.revision,
                "source_state_hash": trip.source_state_hash,
            }
        )
        rows[1].append(current, idempotency_key="canonical-outcome-fixture")
        at = rows[-1].available_at + timedelta(minutes=1)
        kwargs = dict(
            experiment_id="synthetic-context", feedback_ids={current.feedback_id}, as_of=at
        )
        assert not revalidate_retained_outcomes(store, rows[1], **kwargs)
        with db.transaction() as session:
            work = table("core_outcome_work")
            session.execute(
                update(work).where(work.c.lot_id == trip.lot_id).values(version=work.c.version + 1)
            )
        blocked = revalidate_retained_outcomes(store, rows[1], **kwargs)
        assert blocked == ((current.feedback_id, ("CONTEXT_OUTCOME_CORRECTION_PENDING",)),)
        retained, _ = load_runtime_context_feedback(
            rows[1], rows[0], experiment_id="synthetic-context", as_of=at
        )
        assert current.feedback_id not in {row.feedback_id for row in retained}
        with db.transaction() as session:
            session.execute(
                update(work)
                .where(work.c.lot_id == trip.lot_id)
                .values(processed_version=work.c.version)
            )
        assert not revalidate_retained_outcomes(
            store, rows[1], **(kwargs | {"as_of": at + timedelta(seconds=1)})
        )
        retained, _ = load_runtime_context_feedback(
            rows[1], rows[0], experiment_id="synthetic-context", as_of=at + timedelta(seconds=1)
        )
        assert current.feedback_id in {row.feedback_id for row in retained}
        # Exercise 200 deep validations on the same real synthetic owned lot, under
        # the existing send-style lock. This measures the full loop, not production scale.
        from time import perf_counter

        benchmark_ids = {current.feedback_id}
        for index in range(199):
            sample = current.model_copy(
                update={"recorded_at": current.recorded_at + timedelta(microseconds=index + 1)}
            )
            rows[1].append(sample, idempotency_key=f"synthetic-validation-benchmark:{index}")
            benchmark_ids.add(sample.feedback_id)
        with db.transaction() as session:
            from sqlalchemy import text

            session.execute(text("BEGIN IMMEDIATE"))
            started = perf_counter()
            assert not revalidate_retained_outcomes(
                store,
                rows[1],
                experiment_id="synthetic-context",
                feedback_ids=benchmark_ids,
                as_of=at + timedelta(seconds=2),
                session=session,
            )
            print(f"SYNTHETIC_200_DEEP_OUTCOME_CHECKS_SECONDS={perf_counter() - started:.3f}")
    finally:
        db.close()


@pytest.mark.parametrize("learned", [False, True])
def test_prospective_shadow_consumption_can_pair_outcome_without_rewriting_observed_context(
    tmp_path,
    learned,
):
    import pytest

    from quantbot.learner.context_runtime_feedback import (
        ContextShadowConsumption,
        retain_shadow_consumption,
        runtime_downstream_examples,
    )
    from quantbot.paper_core.context_integration import (
        load_context_capture,
        retained_context_documents,
    )
    from quantbot.paper_core.external_context import build_context_snapshot

    rows = evidence(tmp_path, complete=True)
    original = load_context_capture(rows[0], rows[3].context_snapshot_id)
    at = rows[4].recorded_at_utc
    shadow = build_context_snapshot(
        retained_context_documents(rows[0], original),
        symbol="AAPL",
        at=at,
        coverage=original.snapshot.coverage,
        used_document_ids=original.snapshot.eligible_document_ids,
    )
    receipt = ContextShadowConsumption(
        observation_id=rows[3].id,
        original_snapshot_id=original.snapshot.snapshot_id,
        consumed_at=at,
        recorded_at=at,
        shadow=shadow,
        **(
            {
                "candidate_id": "validated-synthetic-head",
                "reading_predictions": ((shadow.readings[0].reading_id, -0.25),),
                "candidate_feature_values": (1.0, 0.0, 1.0, -0.25, 0.0, shadow.features[-1][1]),
            }
            if learned
            else {}
        ),
    )
    with pytest.raises(ValueError, match="PROSPECTIVE"):
        retain_shadow_consumption(rows[0], receipt, now=at + timedelta(seconds=1))
    if learned:
        with pytest.raises(ValueError, match="VALIDATED_CANDIDATE"):
            retain_shadow_consumption(rows[0], receipt, now=at)
    callback = (lambda value: value.candidate_feature_values[3] == -0.25) if learned else None
    assert retain_shadow_consumption(rows[0], receipt, now=at, candidate_validator=callback)
    assert not retain_shadow_consumption(rows[0], receipt, now=at, candidate_validator=callback)
    run(rows)
    examples, exclusions = runtime_downstream_examples(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        observations=(rows[3],),
        as_of=rows[-1].available_at,
        cost_basis="ACTUAL_NET",
    )
    assert not exclusions and len(examples) == 1
    assert examples[0].context.used_document_ids == (rows[2],)
    assert examples[0].net_return_bps == -110
    if learned:
        assert examples[0].interpreter_id == "validated-synthetic-head"
        assert examples[0].consumed_context_features[3] == -0.25
        assert examples[0].context.features[3][1] == 1
    assert load_context_capture(rows[0], original.snapshot.snapshot_id) == original
    assert not original.snapshot.used_document_ids


def test_deleted_source_excludes_retained_feedback_without_fabricated_redaction_record(tmp_path):
    from quantbot.learner.context_runtime_feedback import load_runtime_context_feedback

    rows = evidence(tmp_path)
    assert run(rows).feedback_written == 1
    with rows[0].connection() as db:
        db.execute("delete from research_documents where id=?", (rows[2],))
    retained, excluded = load_runtime_context_feedback(
        rows[1], rows[0], experiment_id="synthetic-context", as_of=rows[-1].available_at
    )
    assert not retained and excluded[0][1] == "SOURCE_DOCUMENT_UNAVAILABLE"


def test_declared_modeled_cost_label_never_changes_unknown_actual_fees(tmp_path):
    from quantbot.learner.context_runtime_feedback import (
        ContextModeledOutcomeLabel,
        ModeledCostContract,
    )

    rows = evidence(tmp_path)
    contract = ModeledCostContract(
        recorded_at=NOW, source_policy_refs=("registered-manifest", "crypto-policy")
    )
    result = run(rows, modeled_cost_contract=contract)
    assert result.feedback_written == 1 and result.modeled_labels_written == 1
    assert result.actual_net_eligible == 0
    events = rows[1].read_as_of("synthetic-context", rows[-1].available_at)
    label = next(
        e.decode(ContextModeledOutcomeLabel)
        for e in events
        if e.kind == "ContextModeledOutcomeLabel"
    )
    assert label.contract.contract_id == contract.contract_id
    assert label.net_return_bps == -110 and label.round_trip_cost_bps == 10
    assert run(rows, modeled_cost_contract=contract).modeled_labels_written == 0


def test_downstream_decision_selection_excludes_unselected_missing_prices(tmp_path):
    from quantbot.learner.context_runtime_feedback import runtime_downstream_examples

    rows = evidence(tmp_path, complete=True)
    run(rows)
    before = rows[1].read_as_of("synthetic-context", rows[-1].available_at)
    examples, excluded = runtime_downstream_examples(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        observations=(),
        as_of=rows[-1].available_at,
        cost_basis="ACTUAL_NET",
        decision_ids=set(),
    )
    assert examples == excluded == ()
    _, selected_exclusions = runtime_downstream_examples(
        rows[1],
        rows[0],
        experiment_id="synthetic-context",
        observations=(),
        as_of=rows[-1].available_at,
        cost_basis="ACTUAL_NET",
        decision_ids={rows[4].id},
    )
    assert selected_exclusions[0][1] == "SHADOW_PRICE_OBSERVATION_UNAVAILABLE"
    with pytest.raises(ValueError, match="BOUNDED"):
        runtime_downstream_examples(
            rows[1],
            rows[0],
            experiment_id="synthetic-context",
            observations=(),
            as_of=rows[-1].available_at,
            cost_basis="ACTUAL_NET",
            decision_ids={str(i) for i in range(201)},
        )
    assert rows[1].read_as_of("synthetic-context", rows[-1].available_at) == before
