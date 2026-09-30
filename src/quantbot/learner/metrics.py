"""Descriptive cross-sectional diagnostics, never a promotion verdict."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, stdev


def _ranks(values: Sequence[float]) -> tuple[float, ...]:
    ranked = [0.0] * len(values)
    order = sorted(range(len(values)), key=lambda i: values[i])
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        for index in order[start:end]:
            ranked[index] = (start + end - 1) / 2 + 1
        start = end
    return tuple(ranked)


def spearman(left: Sequence[float], right: Sequence[float]) -> float | None:
    if len(left) != len(right):
        raise ValueError("rank inputs must have equal lengths")
    if not all(math.isfinite(x) for x in (*left, *right)):
        raise ValueError("rank inputs must be finite")
    if len(left) < 2:
        return None
    x, y = _ranks(left), _ranks(right)
    mx, my = mean(x), mean(y)
    denominator = math.sqrt(sum((v - mx) ** 2 for v in x) * sum((v - my) ** 2 for v in y))
    if denominator == 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y, strict=True)) / denominator


@dataclass(frozen=True)
class ScoredRow:
    prediction_id: str
    cohort_id: str
    symbol: str
    as_of: datetime
    target_at: datetime
    score: float
    raw_return: float
    benchmark_return: float
    excess_return: float


def evaluate_predictions(
    rows: Sequence[ScoredRow],
    *,
    expected_count: int,
    quantile: float = 0.2,
) -> dict[str, object]:
    if not rows:
        raise ValueError("no resolved out-of-sample predictions; no training fallback")
    if expected_count < len(rows) or not 0 < quantile <= 0.5:
        raise ValueError("invalid coverage denominator or quantile")
    if len({row.prediction_id for row in rows}) != len(rows):
        raise ValueError("duplicate prediction identity")
    groups: dict[str, list[ScoredRow]] = defaultdict(list)
    for row in rows:
        if not all(
            math.isfinite(x)
            for x in (
                row.score,
                row.raw_return,
                row.benchmark_return,
                row.excess_return,
            )
        ):
            raise ValueError("scored values must be finite")
        if row.target_at <= row.as_of:
            raise ValueError("target must follow prediction")
        groups[row.cohort_id].append(row)
    periods: list[dict[str, object]] = []
    ics: list[float] = []
    independent_ics: list[float] = []
    last_target: datetime | None = None
    top_raw: list[float] = []
    bottom_raw: list[float] = []
    top_excess: list[float] = []
    bottom_excess: list[float] = []
    turnover: list[float] = []
    previous_top: set[str] | None = None
    ranking_outcomes: list[dict[str, object]] = []
    for group in sorted(groups.values(), key=lambda values: (values[0].as_of, values[0].cohort_id)):
        first = group[0]
        if len({r.symbol for r in group}) != len(group):
            raise ValueError("duplicate instrument in cross-section")
        if any((r.as_of, r.target_at) != (first.as_of, first.target_at) for r in group):
            raise ValueError("cross-section must have one prediction and target session")
        if len(rows) == expected_count:
            realized_ranks = _ranks(tuple(r.excess_return for r in group))
            ranking_outcomes.extend(
                {
                    "prediction_id": row.prediction_id,
                    "realized_percentile": (rank - 0.5) / len(group),
                }
                for row, rank in zip(group, realized_ranks, strict=True)
            )
        ic = spearman(tuple(r.score for r in group), tuple(r.excess_return for r in group))
        # A five-name minimum and tied cutoffs avoid a symbol-name tiebreak becoming signal.
        if len(group) < 5 or len(rows) < expected_count:
            ic = None
            previous_top = None
        period: dict[str, object] = {
            "as_of": first.as_of.isoformat(),
            "target_at": first.target_at.isoformat(),
            "sample_count": len(group),
            "rank_ic": ic,
        }
        if ic is not None:
            ics.append(ic)
            if last_target is None or first.as_of >= last_target:
                independent_ics.append(ic)
                last_target = first.target_at
            ordered = sorted(group, key=lambda r: r.score)
            count = max(1, math.ceil(len(group) * quantile))
            low, high = ordered[count - 1].score, ordered[-count].score
            if low < high:
                bottom = [r for r in group if r.score <= low]
                top = [r for r in group if r.score >= high]
                tr, br = mean(r.raw_return for r in top), mean(r.raw_return for r in bottom)
                te, be = mean(r.excess_return for r in top), mean(r.excess_return for r in bottom)
                top_raw.append(tr)
                bottom_raw.append(br)
                top_excess.append(te)
                bottom_excess.append(be)
                period.update(top_raw_return=tr, bottom_raw_return=br, excess_spread=te - be)
                selected = {r.symbol for r in top}
                if previous_top is not None:
                    turnover.append(
                        sum(
                            abs(
                                (1 / len(selected) if s in selected else 0)
                                - (1 / len(previous_top) if s in previous_top else 0)
                            )
                            for s in selected | previous_top
                        )
                        / 2
                    )
                previous_top = selected
        periods.append(period)
    # No IID t-stat on overlapping five-session returns. This statistic uses a deterministic
    # non-overlapping subset; serial dependence may still remain, so it is descriptive only.
    ic_t: float | None = None
    if len(independent_ics) >= 20 and stdev(independent_ics) > 0:
        ic_t = mean(independent_ics) / (stdev(independent_ics) / math.sqrt(len(independent_ics)))
    directional = [r for r in rows if r.score != 0 and r.excess_return != 0]
    return {
        "evidence_status": "DESCRIPTIVE_ONLY" if ic_t is not None else "INSUFFICIENT_EVIDENCE",
        "positive_expectancy_justified": False,
        "sample_count": len(rows),
        "expected_count": expected_count,
        "coverage": len(rows) / expected_count,
        "period_count": len(groups),
        "valid_ic_periods": len(ics),
        "mean_ic": mean(ics) if ics else None,
        "ic_std": stdev(ics) if len(ics) > 1 else None,
        "ic_t_stat": ic_t,
        "ic_t_stat_method": "nonoverlapping_subset_descriptive",
        "nonoverlapping_periods": len(independent_ics),
        "nonoverlapping_mean_ic": mean(independent_ics) if independent_ics else None,
        "top_quantile_raw_return": mean(top_raw) if top_raw else None,
        "bottom_quantile_raw_return": mean(bottom_raw) if bottom_raw else None,
        "top_quantile_excess_return": mean(top_excess) if top_excess else None,
        "bottom_quantile_excess_return": mean(bottom_excess) if bottom_excess else None,
        "top_minus_bottom_raw_spread": mean(top_raw) - mean(bottom_raw) if top_raw else None,
        "top_minus_bottom_excess_spread": (
            mean(top_excess) - mean(bottom_excess) if top_excess else None
        ),
        "directional_hit_rate": (
            sum((r.score > 0) == (r.excess_return > 0) for r in directional) / len(directional)
            if directional
            else None
        ),
        "equal_weight_top_quantile_turnover": mean(turnover) if turnover else None,
        "periods": periods,
        "ranking_outcomes": ranking_outcomes,
        "limitations": "Gross close-to-close labels; no costs, risk or promotion qualification. "
        "Nonoverlap removes mechanical horizon overlap, not all serial dependence.",
    }
