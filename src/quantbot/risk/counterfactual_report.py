"""Read-only, exploratory views of durable counterfactual evidence (#57).

No horizon inference, outcome evaluation, registration or policy authority lives here.
Missing rows are absence of evidence, not evidence that a horizon is still pending.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field

from quantbot.risk.counterfactual import CounterfactualOutcome, HypotheticalExecutionPolicy
from quantbot.risk.gate_value import GateOutcomeSample, GateVerdict, report_gate_value
from quantbot.risk.regime import MarketRegimeSnapshot
from quantbot.risk.shadow import CounterfactualState, RejectedCandidate, reasons_from_rows
from quantbot.storage import StorageRepository

NO_RECORDED_OUTCOME = "NO_RECORDED_OUTCOME"


@dataclass
class _Group:
    strategy_id: str
    configuration_hash: str
    gate_id: str
    gate_version: int
    rule_parameters_hash: str
    regime: dict[str, object] | None
    candidates: dict[str, tuple[RejectedCandidate, CounterfactualOutcome | None]] = field(
        default_factory=dict
    )
    observation_ids: set[str] = field(default_factory=set)
    snapshot_ids: set[str] = field(default_factory=set)
    regime_reasons: set[str] = field(default_factory=set)


def _regime(
    refusal: RejectedCandidate, snapshots: Mapping[str, Mapping[str, object]]
) -> tuple[dict[str, object], tuple[str, ...]]:
    identity = refusal.market_regime_snapshot_id
    if identity is None:
        return {"status": "NOT_RECORDED"}, ("no regime snapshot bound to refusal",)
    row = snapshots.get(identity)
    if row is None:
        return {"status": "SNAPSHOT_MISSING"}, ("bound regime snapshot is absent",)
    snapshot = MarketRegimeSnapshot.model_validate(
        {
            **{key: value for key, value in row.items() if key != "snapshot_id"},
            "unavailable": tuple(filter(None, str(row["unavailable"]).split(" | "))),
            "source_snapshot_ids": tuple(filter(None, str(row["source_snapshot_ids"]).split(","))),
        }
    )
    if snapshot.as_of_session > refusal.session_date:
        raise ValueError(f"future-dated regime snapshot bound to {refusal.observation_id}")
    return {
        "status": "MEASURED" if snapshot.fully_measured else "PARTIAL",
        "method_version": snapshot.method_version,
        "benchmark_symbol": snapshot.benchmark_symbol,
        "source": snapshot.source,
        "trend": snapshot.trend.value,
        "volatility": snapshot.volatility.value,
        "breadth": snapshot.breadth.value,
    }, snapshot.unavailable


def _answer(outcome: CounterfactualOutcome | None) -> tuple[object, ...]:
    if outcome is None:
        return (NO_RECORDED_OUTCOME,)
    return (
        outcome.state,
        outcome.horizon_session,
        outcome.net_excess_bps,
        outcome.gross_return_bps,
        outcome.benchmark_return_bps,
        outcome.unevaluable_reason,
    )


def _summarize(group: _Group, policy: str) -> dict[str, object]:
    counts = Counter({NO_RECORDED_OUTCOME: 0, **{state.value: 0 for state in CounterfactualState}})
    terminal_reasons: Counter[str] = Counter()
    unavailable_reasons: Counter[str] = Counter()
    samples: list[GateOutcomeSample] = []
    by_day: Counter[str] = Counter()
    for candidate, (refusal, outcome) in sorted(group.candidates.items()):
        by_day[refusal.session_date.isoformat()] += 1
        counts[NO_RECORDED_OUTCOME if outcome is None else outcome.state.value] += 1
        if outcome is None:
            continue
        if outcome.unevaluable_reason is not None:
            terminal_reasons[outcome.unevaluable_reason.value] += 1
        if outcome.state is CounterfactualState.UNAVAILABLE:
            unavailable_reasons[outcome.detail] += 1
        if outcome.state is CounterfactualState.EVALUATED:
            assert outcome.net_excess_bps is not None  # validated by CounterfactualOutcome
            samples.append(
                GateOutcomeSample(
                    observation_id=candidate,
                    gate_id=group.gate_id,
                    gate_version=group.gate_version,
                    rule_parameters_hash=group.rule_parameters_hash,
                    policy_identity=policy,
                    session_date=refusal.session_date,
                    net_excess_bps=outcome.net_excess_bps,
                )
            )
    value = report_gate_value(
        samples,
        gate_id=group.gate_id,
        gate_version=group.gate_version,
        rule_parameters_hash=group.rule_parameters_hash,
        policy_identity=policy,
    )
    return {
        "strategy_id": group.strategy_id,
        "configuration_hash": group.configuration_hash,
        "distinct_candidates": len(group.candidates),
        "refusal_observations": len(group.observation_ids),
        "rejected_candidates_by_day": dict(sorted(by_day.items())),
        "outcome_counts": dict(counts),
        "unevaluable_reasons": dict(sorted(terminal_reasons.items())),
        "unavailable_reasons": dict(sorted(unavailable_reasons.items())),
        "observation_ids": sorted(group.observation_ids),
        "regime": group.regime,
        "regime_snapshot_ids": sorted(group.snapshot_ids),
        "regime_unavailable_reasons": sorted(group.regime_reasons),
        "gate_value": value.model_dump(mode="json"),
    }


def counterfactual_report(
    repository: StorageRepository,
    *,
    policy_identity: str | None = None,
    by_regime: bool = False,
) -> dict[str, object]:
    """Join immutable records, keeping each gate/configuration/policy family separate.

    Candidate IDs deduplicate retries even if another gate changed the observation ID.
    Conflicting answers or candidate definitions fail explicitly instead of choosing a
    favorable result. A candidate may appear in multiple gate views; never sum those views.
    """
    policy = HypotheticalExecutionPolicy().identity if policy_identity is None else policy_identity
    if not policy.strip():
        raise ValueError("policy_identity must not be empty")
    rows = repository.list_counterfactual_outcomes()
    available = sorted({str(row["policy_identity"]) for row in rows})
    outcomes: dict[str, CounterfactualOutcome] = {}
    for row in rows:
        if row["policy_identity"] != policy:
            continue
        outcome = CounterfactualOutcome.model_validate(
            {key: value for key, value in row.items() if key != "outcome_id"}
        )
        for number in (
            outcome.net_excess_bps,
            outcome.gross_return_bps,
            outcome.benchmark_return_bps,
        ):
            if number is not None and not number.is_finite():
                raise ValueError(f"non-finite counterfactual outcome for {outcome.observation_id}")
        if outcome.observation_id in outcomes:
            raise ValueError(f"multiple outcomes under one policy for {outcome.observation_id}")
        outcomes[outcome.observation_id] = outcome
    snapshots = (
        {str(row["snapshot_id"]): row for row in repository.list_market_regime_snapshots()}
        if by_regime
        else {}
    )
    groups: dict[tuple[str, str, str, int, str, str], _Group] = {}
    candidates: set[tuple[str, str, str]] = set()
    observed: set[str] = set()
    for parent, reason_rows in repository.list_rejected_candidates():
        refusal = RejectedCandidate.model_validate(
            {
                **parent,
                "reasons": reasons_from_rows(reason_rows),
            }
        )
        observed.add(refusal.observation_id)
        candidates.add((refusal.strategy_id, refusal.configuration_hash, refusal.candidate_id))
        selected_outcome = outcomes.get(refusal.observation_id)
        regime, regime_reasons = _regime(refusal, snapshots) if by_regime else (None, ())
        for reason in refusal.reasons:
            key = (
                refusal.strategy_id,
                refusal.configuration_hash,
                reason.gate_id,
                reason.gate_version,
                reason.rule_parameters_hash,
                json.dumps(regime, sort_keys=True),
            )
            group = groups.setdefault(key, _Group(*key[:5], regime=regime))
            previous = group.candidates.get(refusal.candidate_id)
            if previous is not None:
                prior, answer = previous
                if _answer(answer) != _answer(selected_outcome) or (
                    prior.session_date,
                    prior.symbol,
                    prior.direction,
                    prior.reference_price,
                ) != (
                    refusal.session_date,
                    refusal.symbol,
                    refusal.direction,
                    refusal.reference_price,
                ):
                    raise ValueError(f"conflicting records for candidate {refusal.candidate_id}")
            group.candidates[refusal.candidate_id] = (refusal, selected_outcome)
            group.observation_ids.add(refusal.observation_id)
            if refusal.market_regime_snapshot_id is not None:
                group.snapshot_ids.add(refusal.market_regime_snapshot_id)
            group.regime_reasons.update(regime_reasons)
    if set(outcomes) - observed:
        raise ValueError("counterfactual outcomes reference absent refusals")
    reports = [_summarize(group, policy) for _, group in sorted(groups.items())]
    # Keep the verdict typed here; the returned representation is JSON-compatible.
    verdicts: Counter[GateVerdict] = Counter()
    for report in reports:
        value = report["gate_value"]
        assert isinstance(value, dict)
        verdicts[GateVerdict(value["verdict"])] += 1
    return {
        "ok": True,
        "analysis_role": "EXPLORATORY",
        "policy_identity": policy,
        "available_policy_identities": available,
        "by_regime": by_regime,
        "distinct_candidates": len(candidates),
        "refusal_observations": len(observed),
        "underpowered_groups": verdicts[GateVerdict.UNDERPOWERED],
        "no_evaluated_outcome_groups": verdicts[GateVerdict.NO_EVALUATED_OUTCOMES],
        "groups": reports,
        "limitations": [
            "NO_RECORDED_OUTCOME means no answer under this policy; maturity is unknown.",
            "Gate groups overlap; do not sum them as independent candidates.",
            "Dependence-group membership is not recorded here; the existing session estimator "
            "cannot account for every shared event or overlapping horizon.",
            "Regime definitions use stored method versions and axes, never reconstructed labels.",
            "Exploratory means and verdicts are not causal gate effects or confidence intervals. "
            "This command grants no policy authority or untouched-confirmation status; threshold "
            "changes require registered research and search accounting.",
        ],
    }
