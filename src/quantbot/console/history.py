"""Bounded, hash-checked saved replay display; no database, fitting or broker IO."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from quantbot.research.manifest import content_id

NAMES = {
    "mean": "Mean / equal-weight baseline",
    "ridge": "Ridge",
    "ridge_successor": "Ridge successor",
}


def _windows(report: dict[str, Any]) -> list[tuple[str, ...]]:
    return [tuple(p[k] for k in ("decision_at", "entry_at", "exit_at")) for p in report["periods"]]


class HistoryPoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    at: str
    equity: float = Field(gt=0)

    @field_validator("at")
    @classmethod
    def aware(cls, value: str) -> str:
        if datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("aware mark required")
        return value


class HistorySeries(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    name: Literal["Mean / equal-weight baseline", "Ridge", "Ridge successor", "Matched SPY"]
    net_return: float
    max_drawdown: float
    excess_return_vs_spy: float
    cost_over_initial: float
    traded_notional_over_initial: float
    daily_equity: tuple[HistoryPoint, ...]


class HistorySection(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    role: Literal["TRAINING_IN_SAMPLE", "VALIDATION_REUSED"]
    start: str
    end: str
    periods: int = Field(ge=1)
    raw_cohorts: int = Field(ge=1)
    sessions: int = Field(ge=1)
    instruments: int = Field(ge=1)
    series: tuple[HistorySeries, ...]

    @property
    def title(self) -> str:
        return (
            "Training replay — in sample"
            if self.role == "TRAINING_IN_SAMPLE"
            else "Validation replay — reused during model development"
        )

    @property
    def chart(self) -> dict[str, Any]:
        values = [1.0] + [p.equity for r in self.series for p in r.daily_equity]
        low, high = min(values), max(values)
        span = max(high - low, 0.01)
        dates = sorted({p.at for r in self.series for p in r.daily_equity})
        indexes = {at: i + 1 for i, at in enumerate(dates)}
        curves = []
        for row in self.series:
            points = [(0, 1.0)] + [(indexes[p.at], p.equity) for p in row.daily_equity]
            curves.append(
                {
                    "name": row.name,
                    "points": " ".join(
                        f"{50 + i / max(len(dates), 1) * 700:.2f},"
                        f"{20 + (high - v) / span * 220:.2f}"
                        for i, v in points
                    ),
                }
            )
        return dict(low=low * 100, high=high * 100, curves=curves)


class HistoryPanel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    selected: str = "ALL"
    options: tuple[str, ...] = ()
    sections: tuple[HistorySection, ...] = ()
    reason: str = "Expanded replay has not been generated from verified saved inputs."


def read_history(
    root: Path, models: dict[str, dict[str, Any]], selected: str = "ALL"
) -> HistoryPanel:
    try:
        if selected != "ALL" and re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", selected) is None:
            raise ValueError("invalid range")
        path = root / "evidence/history-v1.json"
        if path.stat().st_size > 10_000_000:
            raise ValueError("oversized history")
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        bundle = payload["document"]
        if payload["hash"] != content_id(bundle) or bundle["schema"] != 1:
            raise ValueError("bundle integrity")
        verified = []
        signatures = set()
        for view in bundle["views"]:
            role = view["role"]
            if role not in ("TRAINING_IN_SAMPLE", "VALIDATION_REUSED"):
                raise ValueError("role")
            period = view["range"]
            if period != "ALL" and re.fullmatch(r"20[0-9]{2}-(0[1-9]|1[0-2])", period) is None:
                raise ValueError("range")
            tag = view["tag"]
            if tag not in NAMES or (period, role, tag) in signatures:
                raise ValueError("duplicate or unknown model")
            signatures.add((period, role, tag))
            report = view["report"]
            lineage = report["lineage"]
            model = models[lineage["model_id"]]
            if (
                content_id(report) != view["report_hash"]
                or lineage["model_hash"] != content_id(model)
                or lineage["dataset_id"] != model["dataset_id"]
                or report["view_role"] != role
                or report["source_evidence"] != "LOCAL_DECLARED"
                or report["promotion"] != "NONE"
                or report["positive_expectancy_justified"] is not False
                or report["contract"] != "learner-next-open-fifth-close-liquidated-v1"
                or str(report["costs"]["slippage_bps_per_side"]) != "5"
                or str(report["costs"]["commission_per_order"]) != "0"
            ):
                raise ValueError("lineage or costs")
            if model["family"] != ("mean" if tag == "mean" else "ridge"):
                raise ValueError("family")
            if not report["periods"] or len(report["periods"]) != report["period_count"]:
                raise ValueError("period counts")
            expected_start = datetime.fromisoformat(
                model["training_start" if role == "TRAINING_IN_SAMPLE" else "validation_start"]
            )
            expected_end = datetime.fromisoformat(
                model["training_end" if role == "TRAINING_IN_SAMPLE" else "validation_end"]
            )
            for window in report["periods"]:
                decision, entry, exit = (
                    datetime.fromisoformat(window[k])
                    for k in ("decision_at", "entry_at", "exit_at")
                )
                if (
                    any(t.tzinfo is None for t in (decision, entry, exit))
                    or not expected_start <= decision < entry < exit <= expected_end
                ):
                    raise ValueError("period outside declared role")
            for portfolio in report["portfolios"].values():
                for point in portfolio["daily_equity"]:
                    at = datetime.fromisoformat(point["at"])
                    if (
                        at.tzinfo is None
                        or not expected_start <= at <= expected_end
                        or point["equity"] <= 0
                    ):
                        raise ValueError("invalid mark")
            verified.append(view)
        options = tuple(sorted({v["range"] for v in verified if v["range"] != "ALL"}))
        if selected != "ALL" and selected not in options:
            raise ValueError("unavailable range")
        sections = []
        for role in ("TRAINING_IN_SAMPLE", "VALIDATION_REUSED"):
            views = [v for v in verified if v["range"] == selected and v["role"] == role]
            if not views:
                continue
            views.sort(key=lambda v: list(NAMES).index(v["tag"]))
            baseline = views[0]["report"]
            series = []
            for view in views:
                report = view["report"]
                if _windows(report) != _windows(baseline) or any(
                    report["portfolios"][k] != baseline["portfolios"][k]
                    for k in ("all_universe", "spy")
                ):
                    raise ValueError("unmatched windows")
                series.append(
                    HistorySeries(
                        name=NAMES[view["tag"]],
                        **{
                            k: report["portfolios"]["top_quintile"][k]
                            for k in HistorySeries.model_fields
                            if k != "name"
                        },
                    )
                )
            series.append(
                HistorySeries(
                    name="Matched SPY",
                    **{
                        k: baseline["portfolios"]["spy"][k]
                        for k in HistorySeries.model_fields
                        if k != "name"
                    },
                )
            )
            sections.append(
                HistorySection(
                    role=role,
                    start=baseline["periods"][0]["entry_at"],
                    end=baseline["periods"][-1]["exit_at"],
                    periods=baseline["period_count"],
                    raw_cohorts=baseline["raw_cohorts"],
                    instruments=baseline["instrument_count"],
                    sessions=len({p["at"] for p in baseline["portfolios"]["spy"]["daily_equity"]}),
                    series=tuple(series),
                )
            )
        return HistoryPanel(selected=selected, options=options, sections=tuple(sections), reason="")
    except (OSError, ValueError, KeyError, TypeError, IndexError, OverflowError):
        return HistoryPanel(
            selected="ALL",
            reason=(
                "Expanded replay unavailable or failed range, integrity or lineage checks. "
                "Missing values are not zero."
            ),
        )
