"""Prospective forecast diagnostics and explicit next-minute simulated execution.

These are local forward predictions and a cost model, never broker fills or promotion.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

from quantbot.domain import Bar
from quantbot.learner.intraday import IntradayLabel, IntradayPrediction
from quantbot.learner.metrics import spearman


def score_cohort(
    predictions: Sequence[IntradayPrediction],
    labels: Sequence[IntradayLabel],
    bars: Sequence[Bar],
    *,
    source_snapshot_hash: str,
) -> dict[str, Any] | None:
    if not predictions:
        return None
    indexed = {(b.symbol, b.timestamp): b for b in bars}
    resolved = {label.features.symbol: label for label in labels}
    if set(resolved) != {p.features.symbol for p in predictions}:
        raise ValueError("forecast and outcome cohorts differ")
    if len({p.model_id for p in predictions}) != 1:
        raise ValueError("mixed model cohort")
    if (
        len(resolved) != len(labels)
        or len(resolved) != len(predictions)
        or len({p.features.target_at for p in predictions}) != 1
        or len({p.features.decision_at for p in predictions}) != 1
    ):
        raise ValueError("duplicate or mixed-time forecast cohort")
    recorded_at = max(p.recorded_at for p in predictions)
    entry_at = recorded_at.replace(second=0, microsecond=0) + timedelta(minutes=1)
    exit_at = predictions[0].features.target_at
    if entry_at >= exit_at:
        return None
    returns = {}
    for symbol in {p.features.symbol for p in predictions} | {"SPY"}:
        entry = indexed.get((symbol, entry_at))
        target = indexed.get((symbol, exit_at - timedelta(minutes=1)))
        if entry is None or target is None:
            return None
        returns[symbol] = float(target.close) * 0.9995 / (float(entry.open) * 1.0005) - 1
    for p in predictions:
        if p.features != resolved[p.features.symbol].features:
            raise ValueError("forecast outcome feature lineage differs")
    ordered = sorted(predictions, key=lambda p: (-p.score, p.features.symbol))
    threshold = ordered[max(1, math.ceil(len(ordered) / 5)) - 1].score
    selected = [p.features.symbol for p in ordered if p.score >= threshold]
    return dict(
        model_id=predictions[0].model_id,
        evidence="PROSPECTIVE_LOCAL_SIMULATION",
        entry_at=entry_at.isoformat(),
        exit_at=exit_at.isoformat(),
        predictions=len(predictions),
        mse=fmean((p.score - resolved[p.features.symbol].excess_return) ** 2 for p in predictions),
        rank_ic=spearman(
            [p.score for p in predictions],
            [resolved[p.features.symbol].excess_return for p in predictions],
        ),
        net_return=fmean(returns[s] for s in selected),
        equal_weight_return=fmean(returns[p.features.symbol] for p in predictions),
        spy_return=returns["SPY"],
        slippage_bps_per_side=5,
        commission_per_order=0,
        selected_symbols=selected,
        paper_fills=False,
        execution_source_snapshot=source_snapshot_hash,
        prediction_ids=[p.prediction_id for p in predictions],
        label_ids=[label.label_id for label in labels],
    )


def summarize(reports: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only matched holding windows; discard overlapping portfolios within each model."""
    by_model: dict[str, dict[tuple[str, str], dict[str, Any]]] = {}
    for report in reports:
        key = (report["entry_at"], report["exit_at"])
        rows = by_model.setdefault(report["model_id"], {})
        if key in rows:
            raise ValueError("duplicate model execution window")
        rows[key] = report
    if not by_model:
        return []
    dates = sorted(set.intersection(*(set(rows) for rows in by_model.values())))
    result = []
    for model_id, rows in sorted(by_model.items()):
        equity = peak = 1.0
        drawdown = 0.0
        last_exit: datetime | None = None
        used = []
        for window in dates:
            at = window[0]
            row = rows[window]
            if last_exit is not None and datetime.fromisoformat(at) < last_exit:
                continue
            used.append(row)
            last_exit = datetime.fromisoformat(row["exit_at"])
            equity *= 1 + row["net_return"]
            peak = max(peak, equity)
            drawdown = max(drawdown, 1 - equity / peak)
        defined = [r["rank_ic"] for r in used if r["rank_ic"] is not None]
        result.append(
            dict(
                model_id=model_id,
                matched_periods=len(used),
                simulated_net_return=equity - 1 if used else None,
                simulated_max_drawdown=drawdown if used else None,
                rank_ic=fmean(defined) if defined else None,
                mse=fmean(r["mse"] for r in used) if used else None,
            )
        )
    return result
