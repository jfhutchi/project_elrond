"""Independent reading corrections, not trade P&L, train interpreter candidates."""

from datetime import timedelta

import pytest

from tests.unit.paper_core.test_external_context import NOW, document, permissions


def rows(count=16):
    from quantbot.paper_core.external_context import build_context_snapshot

    result = []
    for index in range(count):
        at = NOW + timedelta(days=index)
        positive = index % 2 == 0
        item = document(
            "AAPL beats earnings" if positive else "AAPL misses earnings",
            identity=str(index),
            published_at=(at - timedelta(minutes=5)).isoformat(),
            updated_at=(at - timedelta(minutes=5)).isoformat(),
            retrieved_at=(at - timedelta(minutes=4)).isoformat(),
        )
        snapshot = build_context_snapshot(
            (item,), symbol="AAPL", at=at, used_document_ids=(item.document_id,)
        )
        result.append((item, snapshot))
    return result


def review(reading, *, verdict="CORRECTED", polarity=0.0, at=None, outcome=-10.0):
    from quantbot.learner.context_feedback import ReadingReview, ReviewEvidence

    at = at or reading.interpreted_at + timedelta(hours=1)
    evidence = ReviewEvidence(
        reference_id="review:" + reading.reading_id,
        content_hash="b" * 64,
        available_at=at,
        kind="HUMAN_ANNOTATION",
    )
    return ReadingReview(
        experiment_id="synthetic-context",
        reading_id=reading.reading_id,
        reviewed_at=at,
        recorded_at=at,
        verdict=verdict,
        label_authority="HUMAN_REVIEW",
        evidence=(evidence,),
        corrected_polarity=polarity if verdict == "CORRECTED" else None,
        outcome_net_bps=outcome,
    )


def test_correct_reading_with_losing_trade_retains_positive_label():
    from quantbot.learner.context_feedback import build_examples

    item, snapshot = rows(1)[0]
    label = review(snapshot.readings[0], verdict="CONFIRMED", outcome=-50)
    examples, exclusions = build_examples(
        (snapshot,), (item,), (label,), as_of=NOW + timedelta(days=1)
    )
    assert len(examples) == 1 and not exclusions
    assert examples[0].target_polarity == 1


def test_outcome_only_and_inconclusive_reviews_cannot_train_readings():
    from quantbot.learner.context_feedback import ReadingReview, build_examples

    item, snapshot = rows(1)[0]
    label = ReadingReview(
        experiment_id="synthetic-context",
        reading_id=snapshot.readings[0].reading_id,
        reviewed_at=NOW + timedelta(hours=1),
        recorded_at=NOW + timedelta(hours=1),
        verdict="INCONCLUSIVE",
        label_authority="OUTCOME_ONLY",
        evidence=(),
        outcome_net_bps=-100,
    )
    examples, exclusions = build_examples(
        (snapshot,), (item,), (label,), as_of=NOW + timedelta(days=1)
    )
    assert not examples and exclusions


def test_review_cannot_use_evidence_from_its_future():
    from quantbot.learner.context_feedback import ReadingReview, ReviewEvidence

    with pytest.raises(ValueError, match="available"):
        ReadingReview(
            experiment_id="synthetic-context",
            reading_id="a" * 64,
            reviewed_at=NOW,
            recorded_at=NOW,
            verdict="CORRECTED",
            label_authority="SOURCE_CORRECTION",
            corrected_polarity=-1,
            evidence=(
                ReviewEvidence(
                    reference_id="correction",
                    content_hash="b" * 64,
                    available_at=NOW + timedelta(seconds=1),
                    kind="SOURCE_CORRECTION",
                ),
            ),
        )


def test_unknown_training_rights_exclude_an_otherwise_corrected_reading():
    from quantbot.learner.context_feedback import build_examples

    item, snapshot = rows(1)[0]
    item = item.model_copy(update={"permissions": permissions(training="UNKNOWN")})
    examples, exclusions = build_examples(
        (snapshot,), (item,), (review(snapshot.readings[0]),), as_of=NOW + timedelta(days=1)
    )
    assert not examples
    assert any(reason == "TRAINING_PERMISSION_UNVERIFIED" for _, reason in exclusions)


