"""Fixed, descriptive next-open portfolio replay; no execution or promotion authority."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from quantbot.learner.data import DailyInput, _calendar_hash
from quantbot.learner.metrics import ScoredRow, evaluate_predictions
from quantbot.learner.models import EvaluationClass
from quantbot.learner.repository import LearnerRepository
from quantbot.learner.store import LIMITATIONS, verify_source
from quantbot.research.capital import BASELINE_SLIPPAGE_BPS
from quantbot.research.manifest import canonical_result_json, content_id
from quantbot.research.registry import DataRole
from quantbot.research.reproducibility import capture_git_state
from quantbot.storage import Database
from quantbot.strategy import XNYSSessionSequence

CONTRACT = "learner-next-open-fifth-close-liquidated-v1"


def simulate(
    rows: Sequence[ScoredRow], inputs: Sequence[DailyInput], calendar: XNYSSessionSequence,
    *, expected_count: int,
) -> dict[str, Any]:
    predictive = evaluate_predictions(rows, expected_count=expected_count)
    if len(rows) != expected_count:
        raise ValueError("portfolio evaluation requires complete outcome coverage")
    indexed = {(r.bar.symbol, r.bar.timestamp): r.bar for r in inputs}
    if len(indexed) != len(inputs):
        raise ValueError("duplicate execution prices")
    groups: dict[str, list[ScoredRow]] = defaultdict(list)
    for row in rows:
        groups[row.cohort_id].append(row)
    ordered = sorted(groups.values(), key=lambda g: (g[0].as_of, g[0].cohort_id))
    roster = {r.symbol for r in ordered[0]}
    if any({r.symbol for r in g} != roster for g in ordered):
        raise ValueError("complete matching universe required in every cohort")
    if len({g[0].as_of for g in ordered}) != len(ordered):
        raise ValueError("duplicate decision session")
    closes = [s.close_at for s in calendar.sessions]
    periods: list[dict[str, Any]] = []
    previous_exit = -1
    for group in ordered:
        first = closes.index(group[0].as_of)
        last = closes.index(group[0].target_at)
        if last - first != 5:
            raise ValueError("portfolio target must be exactly five exchange sessions later")
        if first < previous_exit:
            continue
        previous_exit = last
        cutoff = sorted((r.score for r in group), reverse=True)[math.ceil(len(group) * .2) - 1]
        periods.append({
            "decision_at": closes[first].isoformat(),
            "entry_at": calendar.sessions[first + 1].open_at.isoformat(),
            "exit_at": closes[last].isoformat(), "first": first, "last": last,
            "top": sorted(r.symbol for r in group if r.score >= cutoff),
        })
    costs = float(BASELINE_SLIPPAGE_BPS) / 10000
    portfolios: dict[str, Any] = {}
    for name in ("top_quintile", "all_universe", "spy"):
        equity = peak = 1.0
        drawdown = cost_total = traded = 0.0
        daily: list[dict[str, Any]] = []
        active_days = 0
        for period in periods:
            selected = (period["top"] if name == "top_quintile" else
                        sorted(roster) if name == "all_universe" else ["SPY"])
            first, last = period["first"], period["last"]
            for symbol in selected:
                if any((symbol, closes[i]) not in indexed for i in range(first + 1, last + 1)):
                    raise ValueError("missing exact execution or daily marking price")
            allocation = equity / len(selected)
            units = {s: allocation / (float(indexed[s, closes[first + 1]].open) * (1 + costs))
                     for s in selected}
            entry_cost = sum(units[s] * float(indexed[s, closes[first + 1]].open) * costs
                             for s in selected)
            traded += equity
            cost_total += entry_cost
            # Entry mark includes immediate slippage, even if the same day's close rallies.
            drawdown = max(drawdown, 1 - (equity - entry_cost) / peak)
            for i in range(first + 1, last + 1):
                equity = sum(units[s] * float(indexed[s, closes[i]].close) for s in selected)
                if i == last:
                    cost_total += equity * costs
                    equity *= 1 - costs
                    traded += equity
                peak = max(peak, equity)
                drawdown = max(drawdown, 1 - equity / peak)
                daily.append({"at": closes[i].isoformat(), "equity": equity})
                active_days += 1
        portfolios[name] = {
            "net_return": equity - 1, "max_drawdown": drawdown,
            "cost_over_initial": cost_total, "traded_notional_over_initial": traded,
            "mean_daily_gross_exposure": active_days / (periods[-1]["last"] - periods[0]["first"]),
            "daily_equity": daily,
        }
    for summary in portfolios.values():
        summary["excess_return_vs_spy"] = summary["net_return"] - portfolios["spy"]["net_return"]
    return {
        "contract": CONTRACT, "evidence_class": "EXPLORATORY_HISTORICAL_REPLAY",
        "positive_expectancy_justified": False, "promotion": "NONE",
        "costs": {"slippage_bps_per_side": str(BASELINE_SLIPPAGE_BPS),
                  "commission_per_order": "0", "calibration": "ASSUMED_BASELINE"},
        "period_count": len(periods), "periods": periods, "portfolios": portfolios,
        "predictive": predictive,
        "limitations": LIMITATIONS + " Fractional units; close marks miss intraday drawdowns. "
        "Full liquidation each horizon; no netting, financing, impact or NBBO calibration. "
        "Predefined target-close exit; predictive labels remain close-to-close. "
        "SPY is held on matched windows, not continuously through cash gaps. "
        "Nonoverlapping periods are not proven statistically independent.",
    }


def evaluate_store(
    database: Database, model_id: str, calendar: XNYSSessionSequence,
) -> dict[str, Any]:
    with database.transaction() as session:
        repo = LearnerRepository(session)
        model = repo.get_model(model_id)
        dataset = repo.get_dataset(model.dataset_id)
        if not dataset.source_id.startswith("stored-replay:"):
            raise ValueError("economic replay requires a model trained from the stored source")
        predictions = repo.list_predictions(model_id)
        if not predictions or any(p.evaluation_class is not EvaluationClass.HISTORICAL_REPLAY
                                  for p in predictions):
            raise ValueError("economic replay requires historical predictions exclusively")
        if any(p.features.calendar_hash != _calendar_hash(calendar) for p in predictions):
            raise ValueError("execution calendar differs from prediction lineage")
        expected = {r.features.identity for r in dataset.rows if r.role is DataRole.VALIDATION}
        if (
            {p.features.identity for p in predictions} != expected
            or len(predictions) != len(expected)
        ):
            raise ValueError("economic replay requires the complete validation prediction set")
        closes = [s.close_at for s in calendar.sessions]
        first = closes.index(min(p.features.as_of for p in predictions)) + 1
        last = closes.index(max(p.features.target_at for p in predictions))
        inputs = tuple(r for r in verify_source(session, dataset, calendar)
                       if closes[first] <= r.bar.timestamp <= closes[last])
        indexed = {(r.bar.symbol, r.bar.timestamp): r for r in inputs}
        rows = []
        outcomes = []
        for p in predictions:
            o = repo.get_outcome(p.prediction_id)
            if o is None:
                raise ValueError("economic replay requires complete resolved outcomes")
            f = p.features
            if content_id([
                indexed[f.symbol, f.target_at], indexed["SPY", f.target_at]
            ]) != o.source_hash:
                raise ValueError("execution prices differ from the immutable resolved outcome")
            outcomes.append(o)
            rows.append(ScoredRow(p.prediction_id, p.cohort_id, f.symbol, f.as_of, f.target_at,
                                  p.score, o.raw_return, o.benchmark_return, o.excess_return))
        result = simulate(rows, inputs, calendar, expected_count=len(predictions))
        root = Path(__file__).resolve().parents[3]
        commit, dirty = capture_git_state(root)
        result["lineage"] = {
            "model_id": model_id, "model_hash": content_id(model), "dataset_id": dataset.identity,
            "source_id": dataset.source_id, "predictions_hash": content_id(predictions),
            "outcomes_hash": content_id(outcomes), "calendar_hash": _calendar_hash(calendar),
            "execution_inputs_hash": content_id(inputs),
            "code_commit": commit, "code_dirty": dirty,
        }
        # Pin actual execution inputs, not only a hash pointing at potentially changed prices.
        result["execution_inputs"] = [r.model_dump(mode="json") for r in inputs]
        result["calendar"] = calendar.model_dump(mode="json")
        result["prediction_ids"] = [p.prediction_id for p in predictions]
    return result


def write_report(path: Path, report: dict[str, Any]) -> None:
    payload = canonical_result_json({"report_hash": content_id(report), "report": report}) + "\n"
    with path.open("x", encoding="utf-8") as output:
        output.write(payload)