def test_corrected_examples_reach_real_fit_and_heldout_without_validation_leakage():
    from quantbot.learner.context_feedback import build_examples, fit_interpreter_candidate

    source = rows()
    reviews = tuple(review(snapshot.readings[0], polarity=0) for _, snapshot in source)
    examples, exclusions = build_examples(
        tuple(s for _, s in source),
        tuple(d for d, _ in source),
        reviews,
        as_of=NOW + timedelta(days=20),
    )
    candidate = fit_interpreter_candidate(
        examples, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
    )
    assert not exclusions
    assert candidate.heldout_mse < candidate.baseline_heldout_mse
    assert candidate.heldout_mse == pytest.approx(0)
    assert len(candidate.training_reading_ids) == 10
    assert len(candidate.heldout_reading_ids) == 6
    assert not set(candidate.training_reading_ids) & set(candidate.heldout_reading_ids)
    assert candidate.grants_activation is False
    changed = tuple(
        row.model_copy(update={"target_polarity": 1.0})
        if row.decision_at >= NOW + timedelta(days=10)
        else row
        for row in examples
    )
    other = fit_interpreter_candidate(
        changed, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
    )
    assert other.estimator == candidate.estimator
    assert other.heldout_mse != candidate.heldout_mse


def test_late_corrections_cannot_enter_training():
    from quantbot.learner.context_feedback import build_examples, fit_interpreter_candidate

    source = rows()
    reviews = tuple(review(s.readings[0], at=NOW + timedelta(days=19)) for _, s in source)
    examples, _ = build_examples(
        tuple(s for _, s in source),
        tuple(d for d, _ in source),
        reviews,
        as_of=NOW + timedelta(days=20),
    )
    with pytest.raises(ValueError, match="training"):
        fit_interpreter_candidate(
            examples, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
        )


def test_source_redaction_invalidates_examples_and_already_fitted_candidate():
    from quantbot.learner.context_feedback import (
        SourceInvalidation,
        build_examples,
        candidate_usable,
        fit_interpreter_candidate,
    )

    source = rows()
    reviews = tuple(review(s.readings[0]) for _, s in source)
    snapshots, documents = tuple(s for _, s in source), tuple(d for d, _ in source)
    examples, _ = build_examples(snapshots, documents, reviews, as_of=NOW + timedelta(days=20))
    candidate = fit_interpreter_candidate(
        examples, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
    )
    tombstone = SourceInvalidation(
        reference_id=documents[0].document_id,
        invalidated_at=NOW + timedelta(days=21),
        reason="REDACTED",
    )
    remaining, exclusions = build_examples(
        snapshots, documents, reviews, as_of=NOW + timedelta(days=22), invalidations=(tombstone,)
    )
    assert len(remaining) == len(examples) - 1
    assert any(reason == "SOURCE_INVALIDATED" for _, reason in exclusions)
    assert not candidate_usable(candidate, (tombstone,), at=NOW + timedelta(days=22))
    assert snapshots[0].used_document_ids == (documents[0].document_id,)


def test_conflicting_same_time_independent_labels_are_not_arbitrarily_selected():
    from quantbot.learner.context_feedback import build_examples

    item, snapshot = rows(1)[0]
    labels = (review(snapshot.readings[0], polarity=1), review(snapshot.readings[0], polarity=-1))
    examples, exclusions = build_examples(
        (snapshot,), (item,), labels, as_of=NOW + timedelta(days=1)
    )
    assert not examples
    assert any(reason == "CONFLICTING_INDEPENDENT_REVIEWS" for _, reason in exclusions)


def test_candidate_predicts_future_reading_and_refuses_retrospective_use():
    from quantbot.learner.context_feedback import (
        build_examples,
        candidate_polarity,
        fit_interpreter_candidate,
    )

    source = rows(22)
    examples, _ = build_examples(
        tuple(s for _, s in source[:16]),
        tuple(d for d, _ in source[:16]),
        tuple(review(s.readings[0]) for _, s in source[:16]),
        as_of=NOW + timedelta(days=20),
    )
    candidate = fit_interpreter_candidate(
        examples, split_at=NOW + timedelta(days=10), as_of=NOW + timedelta(days=20)
    )
    assert candidate_polarity(
        candidate, source[21][1].readings[0], at=NOW + timedelta(days=21)
    ) == pytest.approx(0)
    with pytest.raises(ValueError):
        candidate_polarity(candidate, source[0][1].readings[0], at=NOW + timedelta(days=21))


def test_downstream_comparison_uses_same_cost_aware_heldout_labels_for_both_arms():
    from quantbot.learner.context_feedback import DownstreamExample, compare_context_to_price_only

    source = rows()
    observations = tuple(
        DownstreamExample(
            decision_id=str(index),
            decision_at=snapshot.at,
            context=snapshot,
            price_features=(1.0,),
            price_available_at=snapshot.at,
            label_available_at=snapshot.at + timedelta(hours=1),
            net_return_bps=10 * snapshot.readings[0].polarity,
            cost_basis="MODELED_NET",
            cost_evidence_id="synthetic-two-sided-cost-model",
        )
        for index, (_, snapshot) in enumerate(source)
    )
    result = compare_context_to_price_only(
        observations,
        tuple(d for d, _ in source),
        price_feature_names=("constant_price_control",),
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
    )
    assert result.price_plus_context_mse < result.price_only_mse
    assert result.heldout_decision_ids == tuple(str(index) for index in range(10, 16))
    assert result.grants_activation is False
    unavailable = source[0][0].model_copy(update={"first_seen_at": NOW + timedelta(hours=1)})
    with pytest.raises(ValueError, match="unavailable"):
        compare_context_to_price_only(
            observations,
            (unavailable, *(d for d, _ in source[1:])),
            price_feature_names=("constant_price_control",),
            split_at=NOW + timedelta(days=10),
            as_of=NOW + timedelta(days=20),
        )


def test_later_verified_training_permission_does_not_rewrite_original_reading_time():
    from quantbot.learner.context_feedback import build_examples

    item, snapshot = rows(1)[0]
    original = item.model_copy(update={"permissions": permissions(training="UNKNOWN")})
    later = permissions(checked_at=NOW + timedelta(hours=2))
    examples, exclusions = build_examples(
        (snapshot,),
        (original,),
        (review(snapshot.readings[0]),),
        as_of=NOW + timedelta(days=1),
        training_permissions={item.document_id: later},
    )
    assert len(examples) == 1 and not exclusions
    assert snapshot.readings[0].interpreted_at == NOW
    assert original.permissions.training == "UNKNOWN"


def test_reused_context_is_split_by_actual_trade_decision_not_first_capture():
    from quantbot.learner.context_feedback import DownstreamExample, compare_context_to_price_only

    source = rows()
    observations = []
    for index, (_, snapshot) in enumerate(source):
        decision_at = snapshot.at if index != 9 else NOW + timedelta(days=10, minutes=1)
        observations.append(
            DownstreamExample(
                decision_id=str(index),
                decision_at=decision_at,
                context=snapshot,
                price_features=(1.0,),
                price_available_at=decision_at,
                label_available_at=decision_at + timedelta(hours=1),
                net_return_bps=10 * snapshot.readings[0].polarity,
                cost_basis="MODELED_NET",
                cost_evidence_id="synthetic-costs",
            )
        )
    result = compare_context_to_price_only(
        observations,
        tuple(d for d, _ in source),
        price_feature_names=("constant_price_control",),
        split_at=NOW + timedelta(days=10),
        as_of=NOW + timedelta(days=20),
    )
    assert result.training_decision_ids == tuple(str(i) for i in range(9))
    assert set(result.heldout_decision_ids) == {str(i) for i in range(9, 16)}
